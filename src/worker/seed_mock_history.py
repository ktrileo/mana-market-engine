import psycopg2
from psycopg2.extras import execute_values
from datetime import datetime, timedelta, timezone
import random

DB_DSN = "postgres://mana_user:mana_password@localhost:5432/mana_market"

def seed_history():
    print("Connecting to database to generate mock historical data...")
    conn = psycopg2.connect(DB_DSN)
    cursor = conn.cursor()

    now = datetime.now(timezone.utc)
    yesterday = now - timedelta(days=1)
    seven_days_ago = now - timedelta(days=7)

    # 1. Fetch current price entries
    cursor.execute("""
        SELECT card_id, vendor, price_usd, price_usd_foil, price_eur 
        FROM fact_card_prices 
        WHERE price_usd IS NOT NULL;
    """)
    rows = cursor.fetchall()
    print(f"Found {len(rows)} valid price rows to clone.")

    yesterday_rows = []
    seven_day_rows = []

    for card_id, vendor, usd, usd_foil, eur in rows:
        usd_float = float(usd)
        
        # Randomly introduce price shifts for 10% of cards to simulate market spikes/drops
        if random.random() < 0.10:
            # Shift yesterday's price by -30% to +30%
            yesterday_multiplier = random.uniform(0.70, 1.30)
            # Shift 7-day price by -50% to +50%
            seven_day_multiplier = random.uniform(0.50, 1.50)
            
            p_yesterday = round(usd_float * yesterday_multiplier, 2)
            p_seven_days = round(usd_float * seven_day_multiplier, 2)
        else:
            p_yesterday = usd_float
            p_seven_days = usd_float

        yesterday_rows.append((yesterday, card_id, vendor, p_yesterday, usd_foil, eur))
        seven_day_rows.append((seven_days_ago, card_id, vendor, p_seven_days, usd_foil, eur))

    print("Inserting 24h historical records...")
    execute_values(
        cursor,
        "INSERT INTO fact_card_prices (timestamp, card_id, vendor, price_usd, price_usd_foil, price_eur) VALUES %s ON CONFLICT DO NOTHING;",
        yesterday_rows
    )

    print("Inserting 7d historical records...")
    execute_values(
        cursor,
        "INSERT INTO fact_card_prices (timestamp, card_id, vendor, price_usd, price_usd_foil, price_eur) VALUES %s ON CONFLICT DO NOTHING;",
        seven_day_rows
    )

    conn.commit()
    cursor.close()
    conn.close()
    print("Mock historical data seeded successfully!")

if __name__ == "__main__":
    seed_history()