from fastapi import FastAPI, HTTPException, Path, Query, Response
from fastapi.staticfiles import StaticFiles
from contextlib import asynccontextmanager
from uuid import UUID
import asyncpg
import redis.asyncio as aioredis
import os
import json
from datetime import timedelta

DB_DSN = os.getenv("DB_DSN", "postgres://mana_user:mana_password@localhost:5432/mana_market")
REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0")

MIN_BASE_PRICE = 1.00
MIN_PERCENT_CHANGE = 15.0
MIN_ABSOLUTE_CHANGE = 0.50
CACHE_TTL_SECONDS = 86400  # 24 hours (synchronized with daily bulk ingestion schedule)

EXCLUDED_CARD_CLAUSE = """
    AND (c.raw_scryfall_data->>'type_line' IS NULL
         OR c.raw_scryfall_data->>'type_line' NOT ILIKE 'Basic Land%')
    AND c.raw_scryfall_data->>'frame' IN ('1993', '1997', '2003', '2015')
    AND c.raw_scryfall_data->>'border_color' IN ('black', 'white', 'silver')
    AND NOT c.raw_scryfall_data->>'is_full_art' = 'true'
    AND NOT c.raw_scryfall_data->>'is_textless' = 'true'
    AND NOT c.raw_scryfall_data->>'is_story_spotlight' = 'true'
    AND NOT EXISTS (
        SELECT 1
        FROM jsonb_array_elements_text(COALESCE(c.raw_scryfall_data->'frame_effects', '[]'::jsonb)) AS fe(eff)
        WHERE fe.eff IN ('showcase', 'extendedart', 'extended', 'colortreatment')
    )
"""


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup: Initialize DB and Redis connection pools
    app.state.db_pool = await asyncpg.create_pool(DB_DSN, min_size=5, max_size=20)
    app.state.redis = aioredis.from_url(REDIS_URL, decode_responses=True)
    print("Database and Redis connection pools established.")
    
    yield
    
    # Shutdown: Close pools gracefully
    await app.state.db_pool.close()
    await app.state.redis.aclose()
    print("Connection pools closed.")


app = FastAPI(
    title="Mana Market API",
    description="Core financial API for MTG market data with Redis caching",
    version="1.0.1",
    lifespan=lifespan
)


async def cached_json_response(redis: aioredis.Redis, cache_key: str, producer_fn, ttl: int = CACHE_TTL_SECONDS) -> Response:
    """Returns cached JSON response from Redis or executes producer_fn, caches, and returns fresh data."""
    cached_data = await redis.get(cache_key)
    if cached_data:
        return Response(content=cached_data, media_type="application/json")

    data = await producer_fn()
    json_bytes = json.dumps(data, default=str)
    await redis.set(cache_key, json_bytes, ex=ttl)
    return Response(content=json_bytes, media_type="application/json")


@app.get("/health")
async def health_check():
    return {"status": "healthy", "service": "mana-market-api"}


@app.get("/api/v1/movers")
async def get_market_movers(
    direction: str = Query("gainers", pattern="^(gainers|losers)$"),
    days: int = Query(1, ge=1, le=30),
    limit: int = Query(10, ge=1, le=50),
    format: str = Query("all", pattern="^(all|standard|pioneer|modern)$")
):
    """Fetch the top market gainers or losers filtered by MTG format, leveraging Redis cache.

    Format legality is read directly from the Scryfall legalities JSONB field stored
    in raw_scryfall_data. This means rotation and ban list changes are automatically
    reflected the next time the daily ingestion worker runs.
    """
    cache_key = f"api:movers:{direction}:d{days}:l{limit}:f{format}"

    async def fetch_movers():
        # Build the format legality clause dynamically.
        # Scryfall legalities values: "legal", "not_legal", "banned", "restricted"
        format_clause = ""
        if format != "all":
            format_clause = f"AND c.raw_scryfall_data->'legalities'->>'{ format }' = 'legal'"

        query = f"""
        WITH current_prices AS (
            SELECT DISTINCT ON (card_id) card_id, price_usd AS price_now
            FROM fact_card_prices WHERE price_usd IS NOT NULL
            ORDER BY card_id, timestamp DESC
        ),
        past_prices AS (
            SELECT DISTINCT ON (card_id) card_id, price_usd AS price_past
            FROM fact_card_prices
            WHERE price_usd IS NOT NULL AND timestamp <= NOW() - $1::interval
            ORDER BY card_id, timestamp DESC
        )
        SELECT c.card_id, c.name, c.set_code, c.rarity, cp.price_now, pp.price_past,
               (cp.price_now - pp.price_past) AS abs_change,
               ROUND(((cp.price_now - pp.price_past) / pp.price_past * 100), 2) AS percent_change
        FROM current_prices cp
        JOIN past_prices pp ON cp.card_id = pp.card_id
        JOIN dim_cards c ON cp.card_id = c.card_id
        WHERE pp.price_past >= $2
          AND ABS(cp.price_now - pp.price_past) >= $3
          AND ABS((cp.price_now - pp.price_past) / pp.price_past * 100) >= $4
          { EXCLUDED_CARD_CLAUSE }
          { format_clause }
        """
        if direction == "gainers":
            query += " AND (cp.price_now - pp.price_past) > 0 ORDER BY percent_change DESC LIMIT $5;"
        else:
            query += " AND (cp.price_now - pp.price_past) < 0 ORDER BY percent_change ASC LIMIT $5;"

        async with app.state.db_pool.acquire() as conn:
            rows = await conn.fetch(
                query,
                timedelta(days=days),
                MIN_BASE_PRICE,
                MIN_ABSOLUTE_CHANGE,
                MIN_PERCENT_CHANGE,
                limit
            )
            return {
                "window_days": days,
                "direction": direction,
                "format": format,
                "cached": False,
                "results": [dict(row) for row in rows]
            }

    return await cached_json_response(app.state.redis, cache_key, fetch_movers)


