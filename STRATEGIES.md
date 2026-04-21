# Strategies

What we tested, what worked, what didn't. Results from walk-forward
backtests on Polymarket political markets. All numbers are from standalone
scripts in `scripts/` that hit public Polymarket APIs and require no DB.

## Validated winners

### smart-whale (live default)

**Thesis**: a single top-100 whale committing >= $1000 USDC on a liquid
market is strong signal. Most of their alpha comes from research, info
edge, and conviction we can free-ride on.

**Filters**:
- Market: `volume24hr >= $2000` OR total volume `>= $20000`
- Whale: in union of top-100 all-time + top-100 30d profit leaderboards
- Trade notional: `>= $1000`
- Entry price: `[0.10, 0.75]`

**Sizing**: 0.75 Kelly x 25 pct max position, hold to resolution.

**9-month walk-forward, $500 bankroll, politics**:
- n = 36 trades
- win rate 72 percent
- pnl +$1421 (284 pct total ROI)
- Sharpe 4.15
- max drawdown 12.5 percent
- P(+) 100 percent (bootstrap)
- CI95 [+$661, +$2147]
- avg +40.6 pct per active month

**Robustness sweep** across `--whale-min-usdc`:
| threshold | n  | wr  | pnl     | avg mo |
|-----------|----|-----|---------|--------|
| $500      | 54 | 63% | +$1583  | +35.2% |
| $1000     | 36 | 72% | +$1421  | +40.6% |  <- locked default
| $2000     | 26 | 77% | +$1303  | +37.2% |

Edge holds across a 4x parameter sweep, each setting independently hits
the target gate. This is the fingerprint of real effect vs curve-fit.

**Known weakness**: only 4-5 / 9 months have trades. Monthly consistency
is poor even though aggregate is great. Addressed by smart-flow below.

**Run it**: `python scripts/backtest_confluence.py --months 9 --bankroll 500`

**Live module**: `strategy/smart_whale.py` -- proper Strategy subclass
consumed by `execution/order_manager.OrderManager`.

### smart-flow (in active validation)

**Thesis**: same smart-money basis, but instead of firing on one whale's
trade, we track cumulative whale flow on each market. Enter when the
asymmetry between YES and NO whale notional crosses a dominance threshold.
Fires more often -> better monthly consistency.

**Filters + sizing**:
- Market liquidity: `vol24hr >= $1000` OR total `>= $10000`
- Min cumulative whale volume on market: `>= $2000`
- Dominance threshold: `(net_flow_yes - net_flow_no) / total_vol >= 0.5`
  (i.e. 75/25 split)
- Price range: [0.10, 0.75]
- Kelly 0.5, max position 15 pct (safer than smart-whale)

**12-month walk-forward on $500 at `--dominance 0.5 --max-pos 0.15`**:
- n = 35 trades
- win rate 63 percent
- pnl +$2853 (570 pct total ROI)
- Sharpe 3.41
- max drawdown 26.7 percent
- P(+) 100 percent
- CI95 [+$1033, +$4723]
- active months 9/12 (75 pct consistency)
- avg +63.4 pct, worst -27.4 pct, best +145.5 pct per active month

Verdict PASS but worst month -27 pct misses my solid gate (wanted >= -15 pct).
A tighter dominance + smaller max position is running to see if we can
trade 15-20 pct of average per month for a worst month above -15 pct.

**Run it**: `python scripts/backtest_smart_flow.py --months 12 --bankroll 500`

## Tested and dropped

### whale-copy v1

Too loose: followed top-50 whales with $500 min trade on any market.
Initial result buggy (fees wrongly charged at 2 pct) showed negative PnL.
After fee fix + Kelly bump: +30 to +50 pct over 9 months at Kelly 0.5 to
0.75, Sharpe 0.5 to 1.0. Outperformed by smart-whale. Kept as
`strategy/whale_copy.py` for comparison/baseline.

### anomaly-hunter momentum

Follow a >= 15 pct hourly price move. **-$56 over window 6 with 10 pct win
rate**. Massively negative. Move-then-momentum = over-reaction continues
rarely; market usually reverts.

### anomaly-hunter mean-reversion

Fade the same 15 pct move. Quick test (3mo, 200 market cap): +$46 with
50 pct win rate, Sharpe 1.21, P(+)=97 pct -- looked amazing. Widened to
6mo, 500 market cap: -$25 with 26 pct win rate, P(+)=17 pct. Edge evaporated.

Classic data-mining: small sample with biased cap produced illusory
performance. Good reminder that Sharpe on n<20 means nothing.

### anomaly-hunter longshot

Filter to pre-anomaly price < 0.20 that jumped up. Only 1 trade in 6 months.
Not enough signal. Either the threshold is too strict or the pattern
doesn't exist on politics.

### confluence >= 2 whales (strict)

Require 2+ distinct top whales on same side within a 60min window.
Only 4-6 trades over 9 months. 100 pct win rate but n is too small to
trust. Kept as an opt-in `--min-confluence 2` mode on
`scripts/backtest_confluence.py`.

## Strategies built but not backtested (in `strategy/`)

These are in the live pipeline but have no validated walk-forward yet.
The reflection layer monitors them, can disable them if they
underperform, but they're essentially speculative until paper-traded.

### llm_conviction

Gemini 3 Pro reads recent news/social events, matches against active
markets via sentence-transformers embeddings, emits signals with
direction + edge + confidence. Requires `GEMINI_API_KEY`. Costs money
per run (Flash for triage, Pro for analysis, with `caches.create()` on
the stable market-context block).

**Why not backtested**: would require replaying Gemini on 9 months of
historical events -> expensive, and the history isn't in our DB yet.
Paper-trade it for 30 days, then backtest on the accumulated signal set.

### news_arbitrage

Breaking news with price stale by more than the max-staleness window.
Piggybacks on signals produced by llm_conviction with a freshness filter.

**Why not backtested**: depends on llm_conviction signal stream.

## Cost model used throughout

- Polymarket taker fee: 0 pct (most political markets have
  `feesEnabled=False` on gamma). Adjust with `--fee` if you end up on
  a fee-enabled market.
- Slippage: 50 bps (copy-latency / price-moved-between-whale-and-us
  proxy). Adjust with `--slippage` for more conservative runs.
- Gas: $0.05 per trade (Polygon USDC-equivalent).

## Methodology guarantees

- **Walk-forward only**: every test is on held-out months. No peeking
  at future data when setting up filters.
- **Current-leaderboard survivorship bias** is the biggest honest
  caveat. Top whales today are those who won already. Real live edge
  will be lower because in reality we wouldn't know who becomes a top
  whale. Treat all results as an upper bound.
- **Bootstrap 2000 resamples, 95 pct CI** on aggregate PnL. A PASS
  requires the lower bound to be strictly positive or (for risk-adjusted
  PASS) better Sharpe + tail than the anti-whale baseline.
- **Anti-whale baseline** compares against betting the opposite side at
  the same price. Critically this keeps variance for free: whale-copy
  tail risk is ~10x smaller than anti-whale tail risk across all tests.

## Shipping checklist before going live

1. Backtest on 12 months walk-forward -> PASS with consistency >= 60 pct.
2. Paper trade for 30 days with the live stack, same params.
3. Compare paper results to backtest expectations within 20 pct.
4. If matches: flip `MODE=live` in `.env` with real wallet funded.
5. Size: start at 25 pct of planned bankroll for 2 weeks. Scale up only
   if live perf matches paper.
6. Kill switch: `/panic` on Telegram halts everything.
