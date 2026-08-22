import psycopg2
import json

DB_DSN = "postgres://mana_user:mana_password@localhost:5432/mana_market"

# Financial Noise Filters
MIN_BASE_PRICE = 1.00
MIN_PERCENT_CHANGE = 15.0
MIN_ABSOLUTE_CHANGE = 0.50

def get_market_movers(days_lookback=1, limit=10):
    conn = psycopg2.connect(DB_DSN)
    cursor = conn.cursor()

    # Query uses SQL subqueries to fetch the latest price and the price N days ago per card
    query = """
    WITH current_prices AS (
        SELECT DISTINCT ON (card_id) 
            card_id, 
            price_usd AS price_now, 
            timestamp AS time_now
        FROM fact_card_prices
        WHERE price_usd IS NOT NULL
        ORDER BY card_id, timestamp DESC
    ),
    past_prices AS (
        SELECT DISTINCT ON (card_id) 
            card_id, 
            price_usd AS price_past, 
            timestamp AS time_past
        FROM fact_card_prices
        WHERE price_usd IS NOT NULL 
          AND timestamp <= NOW() - (%s || ' days')::INTERVAL
        ORDER BY card_id, timestamp DESC
    )
    SELECT 
        c.name,
        c.set_code,
        c.rarity,
        cp.price_now,
        pp.price_past,
        (cp.price_now - pp.price_past) AS abs_change,
        ROUND(((cp.price_now - pp.price_past) / pp.price_past * 100), 2) AS percent_change
    FROM current_prices cp
    JOIN past_prices pp ON cp.card_id = pp.card_id
    JOIN dim_cards c ON cp.card_id = c.card_id
    WHERE pp.price_past >= %s
      AND ABS(cp.price_now - pp.price_past) >= %s
      AND ABS((cp.price_now - pp.price_past) / pp.price_past * 100) >= %s
    ORDER BY percent_change DESC;
    """

    cursor.execute(query, (days_lookback, MIN_BASE_PRICE, MIN_ABSOLUTE_CHANGE, MIN_PERCENT_CHANGE))
    results = cursor.fetchall()

    cursor.close()
    conn.close()

    # Format into Gainers and Losers lists
    gainers = [
        {
            "name": row[0], "set": row[1], "rarity": row[2],
            "price_now": float(row[3]), "price_past": float(row[4]),
            "abs_change": float(row[5]), "percent_change": float(row[6])
        }
        for row in results if row[6] > 0
    ][:limit]

    losers = sorted([
        {
            "name": row[0], "set": row[1], "rarity": row[2],
            "price_now": float(row[3]), "price_past": float(row[4]),
            "abs_change": float(row[5]), "percent_change": float(row[6])
        }
        for row in results if row[6] < 0
    ], key=lambda x: x["percent_change"])[:limit]

    return {"gainers": gainers, "losers": losers}

if __name__ == "__main__":
    print("\n--- 24-HOUR TOP MOVERS ---")
    movers_24h = get_market_movers(days_lookback=1, limit=5)
    
    print("\n[ TOP GAINERS ]")
    for g in movers_24h["gainers"]:
        print(f"{g['name']} ({g['set'].upper()}): ${g['price_past']} -> ${g['price_now']} (+{g['percent_change']}%)")

    print("\n[ TOP LOSERS ]")
    for l in movers_24h["losers"]:
        print(f"{l['name']} ({l['set'].upper()}): ${l['price_past']} -> ${l['price_now']} ({l['percent_change']}%)")