@app.get("/api/v1/cards/search")
async def search_cards(
    q: str = Query(..., min_length=1, max_length=100, description="Card name search query"),
    limit: int = Query(10, ge=1, le=50, description="Maximum number of results to return")
):
    """Search for cards by name with current market prices and artwork URIs."""
    clean_query = q.strip()
    cache_key = f"api:cards:search:{clean_query.lower()}:l{limit}"

    async def fetch_search():
        query = """
        SELECT
            c.card_id,
            c.oracle_id,
            c.name,
            c.set_code,
            c.collector_number,
            c.rarity,
            c.mana_value,
            COALESCE(
                c.raw_scryfall_data->'image_uris'->>'normal',
                c.raw_scryfall_data->'card_faces'->0->'image_uris'->>'normal'
            ) AS image_uri,
            COALESCE(
                c.raw_scryfall_data->'image_uris'->>'small',
                c.raw_scryfall_data->'card_faces'->0->'image_uris'->>'small'
            ) AS image_uri_small,
            c.raw_scryfall_data->>'scryfall_uri' AS scryfall_uri,
            p.price_usd,
            p.price_usd_foil,
            p.price_eur,
            p.timestamp AS price_updated_at
        FROM dim_cards c
        LEFT JOIN LATERAL (
            SELECT price_usd, price_usd_foil, price_eur, timestamp
            FROM fact_card_prices fp
            WHERE fp.card_id = c.card_id
            ORDER BY fp.timestamp DESC
            LIMIT 1
        ) p ON true
        WHERE c.name ILIKE '%' || $1 || '%'
        { EXCLUDED_CARD_CLAUSE }
        AND p.price_usd IS NOT NULL
        ORDER BY
            CASE
                WHEN LOWER(c.name) = LOWER($1) THEN 0
                WHEN LOWER(c.name) LIKE LOWER($1) || '%' THEN 1
                ELSE 2
            END,
            c.name ASC,
            c.set_code ASC
        LIMIT $2;
        """
        async with app.state.db_pool.acquire() as conn:
            rows = await conn.fetch(query, clean_query, limit)
            return {
                "query": clean_query,
                "count": len(rows),
                "cached": False,
                "results": [dict(row) for row in rows]
            }

    return await cached_json_response(app.state.redis, cache_key, fetch_search)


@app.get("/api/v1/cards/{card_id}/history")
async def get_card_history(
    card_id: UUID = Path(..., description="Unique card ID (UUID)"),
    days: int = Query(30, ge=1, le=365, description="Lookback window in days")
):
    """Retrieve historical daily price points and financial summary stats for a card."""
    cache_key = f"api:cards:{card_id}:history:d{days}"

    async def fetch_history():
        async with app.state.db_pool.acquire() as conn:
            card_row = await conn.fetchrow("""
                SELECT
                    card_id, oracle_id, name, set_code, collector_number, rarity, mana_value,
                    COALESCE(
                        raw_scryfall_data->'image_uris'->>'normal',
                        raw_scryfall_data->'card_faces'->0->'image_uris'->>'normal'
                    ) AS image_uri,
                    raw_scryfall_data->>'scryfall_uri' AS scryfall_uri
                FROM dim_cards
                WHERE card_id = $1;
            """, card_id)

            if not card_row:
                raise HTTPException(status_code=404, detail=f"Card with ID '{card_id}' not found")

            # Skip foil-only cards: no non-foil price to track
            if card_row.get("price_usd") is None:
                raise HTTPException(status_code=404, detail=f"Card with ID '{card_id}' is foil-only and not tracked")

            history_rows = await conn.fetch("""
                SELECT
                    timestamp,
                    vendor,
                    price_usd,
                    price_usd_foil,
                    price_eur
                FROM fact_card_prices
                WHERE card_id = $1
                  AND timestamp >= NOW() - $2::interval
                ORDER BY timestamp ASC;
            """, card_id, timedelta(days=days))

            history_list = [dict(r) for r in history_rows]
            valid_usd_prices = [float(h["price_usd"]) for h in history_list if h.get("price_usd") is not None]

            stats = None
            if valid_usd_prices:
                oldest_usd = valid_usd_prices[0]
                current_usd = valid_usd_prices[-1]
                abs_diff = round(current_usd - oldest_usd, 2)
                pct_diff = round((abs_diff / oldest_usd * 100), 2) if oldest_usd > 0 else 0.0
                stats = {
                    "current_price_usd": current_usd,
                    "oldest_price_usd": oldest_usd,
                    "abs_change_usd": abs_diff,
                    "percent_change_usd": pct_diff,
                    "min_price_usd": min(valid_usd_prices),
                    "max_price_usd": max(valid_usd_prices),
                    "data_points": len(valid_usd_prices)
                }

            return {
                "card": dict(card_row),
                "window_days": days,
                "stats": stats,
                "cached": False,
                "history": history_list
            }

    return await cached_json_response(app.state.redis, cache_key, fetch_history)


# Mount Static Files for the lightweight Daily Report Dashboard
STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")
if os.path.exists(STATIC_DIR):
    app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")

