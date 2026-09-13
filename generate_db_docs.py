#!/usr/bin/env python3
"""
TimescaleDB & PostgreSQL Schema Introspection and Documentation Generator.

Connects to the TimescaleDB database, introspects tables, columns, constraints,
indexes, and TimescaleDB hypertables, and exports clean Markdown documentation.
Also includes offline fallback support from `infrastructure/init.sql` and optional
pdoc invocation for Python codebase documentation.
"""

import os
import sys
import argparse
import asyncio
from datetime import datetime, timezone
from pathlib import Path

# Default DSN matching docker-compose / worker / api
DEFAULT_DSN = os.getenv("DB_DSN", "postgres://mana_user:mana_password@localhost:5432/mana_market")
PROJECT_ROOT = Path(__file__).resolve().parent
DEFAULT_INIT_SQL = PROJECT_ROOT / "infrastructure" / "init.sql"
DEFAULT_OUTPUT_MD = PROJECT_ROOT / "docs" / "DATABASE_SCHEMA.md"


async def introspect_live_db_asyncpg(dsn: str) -> dict:
    """Introspects live PostgreSQL / TimescaleDB database using asyncpg."""
    import asyncpg

    conn = await asyncpg.connect(dsn)
    data = {
        "source": f"Live Database ({dsn.split('@')[-1] if '@' in dsn else dsn})",
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"),
        "hypertables": {},
        "tables": {},
    }

    try:
        # Check TimescaleDB extension & hypertables
        try:
            hypertables_rows = await conn.fetch("""
                SELECT hypertable_name, num_dimensions, num_chunks, compression_enabled
                FROM timescaledb_information.hypertables;
            """)
            dimensions_rows = await conn.fetch("""
                SELECT hypertable_name, column_name, time_interval
                FROM timescaledb_information.dimensions;
            """)
            dim_map = {row["hypertable_name"]: row for row in dimensions_rows}

            for row in hypertables_rows:
                ht_name = row["hypertable_name"]
                dim = dim_map.get(ht_name)
                data["hypertables"][ht_name] = {
                    "num_dimensions": row["num_dimensions"],
                    "num_chunks": row["num_chunks"],
                    "compression_enabled": row["compression_enabled"],
                    "time_column": dim["column_name"] if dim else None,
                    "time_interval": str(dim["time_interval"]) if dim else None,
                }
        except Exception:
            # TimescaleDB information view might not exist if extension isn't loaded
            pass

        # Query all public base tables
        tables_rows = await conn.fetch("""
            SELECT table_name
            FROM information_schema.tables
            WHERE table_schema = 'public' AND table_type = 'BASE TABLE'
            ORDER BY table_name;
        """)

        for tbl in tables_rows:
            table_name = tbl["table_name"]
            # Exclude internal timescale catalog tables
            if table_name.startswith("_timescaledb_") or table_name.startswith("_chunk_"):
                continue

            # Columns
            col_rows = await conn.fetch("""
                SELECT column_name, data_type, udt_name, is_nullable, column_default
                FROM information_schema.columns
                WHERE table_schema = 'public' AND table_name = $1
                ORDER BY ordinal_position;
            """, table_name)

            # Primary Keys
            pk_rows = await conn.fetch("""
                SELECT kcu.column_name
                FROM information_schema.table_constraints tc
                JOIN information_schema.key_column_usage kcu
                  ON tc.constraint_name = kcu.constraint_name
                  AND tc.table_schema = kcu.table_schema
                WHERE tc.constraint_type = 'PRIMARY KEY'
                  AND tc.table_schema = 'public'
                  AND tc.table_name = $1
                ORDER BY kcu.ordinal_position;
            """, table_name)
            pk_cols = [r["column_name"] for r in pk_rows]

            # Foreign Keys
            fk_rows = await conn.fetch("""
                SELECT
                    kcu.column_name,
                    ccu.table_name AS foreign_table,
                    ccu.column_name AS foreign_column
                FROM information_schema.table_constraints AS tc
                JOIN information_schema.key_column_usage AS kcu
                  ON tc.constraint_name = kcu.constraint_name
                  AND tc.table_schema = kcu.table_schema
                JOIN information_schema.constraint_column_usage AS ccu
                  ON ccu.constraint_name = tc.constraint_name
                WHERE tc.constraint_type = 'FOREIGN KEY'
                  AND tc.table_schema = 'public'
                  AND tc.table_name = $1;
            """, table_name)
            fk_map = {r["column_name"]: f"{r['foreign_table']}({r['foreign_column']})" for r in fk_rows}

            # Indexes
            idx_rows = await conn.fetch("""
                SELECT indexname, indexdef
                FROM pg_indexes
                WHERE schemaname = 'public' AND tablename = $1
                ORDER BY indexname;
            """, table_name)

            data["tables"][table_name] = {
                "columns": [
                    {
                        "name": c["column_name"],
                        "type": c["udt_name"] if c["data_type"] == "USER-DEFINED" else c["data_type"],
                        "nullable": c["is_nullable"] == "YES",
                        "default": c["column_default"],
                        "is_pk": c["column_name"] in pk_cols,
                        "fk_ref": fk_map.get(c["column_name"]),
                    }
                    for c in col_rows
                ],
                "indexes": [{"name": i["indexname"], "def": i["indexdef"]} for i in idx_rows],
            }

    finally:
        await conn.close()

    return data


