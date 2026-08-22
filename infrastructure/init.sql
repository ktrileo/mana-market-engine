-- Enable TimescaleDB extension
CREATE EXTENSION IF NOT EXISTS timescaledb CASCADE;

-- 1. Static Card Catalog (Dimension Table)
CREATE TABLE IF NOT EXISTS dim_cards (
    card_id UUID PRIMARY KEY,
    oracle_id UUID,
    name TEXT NOT NULL,
    set_code VARCHAR(10) NOT NULL,
    collector_number TEXT NOT NULL,
    rarity TEXT NOT NULL,
    mana_value NUMERIC,
    colors TEXT[],
    raw_scryfall_data JSONB NOT NULL,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

-- Indexes for fast metadata lookups
CREATE INDEX IF NOT EXISTS idx_dim_cards_name ON dim_cards(name);
CREATE INDEX IF NOT EXISTS idx_dim_cards_set ON dim_cards(set_code);

-- 2. Time-Series Price History (Fact Table)
-- Note: In TimescaleDB, primary keys on hypertables MUST include the partition time column.
CREATE TABLE IF NOT EXISTS fact_card_prices (
    timestamp TIMESTAMPTZ NOT NULL,
    card_id UUID NOT NULL REFERENCES dim_cards(card_id),
    vendor TEXT NOT NULL,
    price_usd NUMERIC,
    price_usd_foil NUMERIC,
    price_eur NUMERIC,
    PRIMARY KEY (timestamp, card_id, vendor)
);

-- Convert fact_card_prices into a TimescaleDB Hypertable partitioned by time
SELECT create_hypertable('fact_card_prices', 'timestamp', if_not_exists => TRUE);

-- Index for retrieving price history of a specific card ordered by time
CREATE INDEX IF NOT EXISTS idx_fact_prices_card_time ON fact_card_prices (card_id, timestamp DESC);