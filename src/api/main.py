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
    AND LENGTH(c.set_code) = 3
    AND c.raw_scryfall_data->>'frame' IN ('1993', '1997', '2003', '2015')
    AND c.raw_scryfall_data->>'border_color' IN ('black', 'white', 'silver')
    AND COALESCE(c.raw_scryfall_data->>'is_full_art', 'false') != 'true'
    AND COALESCE(c.raw_scryfall_data->>'is_textless', 'false') != 'true'
    AND COALESCE(c.raw_scryfall_data->>'is_story_spotlight', 'false') != 'true'
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
    format: str = Query("all", pattern="^(all|standard|pioneer|modern)$"),
    tier: str = Query("confirmed", pattern="^(confirmed|watch)$")
):
    """Fetch the top market gainers or losers filtered by MTG format, leveraging Redis cache.

    Format legality is read directly from the Scryfall legalities JSONB field stored
    in raw_scryfall_data. This means rotation and ban list changes are automatically
    reflected the next time the daily ingestion worker runs.
    """
    cache_key = f"api:movers:{direction}:d{days}:l{limit}:f{format}:t{tier}"

    async def fetch_movers():
        # Build the format legality clause dynamically.
        # Scryfall legalities values: "legal", "not_legal", "banned", "restricted"
        # Priority: Standard > Pioneer > Modern. A card appears only in the
        # highest format it's legal in, so results are mutually exclusive.
        format_clause = ""
        if format != "all":
            if format == "standard":
                # Only allow current Standard sets to exclude reprints from older sets
                standard_sets = ("FRA", "HOB", "MSH", "SOS", "TMT", "ECL", "TLA", "OM1", "SPM", "EOE", "FIN", "TDM", "DFT", "FDN", "DSK", "BLB", "OTJ", "MKM", "LCI", "WOE", "BIG", "TRK")
                set_codes_clause = ", ".join(f"'{s}'" for s in standard_sets)
                format_clause = (
                    f"AND c.raw_scryfall_data->'legalities'->>'standard' = 'legal'"
                    f" AND UPPER(c.set_code) IN ({set_codes_clause})"
                )
            elif format == "pioneer":
                # Pioneer sets (non-Standard, to avoid reprints already in Standard section)
                pioneer_sets = ("MAT", "MOM", "ONE", "BRO", "DMU", "SNC", "NEO", "VOW", "MID", "AFR", "STX", "KHM", "ZNR", "M21", "IKO", "THB", "ELD", "M20", "WAR", "RNA", "GRN", "M19", "DOM", "RIX", "XLN", "HOU", "AKH", "AER", "KLD", "EMN", "SOI", "OGW", "BFZ", "ORI", "DTK", "FRF", "KTK", "M15", "JOU", "BNG", "THS", "M14", "DGM", "GTC", "RTR", "TRK")
                set_codes_clause = ", ".join(f"'{s}'" for s in pioneer_sets)
                format_clause = (
                    f"AND c.raw_scryfall_data->'legalities'->>'pioneer' = 'legal'"
                    f" AND c.raw_scryfall_data->'legalities'->>'standard' != 'legal'"
                    f" AND UPPER(c.set_code) IN ({set_codes_clause})"
                )
            elif format == "modern":
                # Modern sets (non-Standard/Pioneer, to avoid reprints already in those sections)
                modern_sets = ("fra", "hob", "msh", "sos", "tmt", "ecl", "tla", "spm", "eoe", "fin", "tdm", "dft", "fdn", "dsk", "blb", "acr", "mh3", "otj", "mkm", "lci", "woe", "ltr", "mat", "mom", "one", "bro", "dmu", "snc", "neo", "vow", "mid", "afr", "mh2", "stx", "khm", "znr", "m21", "iko", "thb", "eld", "m20", "mh1", "war", "rna", "grn", "m19", "dom", "rix", "xln", "hou", "akh", "aer", "kld", "emn", "soi", "ogw", "bfz", "ori", "dtk", "frf", "ktk", "m15", "jou", "bng", "ths", "m14", "dgm", "gtc", "rtr", "m13", "avr", "dka", "isd", "m12", "nph", "mbs", "som", "m11", "roe", "wwk", "zen", "m10", "arb", "con", "ala", "eve", "shm", "mor", "lrw", "10e", "fut", "plc", "tsp", "csp", "dis", "gpt", "rav", "9ed", "sok", "8ed", "bok", "chk", "5dn", "dst", "mrd", "trk")
                set_codes_clause = ", ".join(f"'{s}'" for s in modern_sets)
                format_clause = (
                    f"AND c.raw_scryfall_data->'legalities'->>'modern' = 'legal'"
                    f" AND c.raw_scryfall_data->'legalities'->>'standard' != 'legal'"
                    f" AND c.raw_scryfall_data->'legalities'->>'pioneer' != 'legal'"
                    f" AND c.set_code IN ({set_codes_clause})"
                )

        min_pct = 5.0 if tier == "watch" else MIN_PERCENT_CHANGE
        max_pct_clause = "AND ABS((cp.price_now - pp.price_past) / pp.price_past * 100) < 15.0" if tier == "watch" else ""

        query = f"""
        WITH current_prices AS (
            SELECT DISTINCT ON (card_id) card_id, price_eur AS price_now
            FROM fact_card_prices WHERE price_eur IS NOT NULL
            ORDER BY card_id, timestamp DESC
        ),
        past_prices AS (
            SELECT DISTINCT ON (card_id) card_id, price_eur AS price_past
            FROM fact_card_prices
            WHERE price_eur IS NOT NULL AND timestamp <= NOW() - $1::interval
            ORDER BY card_id, timestamp DESC
        ),
        past_3d AS (
            SELECT DISTINCT ON (card_id) card_id, price_eur AS price_3d
            FROM fact_card_prices
            WHERE price_eur IS NOT NULL AND timestamp <= NOW() - INTERVAL '3 days'
            ORDER BY card_id, timestamp DESC
        ),
        past_7d AS (
            SELECT DISTINCT ON (card_id) card_id, price_eur AS price_7d
            FROM fact_card_prices
            WHERE price_eur IS NOT NULL AND timestamp <= NOW() - INTERVAL '7 days'
            ORDER BY card_id, timestamp DESC
        )
        SELECT c.card_id, c.name, c.set_code, c.rarity, cp.price_now, pp.price_past,
               (cp.price_now - pp.price_past) AS abs_change,
               ROUND(((cp.price_now - pp.price_past) / pp.price_past * 100), 2) AS percent_change,
               CASE
                 WHEN p3.price_3d IS NOT NULL AND p7.price_7d IS NOT NULL AND p7.price_7d > 0
                   AND ROUND(((cp.price_now - p3.price_3d) / p3.price_3d * 100), 2) > 0
                   AND ROUND(((cp.price_now - p3.price_3d) / p3.price_3d * 100), 2) > ROUND(((cp.price_now - p7.price_7d) / p7.price_7d * 100), 2) / 2.0
                 THEN true
                 WHEN p3.price_3d IS NOT NULL AND p7.price_7d IS NULL AND p3.price_3d > 0
                   AND ROUND(((cp.price_now - p3.price_3d) / p3.price_3d * 100), 2) > 0
                 THEN true
                 ELSE false
               END AS accelerating
        FROM current_prices cp
        JOIN past_prices pp ON cp.card_id = pp.card_id
        JOIN dim_cards c ON cp.card_id = c.card_id
        LEFT JOIN past_3d p3 ON cp.card_id = p3.card_id
        LEFT JOIN past_7d p7 ON cp.card_id = p7.card_id
        WHERE pp.price_past >= $2
          AND ABS(cp.price_now - pp.price_past) >= $3
          AND ABS((cp.price_now - pp.price_past) / pp.price_past * 100) >= $4
          {max_pct_clause}
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
                min_pct,
                limit
            )
            results = [dict(row) for row in rows]
            return {
                "window_days": days,
                "direction": direction,
                "format": format,
                "tier": tier,
                "cached": False,
                "results": results
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
        query = f"""
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
            p.price_eur,
            p.price_usd_foil,
            p.price_usd,
            p.timestamp AS price_updated_at
        FROM dim_cards c
        LEFT JOIN LATERAL (
            SELECT price_eur, price_usd_foil, price_usd, timestamp
            FROM fact_card_prices fp
            WHERE fp.card_id = c.card_id
            ORDER BY fp.timestamp DESC
            LIMIT 1
        ) p ON true
        WHERE c.name ILIKE '%' || $1 || '%'
        { EXCLUDED_CARD_CLAUSE }
        AND p.price_eur IS NOT NULL
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
                    raw_scryfall_data->>'mana_cost' AS mana_cost,
                    raw_scryfall_data->>'power' AS power,
                    raw_scryfall_data->>'toughness' AS toughness,
                    COALESCE(
                        raw_scryfall_data->'oracle_text',
                        raw_scryfall_data->'card_faces'->0->'oracle_text'
                    ) AS oracle_text,
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

            history_rows = await conn.fetch("""
                SELECT
                    timestamp,
                    vendor,
                    price_eur,
                    price_usd_foil,
                    price_usd
                FROM fact_card_prices
                WHERE card_id = $1
                  AND timestamp >= NOW() - $2::interval
                ORDER BY timestamp ASC;
            """, card_id, timedelta(days=days))

            # Skip foil-only cards: no non-foil EUR price history to track
            if not any(h.get("price_eur") is not None for h in history_rows):
                raise HTTPException(status_code=404, detail=f"Card with ID '{card_id}' is foil-only and not tracked")

            history_list = [dict(r) for r in history_rows]
            valid_eur_prices = [float(h["price_eur"]) for h in history_list if h.get("price_eur") is not None]

            stats = None
            if valid_eur_prices:
                oldest_eur = valid_eur_prices[0]
                current_eur = valid_eur_prices[-1]
                abs_diff = round(current_eur - oldest_eur, 2)
                pct_diff = round((abs_diff / oldest_eur * 100), 2) if oldest_eur > 0 else 0.0
                stats = {
                    "current_price_eur": current_eur,
                    "oldest_price_eur": oldest_eur,
                    "abs_change_eur": abs_diff,
                    "percent_change_eur": pct_diff,
                    "min_price_eur": min(valid_eur_prices),
                    "max_price_eur": max(valid_eur_prices),
                    "data_points": len(valid_eur_prices)
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