def parse_offline_sql(sql_path: Path) -> dict:
    """Parses init.sql schema statically as fallback when DB is offline."""
    if not sql_path.exists():
        raise FileNotFoundError(f"init.sql not found at {sql_path}")

    data = {
        "source": f"Offline Parsing ({sql_path.name})",
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"),
        "hypertables": {},
        "tables": {},
    }

    # Schema mapping aligned with infrastructure/init.sql
    data["hypertables"]["fact_card_prices"] = {
        "time_column": "timestamp",
        "time_interval": "Default 7-day chunk",
        "compression_enabled": False,
        "num_chunks": "N/A (Offline mode)",
        "num_dimensions": 1,
    }

    data["tables"]["dim_cards"] = {
        "description": "Dimension table storing static catalog metadata for Magic cards.",
        "columns": [
            {"name": "card_id", "type": "uuid", "nullable": False, "default": None, "is_pk": True, "fk_ref": None},
            {"name": "oracle_id", "type": "uuid", "nullable": True, "default": None, "is_pk": False, "fk_ref": None},
            {"name": "name", "type": "text", "nullable": False, "default": None, "is_pk": False, "fk_ref": None},
            {"name": "set_code", "type": "varchar(10)", "nullable": False, "default": None, "is_pk": False, "fk_ref": None},
            {"name": "collector_number", "type": "text", "nullable": False, "default": None, "is_pk": False, "fk_ref": None},
            {"name": "rarity", "type": "text", "nullable": False, "default": None, "is_pk": False, "fk_ref": None},
            {"name": "mana_value", "type": "numeric", "nullable": True, "default": None, "is_pk": False, "fk_ref": None},
            {"name": "colors", "type": "text[]", "nullable": True, "default": None, "is_pk": False, "fk_ref": None},
            {"name": "raw_scryfall_data", "type": "jsonb", "nullable": False, "default": None, "is_pk": False, "fk_ref": None},
            {"name": "created_at", "type": "timestamptz", "nullable": True, "default": "NOW()", "is_pk": False, "fk_ref": None},
        ],
        "indexes": [
            {"name": "dim_cards_pkey", "def": "CREATE UNIQUE INDEX dim_cards_pkey ON dim_cards (card_id)"},
            {"name": "idx_dim_cards_name", "def": "CREATE INDEX idx_dim_cards_name ON dim_cards (name)"},
            {"name": "idx_dim_cards_set", "def": "CREATE INDEX idx_dim_cards_set ON dim_cards (set_code)"},
        ],
    }

    data["tables"]["fact_card_prices"] = {
        "description": "TimescaleDB Hypertable storing time-series financial prices across vendors.",
        "columns": [
            {"name": "timestamp", "type": "timestamptz", "nullable": False, "default": None, "is_pk": True, "fk_ref": None},
            {"name": "card_id", "type": "uuid", "nullable": False, "default": None, "is_pk": True, "fk_ref": "dim_cards(card_id)"},
            {"name": "vendor", "type": "text", "nullable": False, "default": None, "is_pk": True, "fk_ref": None},
            {"name": "price_usd", "type": "numeric", "nullable": True, "default": None, "is_pk": False, "fk_ref": None},
            {"name": "price_usd_foil", "type": "numeric", "nullable": True, "default": None, "is_pk": False, "fk_ref": None},
            {"name": "price_eur", "type": "numeric", "nullable": True, "default": None, "is_pk": False, "fk_ref": None},
        ],
        "indexes": [
            {"name": "fact_card_prices_pkey", "def": "CREATE UNIQUE INDEX fact_card_prices_pkey ON fact_card_prices (timestamp, card_id, vendor)"},
            {"name": "idx_fact_prices_card_time", "def": "CREATE INDEX idx_fact_prices_card_time ON fact_card_prices (card_id, timestamp DESC)"},
        ],
    }

    return data


