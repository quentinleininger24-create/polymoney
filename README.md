# polymoney

Autonomous Polymarket betting system. Politics-focused. Single-user.

- Bankroll target: **100 USDC**
- Mode: paper trading first, live when backtests hold up
- Edges: LLM analyst on breaking news, whale copy trading, news arbitrage

## Architecture

```
ingestion/   collectors (Polymarket CLOB, news, Twitter, Reddit, on-chain whales)
signals/     LLM analyst (Claude) + embedding matcher + anomaly detection
strategy/    llm_conviction, whale_copy, news_arbitrage + backtest engine
risk/        Kelly sizing, exposure caps, circuit breakers
execution/   py-clob-client wrapper (paper + live) and order manager loop
bot/         Telegram commands + push alerts
dashboard/   Next.js local UI (localhost:3000)
shared/      config, async SQLAlchemy models, FastAPI app
```

Data flow: ingestion -> Postgres (`events`, `markets`, `price_ticks`, `whale_trades`) -> strategies read -> intents -> risk sizing -> executor -> `bets` + Telegram alert.

## Setup

Prereqs: Python 3.12, Docker, [uv](https://github.com/astral-sh/uv), Node 20+ (and `pnpm` ideally).

```bash
./scripts/setup.sh
```

That creates `.env`, boots Postgres + Redis, creates the schema, installs Python and Node deps.

Fill `.env` with:
- `ANTHROPIC_API_KEY` (required)
- `WALLET_PRIVATE_KEY` + `WALLET_ADDRESS` + Polymarket API creds (only for live mode)
- `TELEGRAM_BOT_TOKEN` + `TELEGRAM_CHAT_ID` (create a bot via @BotFather, grab chat id via @userinfobot)
- `NEWSAPI_KEY` (free tier fine), `TWITTER_BEARER_TOKEN` (optional), `REDDIT_*`
- No key required for whale tracking — uses Polymarket's public data API

Leave `MODE=paper` until backtests are green.

## Running the system

Open 4 shells (or use `tmux` / a process manager):

```bash
# 1) Ingestion loop
make ingest

# 2) Trade loop (paper by default)
python -m execution.order_manager

# 3) Backend API
uvicorn shared.api:app --port 8000 --reload

# 4) Dashboard
make dashboard   # http://localhost:3000
```

Telegram bot separately:
```bash
make bot
```

## Commands (Telegram)

- `/status` - bankroll, cash, open positions, realized PnL
- `/positions` - list open bets
- `/signals` - last 10 signals (even if they didn't trigger)
- `/panic` - trip manual circuit breaker, halt trading
- `/resume` - clear manual breaker

## Going live

1. Backtest for 2 weeks of historical data -> positive Sharpe
2. Paper trade for 30 days -> matches backtest within tolerance
3. Fund wallet with 100 USDC on Polygon
4. Set `MODE=live` in `.env`
5. Restart `execution.order_manager`
6. Watch Telegram like a hawk for the first week

## Risk defaults (edit in `.env`)

| Setting | Default | Meaning |
|---------|---------|---------|
| `MAX_POSITION_PCT` | 0.05 | 5% bankroll per trade max |
| `MAX_EVENT_EXPOSURE_PCT` | 0.20 | 20% bankroll on a single event |
| `KELLY_FRACTION` | 0.33 | 1/3 Kelly (aggressive but not full) |
| `DAILY_DRAWDOWN_STOP_PCT` | 0.15 | pause 24h if down 15% in a day |
| `MIN_EDGE_BPS` | 300 | need 3%+ edge to bet |
| `MIN_CONFIDENCE` | 0.65 | and confidence 0.65+ |

## What to build next

Ordered by expected ROI:
1. Implement the real backtest loop in `strategy/backtest.py`
2. pgvector embeddings in `events` table (faster matching at scale)
3. Debate real-time analyzer (Whisper -> Claude during political debates)
4. Narrative tracker (topic modeling over Twitter/Reddit streams)
5. Auto-hedge engine (correlations across linked markets)

## Directory layout

See `pyproject.toml` for packages. Every Python module is importable as `from <pkg> import ...` with `uv pip install -e .`.
