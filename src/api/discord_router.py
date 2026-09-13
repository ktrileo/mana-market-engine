"""
Discord HTTP Interactions Endpoint & Command Dispatcher.

Handles Discord slash command webhooks via HTTP POST using Ed25519 signature verification.
Provides responses for:
  - Type 1: PING -> PONG handshake
  - Type 2: APPLICATION_COMMAND -> /price, /movers
  - Type 4: APPLICATION_COMMAND_AUTOCOMPLETE -> real-time card search suggestions
"""

import os
import json
from datetime import timedelta
from fastapi import APIRouter, Request, Response, Header
from nacl.signing import VerifyKey
from nacl.exceptions import BadSignatureError

router = APIRouter(prefix="/api/v1/discord", tags=["Discord Interactions"])

DISCORD_PUBLIC_KEY = os.getenv("DISCORD_PUBLIC_KEY", "")

# Discord Interaction & Callback Types
INTERACTION_TYPE_PING = 1
INTERACTION_TYPE_APPLICATION_COMMAND = 2
INTERACTION_TYPE_MESSAGE_COMPONENT = 3
INTERACTION_TYPE_AUTOCOMPLETE = 4

CALLBACK_TYPE_PONG = 1
CALLBACK_TYPE_CHANNEL_MESSAGE = 4
CALLBACK_TYPE_DEFERRED_CHANNEL_MESSAGE = 5
CALLBACK_TYPE_AUTOCOMPLETE_RESULT = 8


def verify_discord_signature(body: bytes, signature: str | None, timestamp: str | None) -> bool:
    """Verifies Ed25519 signature sent by Discord servers."""
    if not DISCORD_PUBLIC_KEY:
        print("[Discord Security] WARNING: DISCORD_PUBLIC_KEY is not configured.")
        return False
    if not signature or not timestamp:
        return False

    try:
        verify_key = VerifyKey(bytes.fromhex(DISCORD_PUBLIC_KEY))
        verify_key.verify(timestamp.encode() + body, bytes.fromhex(signature))
        return True
    except (BadSignatureError, Exception) as e:
        print(f"[Discord Security] Signature verification failed: {e}")
        return False


def build_price_embed(card: dict) -> dict:
    """Constructs a rich Discord Embed for the /price slash command."""
    usd = f"${float(card['price_usd']):.2f}" if card.get("price_usd") else "N/A"
    usd_foil = f"${float(card['price_usd_foil']):.2f}" if card.get("price_usd_foil") else "N/A"
    eur = f"€{float(card['price_eur']):.2f}" if card.get("price_eur") else "N/A"
    
    set_code = card.get("set_code", "???").upper()
    collector_num = card.get("collector_number", "")
    rarity = card.get("rarity", "common").capitalize()
    
    fields = [
        {
            "name": "💵 Market (USD)",
            "value": usd,
            "inline": True
        },
        {
            "name": "✨ Foil (USD)",
            "value": usd_foil,
            "inline": True
        },
        {
            "name": "💶 Market (EUR)",
            "value": eur,
            "inline": True
        }
    ]
    
    embed = {
        "title": card["name"],
        "url": card.get("scryfall_uri") or f"https://scryfall.com/search?q={card['name']}",
        "description": f"**Set:** {set_code} (#{collector_num}) • **Rarity:** {rarity}",
        "color": 0x5865F2,  # Discord Blurple
        "fields": fields,
        "footer": {
            "text": "Mana Market Engine • Prices updated daily"
        }
    }
    
    # Attach artwork if available
    image_uri = card.get("image_uri") or card.get("image_uri_small")
    if image_uri:
        embed["thumbnail"] = {"url": image_uri}
        
    return embed


def build_movers_embed(direction: str, days: int, rows: list) -> dict:
    """Constructs a Discord Embed displaying top market gainers or losers."""
    is_gainers = direction == "gainers"
    color = 0x2ECC71 if is_gainers else 0xE74C3C  # Green vs Red
    icon = "📈" if is_gainers else "📉"
    title_dir = "Gainers" if is_gainers else "Losers"
    
    lines = []
    for i, row in enumerate(rows, 1):
        pct = float(row["percent_change"])
        pct_str = f"+{pct:.1f}%" if pct > 0 else f"{pct:.1f}%"
        sign = "+" if float(row["abs_change"]) > 0 else ""
        abs_str = f"{sign}${float(row['abs_change']):.2f}"
        
        name = row["name"]
        set_code = row["set_code"].upper()
        p_now = f"${float(row['price_now']):.2f}"
        
        lines.append(f"`{i}.` **{name}** ({set_code}) $\\rightarrow$ **{p_now}** ({pct_str} / {abs_str})")
        
    description = "\n".join(lines) if lines else "*No cards qualified under financial noise filters.*"
    
    return {
        "title": f"{icon} Top MTG Market {title_dir} (Last {days} Day{'s' if days > 1 else ''})",
        "description": description,
        "color": color,
        "footer": {
            "text": f"Showing top {len(rows)} movers • Mana Market Engine"
        }
    }


