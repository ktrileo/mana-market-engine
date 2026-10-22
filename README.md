# Mana Market Engine

An automated Magic: The Gathering (MTG) financial market engine and daily report dashboard. Instead of tracking cards individually, the system streams the entire market daily from Scryfall, applies financial noise filters to calculate true price movers, and presents findings through a high-performance REST API and a lightweight daily report website.

---

## Architecture Overview

```
                      ┌────────────────────────────┐
                      │   Scryfall Bulk Data API   │
                      └─────────────┬──────────────┘
                                    │ Gzip JSONL stream
                                    ▼
                      ┌────────────────────────────┐
                      │      Ingestion Worker      │
                      │    (src/worker/main.py)    │
                      └──────┬──────────────┬──────┘
                             │ Inserts      │ Invalidates cache
                             ▼              ▼
┌──────────────────┐  ┌─────────────┐  ┌───────────┐
│ Browser / LAN    │  │ TimescaleDB │  │   Redis   │
│ Dashboard & API  │─▶│ (Postgres)  │◀─┤   Cache   │
│ (:8000)          │  └──────▲──────┘  └─────▲─────┘
└────────┬─────────┘         │               │
         │                   │ Pool Query    │ Cache Get / Set
         └───────────────────┴───────────────┴─────┘
                     FastAPI Core & Embedded Web
                         (src/api/main.py)
```

* **Storage Layer**: Hosted on **TimescaleDB (PostgreSQL 16)**. Separates static card metadata (`dim_cards`) from time-series price data (`fact_card_prices` hypertable).
* **Caching Layer**: **Redis** caches high-frequency mover queries, card searches, and price history series (synchronized with daily batch ingestion).
* **Ingestion Worker**: Streams compressed JSONL line-by-line with near-zero memory footprint, performing bulk batch inserts every 5,000 cards.
* **FastAPI Backend & Dashboard**: Asynchronous REST service powered by `asyncpg` and `redis.asyncio`, serving both API endpoints and an embedded, zero-overhead daily report web dashboard.

---

## Features

* **Daily Market Report Dashboard**: Modern, dark-mode single-page dashboard accessible at `http://<server-ip>:8000/`.
  * **Granular Format Ticker**: Filter gainers and losers across **Standard** (Wilds of Eldraine $\rightarrow$ Present), **Pioneer** (Return to Ravnica $\rightarrow$ Present), and **Modern** (Mirrodin $\rightarrow$ Present).
  * **3-Format Comparison Matrix**: Side-by-side view comparing top movers across Standard, Pioneer, and Modern in one glance.
  * **Spikes & Crashes**: Toggle between top gainers and top losers.
  * **Time Windows**: Filter price shifts across 24 hours, 7 days, or 30 days.
  * **Card Inspector**: Interactive 30-day price trend line charts (Chart.js) with format legality tags and Scryfall artwork previews.
  * **Live Search**: Instant debounced card search.
  * **Print-Ready**: Dedicated print view for export or morning paper briefing.
* **Financial Noise Filtering**: Eliminates penny-stock bulk-bin volatility by enforcing:
  * Minimum base price: **$1.00**
  * Minimum absolute shift: **$0.50**
  * Minimum relative shift: **15.0%**
* **Containerized Deployment**: Simple 3-container persistent stack (`timescaledb`, `redis`, `api`) plus a `batch` profile worker.
* **Self-Hosted CI/CD**: Automatic deployment on push to `main` via a self-hosted GitHub Actions runner on Debian.

---

## REST API Reference

| Endpoint | Method | Description |
| :--- | :---: | :--- |
| `/` | `GET` | Embedded Daily Report web dashboard |
| `/health` | `GET` | Container health probe |
| `/api/v1/movers` | `GET` | Top market gainers or losers by format (`format=all\|standard\|pioneer\|modern`, `direction=gainers\|losers`, `days=1..30`, `limit=1..50`) |
| `/api/v1/cards/search` | `GET` | Fuzzy card search with current market prices and artwork URIs (`q=...`, `limit=1..50`) |
| `/api/v1/cards/{id}/history` | `GET` | Historical daily price points and summary stats (`days=1..365`) |

---

## Project Structure

```text
.
├── .github/
│   └── workflows/
│       └── deploy.yml          # Self-hosted Debian runner CD workflow
├── infrastructure/
│   ├── .env.example            # Environment variables template
│   ├── docker-compose.yml      # Multi-container orchestration
│   └── init.sql                # TimescaleDB schema (dim_cards + fact hypertable)
├── src/
│   ├── api/
│   │   ├── static/             # Lightweight web dashboard
│   │   │   ├── index.html      # Tailwind CSS layout
│   │   │   ├── app.js          # Alpine.js state & Chart.js logic
│   │   │   └── favicon.svg
│   │   ├── Dockerfile
│   │   ├── requirements.txt    # Direct API dependencies
│   │   └── main.py             # FastAPI endpoints & static mount
│   └── worker/
│       ├── Dockerfile
│       ├── requirements.txt    # Worker dependencies (psycopg2, redis)
│       └── main.py             # Scryfall bulk stream ETL & cache invalidator
└── README.md
```

---

## Getting Started

### 1. Environment Configuration

Copy the environment template:
```bash
cp infrastructure/.env.example infrastructure/.env
```

### 2. Start Services

Launch the database, Redis cache, and FastAPI core:
```bash
cd infrastructure
docker compose up -d
```

Access the web dashboard in your browser:
```text
http://localhost:8000/
```

### 3. Run Ingestion Worker

Trigger a manual data ingestion batch:
```bash
docker compose run --rm worker
```
*(In production, this is triggered daily at 08:00 UTC via a systemd timer on the Debian host).*
