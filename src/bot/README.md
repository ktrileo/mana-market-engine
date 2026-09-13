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

## 4. Interactions Endpoint URL (Cloudflare Quick Tunnel)

Because `infrastructure/docker-compose.yml` runs a free Cloudflare Quick Tunnel (`trycloudflare.com`):

1. On your Debian host, check the tunnel logs to find your public URL:
   ```bash
   docker logs mana_market_tunnel 2>&1 | grep -o 'https://.*\.trycloudflare\.com' | head -n 1
   ```
   *(Or simply run `docker compose logs tunnel` to view the quick tunnel banner)*.

2. Go to the [Discord Developer Portal](https://discord.com/developers/applications) $\rightarrow$ Your App $\rightarrow$ **General Information**.

3. In the **Interactions Endpoint URL** field, paste your URL followed by `/api/v1/discord/interactions`:
   ```text
   https://<your-subdomain>.trycloudflare.com/api/v1/discord/interactions
   ```

4. Click **Save Changes**. Discord will immediately dispatch an Ed25519 `PING` request. The API verifies the cryptographic signature and returns `{"type": 1}` (`PONG`), activating your Discord bot!
