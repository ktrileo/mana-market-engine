from fastapi import FastAPI, HTTPException, Query, Response
from contextlib import asynccontextmanager
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
CACHE_TTL_SECONDS = 3600  # 1 hour cache

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