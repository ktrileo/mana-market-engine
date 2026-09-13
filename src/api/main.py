from fastapi import FastAPI, HTTPException, Query
from contextlib import asynccontextmanager
import asyncpg
import os
from datetime import timedelta

DB_DSN = os.getenv("DB_DSN", "postgres://mana_user:mana_password@localhost:5432/mana_market")

# Financial Noise Filters (matching our delta engine)
MIN_BASE_PRICE = 1.00
MIN_PERCENT_CHANGE = 15.0
MIN_ABSOLUTE_CHANGE = 0.50

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup: Initialize the async connection pool
    app.state.db_pool = await asyncpg.create_pool(DB_DSN, min_size=5, max_size=20)
    print("Database connection pool established.")
    yield
    # Shutdown: Close the pool gracefully
    await app.state.db_pool.close()
    print("Database connection pool closed.")

app = FastAPI(
    title="Mana Market API",
    description="Core financial API for MTG market data",
    version="1.0.0",
    lifespan=lifespan
)

@app.get("/health")
async def health_check():
    return {"status": "healthy", "service": "mana-market-api"}

@app.get("/api/v1/movers")
async def get_market_movers(
    direction: str = Query("gainers", regex="^(gainers|losers)$"),
    days: int = Query(1, ge=1, le=30),
    limit: int = Query(10, ge=1, le=50)
):
    """Fetch the top market gainers or losers over a specific time window."""
    
    # asyncpg uses $1, $2 for parameterized variables, which is safer than %s
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
    
    # Add sorting logic based on direction
    if direction == "gainers":
        query += " AND (cp.price_now - pp.price_past) > 0 ORDER BY percent_change DESC LIMIT $5;"
    else:
        query += " AND (cp.price_now - pp.price_past) < 0 ORDER BY percent_change ASC LIMIT $5;"

    try:
        # Request a connection from the pool and execute
        async with app.state.db_pool.acquire() as conn:
            rows = await conn.fetch(
                query, 
                timedelta(days=days), # $1
                MIN_BASE_PRICE,       # $2
                MIN_ABSOLUTE_CHANGE,  # $3
                MIN_PERCENT_CHANGE,   # $4
                limit                 # $5
            )
            
            return {
                "window_days": days,
                "direction": direction,
                "results": [dict(row) for row in rows]
            }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))