@router.post("/interactions")
async def discord_interactions(
    request: Request,
    x_signature_ed25519: str | None = Header(None),
    x_signature_timestamp: str | None = Header(None)
):
    """
    Main webhook entrypoint for Discord HTTP Interactions.
    Verifies Ed25519 signature before dispatching commands.
    """
    raw_body = await request.body()

    # 1. Cryptographic Signature Verification
    if not verify_discord_signature(raw_body, x_signature_ed25519, x_signature_timestamp):
        return Response(content="Invalid request signature", status_code=401)

    try:
        interaction = json.loads(raw_body.decode("utf-8"))
    except Exception:
        return Response(content="Invalid JSON body", status_code=400)

    interaction_type = interaction.get("type")

    # 2. Type 1: Discord PING / Healthcheck (Required for endpoint registration in Dev Portal)
    if interaction_type == INTERACTION_TYPE_PING:
        return Response(
            content=json.dumps({"type": CALLBACK_TYPE_PONG}),
            media_type="application/json"
        )

    # 3. Type 4: Real-time Autocomplete (Card Search Suggestions)
    if interaction_type == INTERACTION_TYPE_AUTOCOMPLETE:
        data = interaction.get("data", {})
        options = data.get("options", [])
        
        focused_val = ""
        for opt in options:
            if opt.get("focused"):
                focused_val = str(opt.get("value", "")).strip()
                break
                
        choices = []
        if focused_val and len(focused_val) >= 2:
            async with request.app.state.db_pool.acquire() as conn:
                rows = await conn.fetch("""
                    SELECT DISTINCT name
                    FROM dim_cards
                    WHERE name ILIKE '%' || $1 || '%'
                    ORDER BY
                        CASE
                            WHEN LOWER(name) = LOWER($1) THEN 0
                            WHEN LOWER(name) LIKE LOWER($1) || '%' THEN 1
                            ELSE 2
                        END,
                        name ASC
                    LIMIT 10;
                """, focused_val)
                choices = [{"name": r["name"][:100], "value": r["name"][:100]} for r in rows]

        return Response(
            content=json.dumps({
                "type": CALLBACK_TYPE_AUTOCOMPLETE_RESULT,
                "data": {"choices": choices}
            }),
            media_type="application/json"
        )

    # 4. Type 2: Application Command (Slash Commands)
    if interaction_type == INTERACTION_TYPE_APPLICATION_COMMAND:
        data = interaction.get("data", {})
        command_name = data.get("name")
        options = {opt["name"]: opt.get("value") for opt in data.get("options", [])}

        # Handle /price command
        if command_name == "price":
            card_query = options.get("card", "").strip()
            if not card_query:
                return Response(
                    content=json.dumps({
                        "type": CALLBACK_TYPE_CHANNEL_MESSAGE,
                        "data": {"content": "⚠️ Please specify a card name."}
                    }),
                    media_type="application/json"
                )

            async with request.app.state.db_pool.acquire() as conn:
                # Search for the best match
                row = await conn.fetchrow("""
                    SELECT
                        c.card_id,
                        c.name,
                        c.set_code,
                        c.collector_number,
                        c.rarity,
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
                        p.price_eur
                    FROM dim_cards c
                    LEFT JOIN LATERAL (
                        SELECT price_usd, price_usd_foil, price_eur
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
                        END
                    LIMIT 1;
                """, card_query)

                if not row:
                    return Response(
                        content=json.dumps({
                            "type": CALLBACK_TYPE_CHANNEL_MESSAGE,
                            "data": {"content": f"🔍 No card found matching **'{card_query}'**."}
                        }),
                        media_type="application/json"
                    )

                embed = build_price_embed(dict(row))
                
                # Interactive button linking directly to Scryfall
                components = []
                scryfall_url = row["scryfall_uri"]
                if scryfall_url:
                    components = [
                        {
                            "type": 1,  # Action Row
                            "components": [
                                {
                                    "type": 2,  # Button
                                    "style": 5,  # Link Button
                                    "label": "View on Scryfall",
                                    "url": scryfall_url
                                }
                            ]
                        }
                    ]

                return Response(
                    content=json.dumps({
                        "type": CALLBACK_TYPE_CHANNEL_MESSAGE,
                        "data": {
                            "embeds": [embed],
                            "components": components
                        }
                    }),
                    media_type="application/json"
                )

        # Handle /movers command
        if command_name == "movers":
            direction = str(options.get("direction", "gainers")).lower()
            days = int(options.get("days", 1))
            limit = min(max(int(options.get("limit", 5)), 1), 25)

            # Query movers
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
            WHERE pp.price_past >= 1.00
              AND ABS(cp.price_now - pp.price_past) >= 0.50
              AND ABS((cp.price_now - pp.price_past) / pp.price_past * 100) >= 15.0
            """
            if direction == "gainers":
                query += " AND (cp.price_now - pp.price_past) > 0 ORDER BY percent_change DESC LIMIT $2;"
            else:
                query += " AND (cp.price_now - pp.price_past) < 0 ORDER BY percent_change ASC LIMIT $2;"

            async with request.app.state.db_pool.acquire() as conn:
                rows = await conn.fetch(query, timedelta(days=days), limit)
                embed = build_movers_embed(direction, days, [dict(r) for r in rows])

                return Response(
                    content=json.dumps({
                        "type": CALLBACK_TYPE_CHANNEL_MESSAGE,
                        "data": {"embeds": [embed]}
                    }),
                    media_type="application/json"
                )

    # Fallback for unhandled interaction types
    return Response(
        content=json.dumps({
            "type": CALLBACK_TYPE_CHANNEL_MESSAGE,
            "data": {"content": "Command received."}
        }),
        media_type="application/json"
    )
