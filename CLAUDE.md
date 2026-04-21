# CLAUDE.md

Guidance for Claude sessions working on this repo. Read this before touching code.

## What this is

Autonomous Polymarket betting system. Single-user (quentin). Politics focus.
Goal: validated strategies that deliver >=10 percent per month average with
acceptable drawdown, running hands-off with Telegram control.

Live status: scaffold complete, smart-whale strategy validated on 9-month
walk-forward (+40 percent per active month, Sharpe 4.15). Not yet deployed.
`.env` not yet populated. Docker stack not yet launched locally.

## Architecture (top-down)

```
ingestion/    API collectors: Polymarket gamma/CLOB, NewsAPI+GDELT,
              Twitter, Reddit, whale leaderboard + trades, prices-history
signals/      LLM analyst (Gemini 3 Pro + Flash preview, prompt-cached)
              + embedding-match + anomaly detection
strategy/     llm_conviction, whale_copy, news_arbitrage, backtest engine
risk/         Kelly sizing, exposure caps, circuit breakers
reflection/   self-correction: drawdown trigger + source/strategy scoring
              + confluence gate + retrospective + adapter
execution/    py-clob-client wrapper (paper and live modes) + order manager
bot/          Telegram commands + push alerts
dashboard/    Next.js 15 local UI (localhost:3000) + FastAPI backend
shared/       config (pydantic-settings), async SQLAlchemy models, db session
scripts/      standalone backtests (no DB needed), setup.sh
```

Data flow: ingestion -> Postgres (markets, events, signals, bets,
whale_trades, price_ticks, source_scores, reflection_runs) -> strategies
read -> intents -> risk sizing -> order manager -> executor (paper/live)
-> bets update -> reflection checks health each tick.

## Stack

- Python 3.12 (3.13 works too), async SQLAlchemy 2.0, Alembic
- Postgres + TimescaleDB + Redis via `docker compose`
- Gemini 3 (`google-genai` SDK) with explicit `client.aio.caches.create()`
- `py-clob-client` for Polymarket execution
- Next.js 15 + Tailwind + SWR (dashboard)
- `python-telegram-bot` for the control bot
- `uv` for Python dep management (not poetry, not pip-tools)

## Validated strategy: smart-whale

Locked defaults in `scripts/backtest_confluence.py`:
  --min-confluence 1  --whale-min-usdc 1000  --min-vol-24h 2000
  --min-total-vol 20000  --kelly 0.75  --max-pos 0.25
  --min-price 0.10  --max-price 0.75

9-month walk-forward, $500 bankroll, politics:
  n=36, wr 72 percent, pnl +$1421, Sharpe 4.15, max DD 12.5 percent,
  P(+) 100 percent, avg +40.6 percent per active month.

Robustness sweep (whale-min-usdc 500/1000/2000) all hit >=10 percent
per month target. Edge holds across a 4x parameter range -- this is the
signature of a real effect, not curve-fitting.

Known weakness: only 4-5/9 months have trades (concentration risk).
Active exploration (`scripts/backtest_smart_flow.py`) to fix this via
cumulative flow dominance instead of single-trigger entries.

## Running backtests

Standalone, no DB needed, just stdlib:

```bash
# Smart-whale (locked winner)
python scripts/backtest_confluence.py --months 9 --bankroll 500

# Whale-copy baseline
python scripts/backtest_whale_copy.py --months 9

# Anomaly hunter (research artifact, did not pan out)
python scripts/backtest_anomaly_hunter.py --months 6

# Smart-flow (in-progress replacement for smart-whale)
python scripts/backtest_smart_flow.py --months 12
```

All scripts hit public Polymarket APIs (gamma-api, clob, lb-api, data-api),
no keys needed.

## Running the live system

Requires Docker + uv + Node 20+ installed. See README.md for setup.sh.
Stack components that must all run:

```
make up                                   # postgres + redis
make ingest                               # ingestion scheduler (cron)
python -m execution.order_manager         # 60s tick trade loop
uvicorn shared.api:app --port 8000        # dashboard API
make dashboard                            # Next.js on :3000
make bot                                  # Telegram control bot
```