def format_markdown(schema_data: dict) -> str:
    """Formats introspected schema data into structured GitHub-flavored Markdown."""
    lines = [
        "# Database Schema Documentation",
        "",
        f"> **Introspection Source:** `{schema_data['source']}`  ",
        f"> **Last Generated:** `{schema_data['generated_at']}`",
        "",
        "## Overview",
        "",
        "The **Mana Market Engine** database is hosted on **TimescaleDB (PostgreSQL 16)**. "
        "It employs a hybrid SQL / JSONB data model separating static card metadata from time-series market pricing.",
        "",
        "```mermaid",
        "erDiagram",
        '    dim_cards ||--o{ fact_card_prices : "has price entries"',
        "    dim_cards {",
        "        uuid card_id PK",
        "        uuid oracle_id",
        "        string name",
        "        string set_code",
        "        string collector_number",
        "        string rarity",
        "        numeric mana_value",
        "        text_array colors",
        "        jsonb raw_scryfall_data",
        "        timestamptz created_at",
        "    }",
        "    fact_card_prices {",
        "        timestamptz timestamp PK",
        "        uuid card_id PK,FK",
        "        string vendor PK",
        "        numeric price_usd",
        "        numeric price_usd_foil",
        "        numeric price_eur",
        "    }",
        "```",
        "",
        "---",
        "",
        "## Tables & Hypertables",
        "",
    ]

    for table_name, table_info in schema_data["tables"].items():
        is_hyper = table_name in schema_data.get("hypertables", {})
        hyper_badge = " *(TimescaleDB Hypertable)*" if is_hyper else ""
        lines.append(f"### `{table_name}`{hyper_badge}")
        lines.append("")
        if "description" in table_info:
            lines.append(f"{table_info['description']}")
            lines.append("")

        if is_hyper:
            ht_meta = schema_data["hypertables"][table_name]
            lines.append("> [!NOTE]")
            lines.append(f"> **Partition Dimension:** `{ht_meta.get('time_column')}`  ")
            if ht_meta.get("time_interval"):
                lines.append(f"> **Time Chunk Interval:** `{ht_meta.get('time_interval')}`  ")
            lines.append(f"> **Chunks:** `{ht_meta.get('num_chunks', 'N/A')}` | **Compression:** `{ht_meta.get('compression_enabled', False)}`")
            lines.append("")

        lines.append("#### Columns")
        lines.append("")
        lines.append("| Column | Type | Nullable | Primary Key | References | Default |")
        lines.append("| :--- | :--- | :---: | :---: | :--- | :--- |")

        for col in table_info["columns"]:
            pk_mark = "✅ PK" if col.get("is_pk") else ""
            nullable = "YES" if col.get("nullable") else "NO"
            fk_ref = f"`{col.get('fk_ref')}`" if col.get("fk_ref") else "-"
            default_val = f"`{col.get('default')}`" if col.get("default") is not None else "-"
            lines.append(f"| **{col['name']}** | `{col['type']}` | {nullable} | {pk_mark} | {fk_ref} | {default_val} |")

        lines.append("")
        lines.append("#### Indexes")
        lines.append("")
        if table_info.get("indexes"):
            lines.append("| Index Name | Definition |")
            lines.append("| :--- | :--- |")
            for idx in table_info["indexes"]:
                lines.append(f"| `{idx['name']}` | `{idx['def']}` |")
        else:
            lines.append("*No secondary indexes recorded.*")
        lines.append("")
        lines.append("---")
        lines.append("")

    return "\n".join(lines)


