# Discord Interaction Bot Setup Guide

This project implements an **HTTP-based Discord Interaction Bot**. It does not use a long-running Gateway WebSocket connection; instead, Discord forwards slash command payloads via HTTPS POST requests directly to our FastAPI backend.

---

## 1. Discord Developer Portal Setup

1. Go to the [Discord Developer Portal](https://discord.com/developers/applications).
2. Click **New Application** and name it (e.g., `Mana Market Engine`).
3. Under **General Information**, note your credentials:
   * **Application ID** (Client ID)
   * **Public Key** (Ed25519 Hex string)
4. Under the **Bot** tab:
   * Click **Reset Token** to generate your **Bot Token** (save this securely).
5. Under **OAuth2** $\rightarrow$ **URL Generator**:
   * Scopes: Select `bot` and `applications.commands`.
   * Bot Permissions: `Send Messages`, `Embed Links`, `Read Message History`.
   * Copy the generated invite link and open it in a browser to invite the bot to your Discord server.

---

## 2. Environment Configuration

Add the following environment variables to your `.env` file (or host environment on Debian):

```env
# Discord Credentials
DISCORD_APPLICATION_ID="your_application_id_here"
DISCORD_PUBLIC_KEY="your_64_char_hex_public_key_here"
DISCORD_BOT_TOKEN="your_bot_token_here"
```

In `infrastructure/docker-compose.yml`, the `api` service consumes `DISCORD_PUBLIC_KEY` to cryptographically verify incoming Discord webhooks.

---

## 3. Registering Slash Commands

Run the CLI command registration script to sync the slash commands (`/price` and `/movers`) with Discord:

```bash
# Register globally (available on all servers within ~5 minutes)
python src/bot/register_commands.py --app-id <APP_ID> --token <BOT_TOKEN>

# Or register to a specific test server for instant propagation
python src/bot/register_commands.py --app-id <APP_ID> --token <BOT_TOKEN> --guild-id <SERVER_GUILD_ID>

# List currently registered commands
python src/bot/register_commands.py --app-id <APP_ID> --token <BOT_TOKEN> --list
```

---

## 4. Interactions Endpoint URL

Once your Cloudflare Tunnel is running and pointing to your API:
1. Go to [Discord Developer Portal](https://discord.com/developers/applications) $\rightarrow$ Your App $\rightarrow$ **General Information**.
2. In the **Interactions Endpoint URL** field, enter:
   ```text
   https://market.yourdomain.com/api/v1/discord/interactions
   ```
3. Click **Save Changes**. Discord will immediately send a cryptographic `PING` request. Our FastAPI server validates the Ed25519 signature and returns `{"type": 1}` (`PONG`), verifying the URL.
