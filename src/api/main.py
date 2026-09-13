from fastapi import FastAPI, HTTPException, Path, Query, Response
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

@app.get("/health")
async def health_check():
    return {"status": "healthy", "service": "mana-market-api"}

@app.get("/api/v1/movers")
async def get_market_movers(
    direction: str = Query("gainers", pattern="^(gainers|losers)$"),
    days: int = Query(1, ge=1, le=30),
    limit: int = Query(10, ge=1, le=50)
):
    """Fetch the top market gainers or losers, leveraging Redis cache."""
    
    cache_key = f"api:movers:{direction}:d{days}:l{limit}"
    
    try:
        # 1. Check Redis for a cached response
        cached_data = await app.state.redis.get(cache_key)
        if cached_data:
            return Response(content=cached_data, media_type="application/json")
            
        # 2. Cache Miss: Execute TimescaleDB Query
        query = """
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
        SELECT c.name, c.set_code, c.rarity, cp.price_now, pp.price_past,
               (cp.price_now - pp.price_past) AS abs_change,
               ROUND(((cp.price_now - pp.price_past) / pp.price_past * 100), 2) AS percent_change
        FROM current_prices cp
        JOIN past_prices pp ON cp.card_id = pp.card_id
        JOIN dim_cards c ON cp.card_id = c.card_id
        WHERE pp.price_past >= $2
          AND ABS(cp.price_now - pp.price_past) >= $3
          AND ABS((cp.price_now - pp.price_past) / pp.price_past * 100) >= $4
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
            
            # Format and serialize results
            response_payload = {
                "window_days": days,
                "direction": direction,
                "cached": False,
                "results": [dict(row) for row in rows]
            }
            
            # default=str handles asyncpg Decimal conversions natively
            json_response = json.dumps(response_payload, default=str)
            
            # 3. Store in Redis with TTL
            await app.state.redis.set(cache_key, json_response, ex=CACHE_TTL_SECONDS)
            
            return Response(content=json_response, media_type="application/json")
            
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/v1/cards/search")
async def search_cards(
    q: str = Query(..., min_length=1, max_length=100, description="Card name search query"),
    limit: int = Query(10, ge=1, le=50, description="Maximum number of results to return")
):
    """Search for cards by name with current market prices and artwork URIs."""
    clean_query = q.strip()
    cache_key = f"api:cards:search:{clean_query.lower()}:l{limit}"
    
    try:
        # 1. Check Redis cache
        cached_data = await app.state.redis.get(cache_key)
        if cached_data:
            return Response(content=cached_data, media_type="application/json")
            
        # 2. Query TimescaleDB / PostgreSQL
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
            
            response_payload = {
                "query": clean_query,
                "count": len(rows),
                "cached": False,
                "results": [dict(row) for row in rows]
            }
            
            json_response = json.dumps(response_payload, default=str)
            await app.state.redis.set(cache_key, json_response, ex=CACHE_TTL_SECONDS)
            return Response(content=json_response, media_type="application/json")
            
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/v1/cards/{card_id}/history")
async def get_card_history(
    card_id: UUID = Path(..., description="Unique card ID (UUID)"),
    days: int = Query(30, ge=1, le=365, description="Lookback window in days")
):
    """Retrieve historical daily price points and financial summary stats for a card."""
    cache_key = f"api:cards:{card_id}:history:d{days}"
    
    try:
        # 1. Check Redis cache
        cached_data = await app.state.redis.get(cache_key)
        if cached_data:
            return Response(content=cached_data, media_type="application/json")
            
        async with app.state.db_pool.acquire() as conn:
            # 2. Fetch card metadata
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
                
            # 3. Fetch price history series
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
            
            # 4. Compute financial performance metrics over the requested window
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
                
            response_payload = {
                "card": dict(card_row),
                "window_days": days,
                "stats": stats,
                "cached": False,
                "history": history_list
            }
            
            json_response = json.dumps(response_payload, default=str)
            await app.state.redis.set(cache_key, json_response, ex=CACHE_TTL_SECONDS)
            return Response(content=json_response, media_type="application/json")
            
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))