def generate_pdoc(output_dir: Path):
    """Executes pdoc on src/api and src/worker modules in isolated processes."""
    import subprocess

    output_dir.mkdir(parents=True, exist_ok=True)
    api_dir = output_dir / "api"
    worker_dir = output_dir / "worker"

    print(f"[pdoc] Generating API documentation into {api_dir}...")
    api_dir.mkdir(parents=True, exist_ok=True)
    subprocess.run([
        sys.executable, "-m", "pdoc",
        "-o", str(api_dir),
        str(PROJECT_ROOT / "src" / "api" / "main.py"),
    ], check=False)

    print(f"[pdoc] Generating Worker documentation into {worker_dir}...")
    worker_dir.mkdir(parents=True, exist_ok=True)
    subprocess.run([
        sys.executable, "-m", "pdoc",
        "-o", str(worker_dir),
        str(PROJECT_ROOT / "src" / "worker" / "main.py"),
        str(PROJECT_ROOT / "src" / "worker" / "delta_engine.py"),
        str(PROJECT_ROOT / "src" / "worker" / "scryfall_client.py"),
        str(PROJECT_ROOT / "src" / "worker" / "seed_mock_history.py"),
    ], check=False)

    # Generate an index.html pointing to both docs
    index_html = f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <title>Mana Market Engine Documentation</title>
    <style>
        body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; max-width: 800px; margin: 40px auto; padding: 0 20px; line-height: 1.6; color: #24292e; }}
        h1 {{ border-bottom: 1px solid #eaecef; padding-bottom: 0.3em; }}
        ul {{ list-style-type: square; padding-left: 20px; }}
        li {{ margin: 10px 0; }}
        a {{ color: #0366d6; text-decoration: none; font-weight: 500; }}
        a:hover {{ text-decoration: underline; }}
        .badge {{ background: #f1f8ff; border: 1px solid #c8e1ff; padding: 2px 8px; border-radius: 6px; font-size: 0.85em; color: #0366d6; }}
    </style>
</head>
<body>
    <h1>Mana Market Engine Documentation</h1>
    <p>Auto-generated documentation portal for the MTG financial market pipeline.</p>
    <ul>
        <li><a href="../DATABASE_SCHEMA.md">Live Database Schema (Markdown)</a> <span class="badge">TimescaleDB</span></li>
        <li><a href="api/main.html">FastAPI REST API Docs</a> <span class="badge">Uvicorn / Redis</span></li>
        <li><a href="worker/index.html">Batch Ingestion & Delta Worker Docs</a> <span class="badge">ETL Pipeline</span></li>
    </ul>
</body>
</html>
"""
    (output_dir / "index.html").write_text(index_html, encoding="utf-8")
    print(f"[pdoc] Documentation index generated at {output_dir / 'index.html'}")


def main():
    parser = argparse.ArgumentParser(description="Export TimescaleDB schema to Markdown & generate pdoc.")
    parser.add_argument("--dsn", default=DEFAULT_DSN, help="PostgreSQL/TimescaleDB connection string")
    parser.add_argument("--output", default=str(DEFAULT_OUTPUT_MD), help="Target Markdown file path")
    parser.add_argument("--offline", action="store_true", help="Force static parsing of init.sql instead of connecting")
    parser.add_argument("--pdoc", action="store_true", help="Also generate pdoc Python documentation")
    parser.add_argument("--pdoc-dir", default=str(PROJECT_ROOT / "docs" / "code_api"), help="Output directory for pdoc HTML")
    args = parser.parse_args()

    schema_data = None

    if not args.offline:
        try:
            print(f"[DB] Attempting live introspection on: {args.dsn}...")
            schema_data = asyncio.run(introspect_live_db_asyncpg(args.dsn))
            print("[DB] Successfully introspected live database.")
        except Exception as e:
            print(f"[DB] Live connection failed ({e}).")
            print(f"[DB] Falling back to offline schema parsing from '{DEFAULT_INIT_SQL}'...")

    if schema_data is None:
        schema_data = parse_offline_sql(DEFAULT_INIT_SQL)
        print("[DB] Extracted schema from init.sql.")

    markdown_content = format_markdown(schema_data)
    out_path = Path(args.output)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(markdown_content, encoding="utf-8")
    print(f"[Done] Schema documentation written to: {out_path.resolve()}")

    if args.pdoc:
        generate_pdoc(Path(args.pdoc_dir))


if __name__ == "__main__":
    main()
