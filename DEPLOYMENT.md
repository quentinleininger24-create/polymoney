# Deployment — Hetzner CX22 (€4.59/month)

Fastest path from zero to paper-trading on a VPS. You'll need about an
hour total, most of it filling in API keys and waiting for Hetzner to
provision.

## 1. Create the Hetzner server (10 min)

1. Sign up: https://accounts.hetzner.com/signUp (EU cloud, 4.59 EUR/mo).
2. Console -> Cloud -> `+ New Project` -> call it `polymoney`.
3. `+ Add Server`:
   - Location: **Falkenstein** or **Nuremberg** (Germany) — low latency to Polygon nodes.
   - Image: **Ubuntu 24.04**
   - Type: **Shared vCPU -> CX22** (2 vCPU, 4 GB RAM, 40 GB disk, 4.59 EUR/mo)
   - SSH keys: upload your public key (on your PC, `type %USERPROFILE%\.ssh\id_ed25519.pub`
     in PowerShell; if you don't have one, generate it: `ssh-keygen -t ed25519`).
   - Name: `polymoney-01`
   - Click **Create & Buy Now**.
4. Wait 30 seconds. Note the public IPv4 from the server page.

## 2. Bootstrap the VPS (5 min)

On your PC:
```bash
ssh root@<your-vps-ip>
```

Once in, run:
```bash
curl -sSL https://raw.githubusercontent.com/quentinleininger24-create/polymoney/main/scripts/deploy-vps.sh | bash
```

That installs Docker, firewall, fail2ban, clones the repo to
`/opt/polymoney`, and prints the next steps.

## 3. Fill in .env (20 min)

```bash
cd /opt/polymoney
nano .env
```

Required:
- `GEMINI_API_KEY` — https://ai.google.dev (free, 50M tokens/day)
- `TELEGRAM_BOT_TOKEN` — talk to @BotFather on Telegram, /newbot
- `TELEGRAM_CHAT_ID` — talk to @userinfobot on Telegram
- `NEWSAPI_KEY` — https://newsapi.org (free, 100 req/day) *(optional)*

Leave `MODE=paper` for the first 30 days.

For LIVE mode (later):
- `WALLET_PRIVATE_KEY` — a **fresh** Polygon wallet's private key (NOT your personal one)
- `WALLET_ADDRESS` — matching address
- `POLYMARKET_API_KEY` / `POLYMARKET_API_SECRET` / `POLYMARKET_API_PASSPHRASE` —
  from polymarket.com UI (use a VPN if French IP is blocked)

Save (Ctrl-O, Enter, Ctrl-X).

## 4. Start the stack (2 min)

```bash
docker compose -f docker-compose.prod.yml up -d --build
```

First build takes ~3 minutes (downloads Python image, installs deps).
After that, all services come up and restart automatically on VPS reboot.

Check logs:
```bash
docker compose -f docker-compose.prod.yml logs -f order_manager
docker compose -f docker-compose.prod.yml logs -f ingestion
docker compose -f docker-compose.prod.yml logs -f bot
```

Ctrl-C stops following logs (services keep running).

## 5. Test on Telegram

Open your Telegram bot, send:
- `/status` — should respond with bankroll and positions
- `/signals` — last 10 signals detected
- `/reflect` — reflection state (empty at first)

If the bot doesn't respond, check its logs:
```bash
docker compose -f docker-compose.prod.yml logs --tail 50 bot
```

## 6. Daily routine (2 min/day)

From your phone (Telegram):
- `/status` each morning
- `/panic` if anything looks weird
- `/resume` when ready to restart

From your PC (occasionally):
```bash
ssh root@<vps-ip>
cd /opt/polymoney
docker compose -f docker-compose.prod.yml ps          # all healthy?
docker compose -f docker-compose.prod.yml logs --tail 100 order_manager
```

## 7. Upgrade to LIVE (after 30 days of good paper results)

1. Fund your dedicated Polygon wallet with USDC (~500 USDC to start).
2. SSH in, `nano /opt/polymoney/.env`, change `MODE=live`.
3. Restart order manager:
   ```bash
   docker compose -f docker-compose.prod.yml restart order_manager
   ```
4. Watch Telegram like a hawk for the first week.

## Viewing the web dashboard

Dashboard API listens on localhost:8000 of the VPS. To access from your PC:
```bash
ssh -L 8000:localhost:8000 root@<vps-ip>
# leave this terminal open, open http://localhost:8000/docs in your browser
```

Or skip the dashboard entirely — Telegram is enough for running.

## Troubleshooting

**Services won't start**: check `.env` is complete. `docker compose logs migrate`
will show DB connection errors.

**Bot offline**: `TELEGRAM_BOT_TOKEN` or `TELEGRAM_CHAT_ID` wrong. Re-check
via @BotFather and @userinfobot.

**No signals after a few hours**: `NEWSAPI_KEY` missing or wrong? Political
markets also need `DUNE_API_KEY` removed (we use the public Polymarket
leaderboard instead, so this should just work).

**VPS ran out of memory**: unlikely on CX22 (4 GB), but if so upgrade to
CX32 (8 GB, ~8 EUR/mo).

**Want to update to latest code**:
```bash
cd /opt/polymoney
git pull
docker compose -f docker-compose.prod.yml up -d --build
```

## Kill switch

Full stop:
```bash
docker compose -f docker-compose.prod.yml down
```

Pause trading but keep ingestion running:
- Telegram: `/panic` (trips manual circuit breaker)
- Later: `/resume`

Emergency wallet recovery: your funds are on Polygon, controlled by
the private key in `.env`. If the VPS is compromised or you lose SSH
access, import that key into MetaMask to recover.
