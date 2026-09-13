#!/usr/bin/env python3
"""
Discord Slash Command Registration Utility.

Registers the /price and /movers application commands with the Discord REST API.
Supports both Global registration (all servers) and Guild-specific registration (instant updates for development).

Usage:
  python src/bot/register_commands.py
  python src/bot/register_commands.py --app-id <APP_ID> --token <BOT_TOKEN>
  python src/bot/register_commands.py --guild-id <GUILD_ID>
  python src/bot/register_commands.py --list
  python src/bot/register_commands.py --delete-all
"""

import os
import sys
import json
import argparse
import urllib.request
import urllib.error

DISCORD_API_VERSION = "v10"
BASE_URL = f"https://discord.com/api/{DISCORD_API_VERSION}"

# Slash Command Specifications (Discord Application Command Schema)
COMMANDS = [
    {
        "name": "price",
        "description": "Check current market prices and artwork for an MTG card",
        "type": 1,  # CHAT_INPUT
        "options": [
            {
                "name": "card",
                "description": "Name of the card (autocomplete supported)",
                "type": 3,  # STRING
                "required": True,
                "autocomplete": True
            }
        ]
    },
    {
        "name": "movers",
        "description": "View MTG market top gainers or losers",
        "type": 1,  # CHAT_INPUT
        "options": [
            {
                "name": "direction",
                "description": "Trend direction (gainers or losers)",
                "type": 3,  # STRING
                "required": False,
                "choices": [
                    {"name": "Gainers (📈 Spikes)", "value": "gainers"},
                    {"name": "Losers (📉 Drops)", "value": "losers"}
                ]
            },
            {
                "name": "days",
                "description": "Lookback time window",
                "type": 4,  # INTEGER
                "required": False,
                "choices": [
                    {"name": "24 Hours", "value": 1},
                    {"name": "7 Days", "value": 7},
                    {"name": "30 Days", "value": 30}
                ]
            },
            {
                "name": "limit",
                "description": "Number of cards to return (1-25, default 5)",
                "type": 4,  # INTEGER
                "required": False,
                "min_value": 1,
                "max_value": 25
            }
        ]
    }
]


def make_discord_request(endpoint: str, token: str, method: str = "GET", data: list | dict | None = None):
    """Makes an authenticated HTTP request to Discord REST API."""
    url = f"{BASE_URL}{endpoint}"
    headers = {
        "Authorization": f"Bot {token}",
        "Content-Type": "application/json",
        "User-Agent": "ManaMarketEngine/1.0"
    }

    body = json.dumps(data).encode("utf-8") if data is not None else None
    req = urllib.request.Request(url, data=body, headers=headers, method=method)

    try:
        with urllib.request.urlopen(req) as resp:
            resp_body = resp.read().decode("utf-8")
            return json.loads(resp_body) if resp_body else {}
    except urllib.error.HTTPError as e:
        err_msg = e.read().decode("utf-8")
        print(f"[Error] Discord API returned HTTP {e.code}: {err_msg}", file=sys.stderr)
        sys.exit(1)
    except Exception as e:
        print(f"[Error] Network request failed: {e}", file=sys.stderr)
        sys.exit(1)


def main():
    parser = argparse.ArgumentParser(description="Register Discord Application Slash Commands")
    parser.add_argument("--app-id", default=os.getenv("DISCORD_APPLICATION_ID") or os.getenv("DISCORD_APP_ID"),
                        help="Discord Application ID (Client ID)")
    parser.add_argument("--token", default=os.getenv("DISCORD_BOT_TOKEN"),
                        help="Discord Bot Token")
    parser.add_argument("--guild-id", default=os.getenv("DISCORD_GUILD_ID"),
                        help="Optional Guild (Server) ID for instantaneous testing")
    parser.add_argument("--list", action="store_true", help="List currently registered commands")
    parser.add_argument("--delete-all", action="store_true", help="Delete all registered commands")
    args = parser.parse_args()

    app_id = args.app_id
    token = args.token
    guild_id = args.guild_id

    # Interactive prompt if variables are missing
    if not app_id:
        app_id = input("Enter Discord Application ID (from Developer Portal): ").strip()
    if not token:
        token = input("Enter Discord Bot Token: ").strip()

    if not app_id or not token:
        print("[Error] Both Application ID and Bot Token are required.", file=sys.stderr)
        sys.exit(1)

    # Determine command endpoint scope (Guild-specific or Global)
    if guild_id:
        endpoint = f"/applications/{app_id}/guilds/{guild_id}/commands"
        scope_str = f"Guild ({guild_id}) [Instant propagation]"
    else:
        endpoint = f"/applications/{app_id}/commands"
        scope_str = "Global [Propagates across all servers within ~1-5 minutes]"

    print(f"[Info] Target Scope: {scope_str}")

    # 1. Action: List Commands
    if args.list:
        print("[Info] Fetching currently registered commands...")
        res = make_discord_request(endpoint, token, method="GET")
        if not res:
            print("No commands currently registered.")
        for cmd in res:
            print(f"  - /{cmd['name']} (ID: {cmd['id']}): {cmd.get('description')}")
        return

    # 2. Action: Delete All Commands
    if args.delete_all:
        confirm = input(f"Are you sure you want to delete all commands in {scope_str}? (y/N): ")
        if confirm.lower() == "y":
            print("[Info] Overwriting with empty command list...")
            make_discord_request(endpoint, token, method="PUT", data=[])
            print("[Success] All commands deleted.")
        return

    # 3. Action: Register / Bulk Overwrite Commands
    print(f"[Info] Syncing {len(COMMANDS)} commands to Discord ({scope_str})...")
    for cmd in COMMANDS:
        print(f"  -> /{cmd['name']}: {cmd['description']}")

    res = make_discord_request(endpoint, token, method="PUT", data=COMMANDS)
    print("\n[Success] Slash commands registered successfully!")
    print("Registered Commands:")
    for cmd in res:
        print(f"  ✅ /{cmd['name']} (Command ID: {cmd['id']})")


if __name__ == "__main__":
    main()