## Conventions

- No comments unless the WHY is non-obvious. Well-named identifiers do
  the explaining. No PR/ticket references, no "added for X flow".
- No trailing session summaries. No emojis in code or docs.
- Prefer editing existing files over creating new ones. README is
  explicit in scope -- do not spawn new docs unless asked.
- Paper mode is the default. Live requires `MODE=live` in `.env` AND a
  validated backtest on the strategy being deployed.
- Every strategy must implement `strategy.base.Strategy`. Risk sizing
  goes through `risk.position_sizing.size_intent` only. Do not bypass.
- New DB fields go in `shared/models.py` + alembic revision. Alembic
  auto-generate works since env.py imports `Base.metadata`.

## Gotchas / non-obvious Polymarket behavior

- Gamma `/markets` endpoint mis-tags crypto markets as "politics". Use
  `/events?tag_slug=us-politics|elections|trump` + flatten `markets[]`.
- `outcomes` and `outcomePrices` and `clobTokenIds` come back as
  JSON-ENCODED STRINGS, not arrays. Parse them before use. See
  `ingestion/polymarket._parse_tokens`.
- CLOB `maker_base_fee` and `taker_base_fee` are in BASIS POINTS
  (1000 = 10 percent). Most active political markets have
  `feesEnabled=False` on gamma = 0 fees, but check per market.
- `data-api /trades` supports `offset` pagination. `before`/`after` time
  filters do NOT work as documented. Paginate then filter in code.
- Polymarket `lb-api /profit` windows: `1d`, `7d`, `30d`, `all` only.
  Capitalized variants (`Day`, `Month`) return 400.
- `clob/prices-history` needs a `token_id`, not a `conditionId`.
  Parameters: `interval=max|1d|1h|1m`, `fidelity=<minutes>`.
- Markets with `outcomePrices=["1","0"]` or `["0","1"]` are resolved.
  With prices close to 0.5 they are still open or resolved 50/50.

## Reflection layer mental model

Every order-manager tick:
1. Recompute strategy scores (win rate, Sharpe, streak).
2. Check drawdown triggers (5 consecutive losses OR rolling wr below 35
   percent on last 20 OR 7d drawdown above 15 percent).
3. If triggered: halt (reflection_active breaker), diagnose last 14 days
   (which sources pointed at the winning side vs losing side), adapt
   (disable losing strats, boost accurate-and-early sources, penalize
   misleading ones), backtest before vs after, resume only if BOTH
   Sharpe AND PnL improved.
4. Confluence gate on every intent: need >=1 distinct source type
   supporting direction (>=2 when stressed post-reflection).

Source scoring is fed hourly by `reflection/scoring_loop.score_newly_resolved`
which scores every signal emitted on a just-resolved market by correctness
and lead time vs the decisive price move.

## What NOT to do

- Do not bump Kelly above 0.75 without re-running the robustness sweep.
- Do not add new strategies without a backtest first. Paper-only is fine
  to ship, but it has to pass either `backtest.replay()` or the
  standalone scripts' validation gate.
- Do not commit `.env`, `wallet.json`, anything in `secrets/`, or
  `.claude/` runtime files. Gitignore covers these; double-check.
- Do not create documentation files (README, CHANGELOG, guides) without
  being asked. README is the single user-facing doc.
- Do not switch LLM providers without the user's say-so. Gemini 3 was
  picked explicitly -- Anthropic swap out is in history but not default.

## Repo state shortcuts

- Commit history tells the story: `git log --oneline` in reverse chrono.
- Key milestones: initial scaffold (8beae33), reflection layer (a162a0f),
  Gemini swap (39bf01d), smart-whale locked (6ea3784).
- Active branch: `main`. No feature branches -- direct commits.
- `.env.example` has every variable with sensible defaults.
- Tests directory exists but is empty. Fine for now given validation
  happens via standalone backtest scripts.
