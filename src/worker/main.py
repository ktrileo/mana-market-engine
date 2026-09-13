import os
import urllib.request
import gzip
import json
import psycopg2
from psycopg2.extras import execute_values
from datetime import datetime, timezone

# Homelab Database Connection String (matching docker-compose.yml)
DB_DSN = os.getenv("DB_DSN", "postgres://mana_user:mana_password@localhost:5432/mana_market")
REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0")

HEADERS = {
    "User-Agent": "ManaMarketEngine/1.0",
    "Accept": "application/json;q=0.9,*/*;q=0.8"
}

def flush_to_db(cursor, cards_batch, prices_batch):
    """Executes bulk batch inserts to PostgreSQL."""
    
    # Insert new card metadata (if any exist)
    if cards_batch:
        execute_values(cursor, """
            INSERT INTO dim_cards (card_id, oracle_id, name, set_code, collector_number, rarity, mana_value, colors, raw_scryfall_data)
            VALUES %s 
            ON CONFLICT (card_id) DO NOTHING
        """, cards_batch, template="(%s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb)")
    
    # Insert price data into the TimescaleDB hypertable
    if prices_batch:
        execute_values(cursor, """
            INSERT INTO fact_card_prices (timestamp, card_id, vendor, price_usd, price_usd_foil, price_eur)
            VALUES %s 
            ON CONFLICT (timestamp, card_id, vendor) DO NOTHING
        """, prices_batch)

def invalidate_movers_cache():
    """Invalidates cached movers in Redis so API serves fresh data for the new day."""
    print("6. Invalidating Redis movers cache...")
    try:
        import redis
        r = redis.from_url(REDIS_URL, decode_responses=True)
        cursor = 0
        keys_deleted = 0
        while True:
            cursor, keys = r.scan(cursor=cursor, match="api:movers:*", count=100)
            if keys:
                keys_deleted += r.delete(*keys)
            if cursor == 0:
                break
        print(f"   -> Deleted {keys_deleted} cached mover key(s).")
    except Exception as e:
        print(f"   -> Warning: Could not invalidate Redis cache ({e})")

def run_ingestion():
    print("1. Connecting to database...")
    conn = psycopg2.connect(DB_DSN)
    cursor = conn.cursor()

    # Cache Warmup: Get existing card IDs so we don't overwrite metadata unnecessarily
    print("2. Loading existing cards into cache...")
    cursor.execute("SELECT card_id::text FROM dim_cards;")
    existing_cards = {row[0] for row in cursor.fetchall()}
    print(f"   -> Found {len(existing_cards)} existing cards in DB.")

    # Setup Scryfall stream
    print("3. Fetching Scryfall bulk data URL...")
    meta_req = urllib.request.Request("https://api.scryfall.com/bulk-data/default-cards", headers=HEADERS)
    meta_data = json.load(urllib.request.urlopen(meta_req))
    download_url = meta_data.get("jsonl_download_uri") or meta_data.get("download_uri")
    
    print(f"4. Streaming and processing cards from Scryfall...")
    stream_response = urllib.request.urlopen(urllib.request.Request(download_url, headers=HEADERS))
    
    timestamp_now = datetime.now(timezone.utc)
    
    cards_to_insert = []
    prices_to_insert = []
    cards_processed = 0

    with gzip.GzipFile(fileobj=stream_response) as gz:
        for line in gz:
            card = json.loads(line)
            card_id = card.get("id")
            
            # EXTRACT & BRANCH: Metadata
            if card_id not in existing_cards:
                cards_to_insert.append((
                    card_id,
                    card.get("oracle_id"),
                    card.get("name", "Unknown"),
                    card.get("set", "unknown"),
                    card.get("collector_number", "0"),
                    card.get("rarity", "common"),
                    card.get("cmc", 0.0),
                    card.get("colors", []),
                    json.dumps(card) # Dumps raw object for JSONB column
                ))
                existing_cards.add(card_id) # Add to RAM cache immediately
            
            # EXTRACT & BRANCH: Prices
            prices = card.get("prices", {})
            prices_to_insert.append((
                timestamp_now,
                card_id,
                "scryfall_aggregate",
                prices.get("usd"),
                prices.get("usd_foil"),
                prices.get("eur")
            ))

            cards_processed += 1
            
            # FLUSH BATCH: Execute writes every 5,000 cards to prevent memory bloat
            if len(prices_to_insert) >= 5000:
                flush_to_db(cursor, cards_to_insert, prices_to_insert)
                conn.commit()  # Commit transaction
                cards_to_insert.clear()
                prices_to_insert.clear()
                print(f"   -> Processed {cards_processed} cards...")
    
    # FLUSH REMAINDER
    if prices_to_insert:
        flush_to_db(cursor, cards_to_insert, prices_to_insert)
        conn.commit()
        print(f"   -> Processed {cards_processed} cards total.")

    cursor.close()
    conn.close()
    print("5. Daily ingestion complete!")

    # Synchronize cache: Invalidate cached mover keys so API serves fresh data
    invalidate_movers_cache()

if __name__ == "__main__":
    run_ingestion()