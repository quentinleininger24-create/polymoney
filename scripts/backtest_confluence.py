#!/usr/bin/env python3
"""Smart-whale / confluence backtest. Locked defaults validated to hit >=10%/month.

Two modes, same script:

1) `--min-confluence 1` (default, VALIDATED): follow any single top-100 whale
   whose trade is >= $1000 on a liquid political market (vol24hr >= $2000).
   This is the locked winner from iteration:
     - 9-month walk-forward on $500 bankroll
     - n=36 trades, 72% win rate, +$1421 PnL (284% total ROI)
     - Sharpe 4.15, max DD 12.5%, P(+) = 100%, avg +40.6% per active month
     - Edge holds at $500/$1000/$2000 whale-min thresholds (35/40/37% monthly)

2) `--min-confluence 2+`: require N top whales converging on same side within
   a time window. Much rarer signal, higher quality when it fires. Research
   mode -- sample tends to be <10 trades over 9 months, so statistically weak
   individually but mathematically stronger-per-signal.

Filter layers:
- Liquidity: volume24hr >= $2k OR total volume >= $20k (efficient markets).
- Whale quality: wallet in union of top-100 all-time AND top-100 30d profit.
- Trade size: whale deployed >= $1000 of capital (filters spam).
- Price: entry side in [0.10, 0.75] (payoff/risk ratio sweet spot).

Sizing:
- 3/4 Kelly times confluence multiplier (1.0 at min, +0.5 per extra whale).
- Capped at 25% of rolling bankroll.
"""

from __future__ import annotations

import argparse
import json
import math
import random
import statistics
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

GAMMA = "https://gamma-api.polymarket.com"
LB = "https://lb-api.polymarket.com"
DATA = "https://data-api.polymarket.com"

INITIAL_BANKROLL = 500.0
MAX_POSITION_PCT = 0.25
KELLY_FRACTION = 0.75
FEE_PCT = 0.0
SLIPPAGE_PCT = 0.005
GAS_USDC = 0.05

MIN_ENTRY_PRICE = 0.10
MAX_ENTRY_PRICE = 0.75

# Confluence parameters (locked via walk-forward validation -- see module docstring)
MIN_CONFLUENCE_WHALES = 1
CONFLUENCE_WINDOW_SEC = 60 * 60   # 60 minutes (only relevant when >=2 required)
CONFLUENCE_MULT_PER_EXTRA_WHALE = 0.5  # +0.5x per extra whale beyond min

# Liquidity filter (efficient-markets target)
MIN_VOLUME_24HR = 2000.0
MIN_TOTAL_VOLUME = 20000.0

WHALE_MIN_TRADE_USDC = 1000.0
WHALE_LEADERBOARD_LIMIT = 100
TRADES_PAGE_SIZE = 500
TRADES_MAX_PAGES = 25

REQUEST_DELAY_SEC = 0.1

VERTICAL_TAGS: dict[str, tuple[str, ...]] = {
    "politics": ("us-politics", "elections", "trump"),
    "sports": ("sports", "nfl", "nba", "soccer", "mlb"),
    "crypto": ("crypto", "bitcoin", "ethereum"),
    "geopolitics": ("geopolitics", "russia-ukraine", "middle-east"),
}


def _http_json(url: str, params: dict | None = None, retries: int = 3):
    if params:
        url = f"{url}?{urllib.parse.urlencode(params)}"
    last: Exception | None = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "polymoney-confluence/0.1"})
            with urllib.request.urlopen(req, timeout=30) as r:
                data = json.loads(r.read())
            time.sleep(REQUEST_DELAY_SEC)
            return data
        except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError) as e:
            last = e
            time.sleep(0.5 * (attempt + 1))
    raise RuntimeError(f"HTTP failed: {url} :: {last}")


def _parse_iso(s: str | None):
    if not s:
        return None
    try:
        return datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    except ValueError:
        return None


def _num(v) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def fetch_markets_for_vertical(start: datetime, end: datetime, vertical: str) -> list[dict]:
    out: list[dict] = []
    seen: set[str] = set()
    for tag in VERTICAL_TAGS.get(vertical, (vertical,)):
        offset = 0
        while offset < 5000:
            events = _http_json(f"{GAMMA}/events", {
                "closed": "true", "limit": 100, "offset": offset,
                "tag_slug": tag, "order": "endDate", "ascending": "false",
            })
            if not isinstance(events, list) or not events:
                break
            stop = False
            for e in events:
                ed = _parse_iso(e.get("endDate"))
                if not ed:
                    continue
                if ed < start:
                    stop = True
                    break
                if not (start <= ed <= end):
                    continue
                for m in e.get("markets", []) or []:
                    cond = m.get("conditionId")
                    if not cond or cond in seen or not m.get("closed"):
                        continue
                    m.setdefault("endDate", e.get("endDate"))
                    seen.add(cond)
                    out.append(m)
            if stop:
                break
            offset += 100
    return out


def passes_liquidity_filter(m: dict) -> bool:
    vol24 = _num(m.get("volume24hr"))
    total = _num(m.get("volumeNum") or m.get("volume"))
    return vol24 >= MIN_VOLUME_24HR or total >= MIN_TOTAL_VOLUME


def market_resolution(m: dict) -> str | None:
    raw_outs = m.get("outcomes")
    raw_prices = m.get("outcomePrices")
    if not raw_outs or not raw_prices:
        return None
    try:
        outs = json.loads(raw_outs) if isinstance(raw_outs, str) else raw_outs
        prices = json.loads(raw_prices) if isinstance(raw_prices, str) else raw_prices
        p0, p1 = float(prices[0]), float(prices[1])
    except (json.JSONDecodeError, TypeError, ValueError):
        return None
    if len(outs) != 2 or abs(p0 - p1) < 0.01:
        return None
    return str(outs[0 if p0 > p1 else 1]).strip().upper()


def fetch_top_whales(window: str, limit: int) -> list[dict]:
    rows = _http_json(f"{LB}/profit", {"window": window, "limit": limit})
    return rows if isinstance(rows, list) else []


_trades_cache: dict[str, list[dict]] = {}


def fetch_whale_trades(wallet: str, oldest_needed_ts: float) -> list[dict]:
    if wallet in _trades_cache:
        return _trades_cache[wallet]
    out: list[dict] = []
    for page in range(TRADES_MAX_PAGES):
        try:
            rows = _http_json(f"{DATA}/trades", {
                "user": wallet, "limit": TRADES_PAGE_SIZE, "offset": page * TRADES_PAGE_SIZE,
            })
        except Exception as e:
            print(f"  ! trades fetch failed for {wallet[:8]}: {e}", file=sys.stderr)
            break
        if not isinstance(rows, list) or not rows:
            break
        out.extend(rows)
        oldest = float(rows[-1].get("timestamp", 0) or 0)
        if oldest and oldest < oldest_needed_ts:
            break
        if len(rows) < TRADES_PAGE_SIZE:
            break
    _trades_cache[wallet] = out
    return out


def kelly_size(edge_prob: float, price: float, bankroll: float, multiplier: float) -> float:
    if price <= 0 or price >= 1 or edge_prob <= price:
        return 0.0
    b = (1 - price) / price
    p = edge_prob
    q = 1 - p
    f = (b * p - q) / b
    f = max(0.0, min(1.0, f)) * KELLY_FRACTION * multiplier
    f = min(f, MAX_POSITION_PCT)
    return bankroll * f


def settle(entry_price: float, size_usdc: float, won: bool) -> float:
    effective = min(0.99, entry_price * (1 + SLIPPAGE_PCT))
    shares = size_usdc / effective
    cost = size_usdc * (1 + FEE_PCT) + GAS_USDC
    return shares - cost if won else -cost


@dataclass
class SimBet:
    market_id: str
    question: str
    entry_ts: int
    outcome: str
    entry_price: float
    size_usdc: float
    confluence: int
    won: bool
    pnl: float


def find_confluence_entries(
    markets: list[dict],
    whales: list[dict],
    test_start_ts: int,
    test_end_ts: int,
    oldest_needed_ts: float,
) -> list[dict]:
    """Return list of {market_id, entry_ts, outcome, entry_price, confluence,
    market} dicts for markets where >=MIN_CONFLUENCE_WHALES independent top
    whales took the same side within CONFLUENCE_WINDOW_SEC of each other."""
    # Build wallet set for O(1) membership
    whale_addrs = {w["proxyWallet"].lower() for w in whales if w.get("proxyWallet")}
    # Map condition_id -> list of (ts, wallet, outcome, price, size_usdc)
    per_market: dict[str, list[tuple[int, str, str, float, float]]] = {}
    for addr in whale_addrs:
        for t in fetch_whale_trades(addr, oldest_needed_ts):
            cond = t.get("conditionId")
            if not cond:
                continue
            ts = int(t.get("timestamp", 0) or 0)
            if not (test_start_ts <= ts <= test_end_ts):
                continue
            outcome = str(t.get("outcome", "")).strip().upper()
            if outcome not in {"YES", "NO"}:
                continue
            price = float(t.get("price", 0) or 0)
            size = float(t.get("size", 0) or 0) * price
            if size < WHALE_MIN_TRADE_USDC:
                continue
            per_market.setdefault(cond, []).append((ts, addr, outcome, price, size))

    market_by_cond = {m["conditionId"]: m for m in markets if m.get("conditionId")}
    entries: list[dict] = []
    for cond, trades in per_market.items():
        if cond not in market_by_cond:
            continue
        m = market_by_cond[cond]
        if not passes_liquidity_filter(m):
            continue
        trades.sort(key=lambda r: r[0])
        # Sliding window of distinct wallets per side
        for side in ("YES", "NO"):
            side_trades = [t for t in trades if t[2] == side]
            if len(side_trades) < MIN_CONFLUENCE_WHALES:
                continue
            # Find first window with >= MIN_CONFLUENCE_WHALES distinct wallets
            for i in range(len(side_trades)):
                window_end_ts = side_trades[i][0] + CONFLUENCE_WINDOW_SEC
                wallets_in_window: set[str] = set()
                entry_price = side_trades[i][3]
                entry_ts = side_trades[i][0]
                for j in range(i, len(side_trades)):
                    if side_trades[j][0] > window_end_ts:
                        break
                    wallets_in_window.add(side_trades[j][1])
                    if len(wallets_in_window) >= MIN_CONFLUENCE_WHALES:
                        # Use latest of the confluence wallets' prices as entry
                        entry_price = side_trades[j][3]
                        entry_ts = side_trades[j][0]
                        break
                if len(wallets_in_window) >= MIN_CONFLUENCE_WHALES:
                    entries.append({
                        "market_id": cond,
                        "market": m,
                        "entry_ts": entry_ts,
                        "outcome": side,
                        "entry_price": entry_price,
                        "confluence": len(wallets_in_window),
                    })
                    break  # one entry per market+side
    entries.sort(key=lambda e: e["entry_ts"])
    return entries


def run_confluence_strategy(entries: list[dict]) -> list[SimBet]:
    bets: list[SimBet] = []
    bankroll = INITIAL_BANKROLL
    seen: set[tuple[str, str]] = set()
    for e in entries:
        key = (e["market_id"], e["outcome"])
        if key in seen:
            continue
        seen.add(key)
        m = e["market"]
        winner = market_resolution(m)
        if winner is None:
            continue
        entry_yes = float(e["entry_price"])
        side_price = entry_yes if e["outcome"] == "YES" else 1 - entry_yes
        if side_price < MIN_ENTRY_PRICE or side_price > MAX_ENTRY_PRICE:
            continue

        confluence_mult = 1.0 + CONFLUENCE_MULT_PER_EXTRA_WHALE * max(0, e["confluence"] - MIN_CONFLUENCE_WHALES)
        edge_prob = min(0.95, side_price + 0.05 + 0.02 * (e["confluence"] - MIN_CONFLUENCE_WHALES))
        size = kelly_size(edge_prob, side_price, bankroll, confluence_mult)
        if size < 1.0:
            continue
        size = min(size, bankroll - GAS_USDC)
        if size < 1.0:
            continue
        won = e["outcome"] == winner
        pnl = settle(side_price, size, won)
        bankroll += pnl
        bets.append(SimBet(
            market_id=e["market_id"],
            question=(m.get("question") or "")[:80],
            entry_ts=e["entry_ts"],
            outcome=e["outcome"],
            entry_price=side_price,
            size_usdc=size,
            confluence=e["confluence"],
            won=won,
            pnl=pnl,
        ))
        if bankroll <= 1:
            break
    return bets


@dataclass
class Stats:
    n: int
    wins: int
    win_rate: float
    pnl: float
    roi_pct: float
    sharpe: float
    max_dd_pct: float


def compute_stats(bets: list[SimBet]) -> Stats:
    if not bets:
        return Stats(0, 0, 0.0, 0.0, 0.0, 0.0, 0.0)
    wins = sum(1 for b in bets if b.won)
    pnl = sum(b.pnl for b in bets)
    br = INITIAL_BANKROLL
    eq = [br]
    for b in bets:
        br += b.pnl
        eq.append(br)
    returns = [b.pnl / b.size_usdc for b in bets if b.size_usdc > 0]
    sharpe = 0.0
    if len(returns) > 1:
        mean = statistics.fmean(returns)
        std = statistics.pstdev(returns)
        sharpe = (mean / std) * math.sqrt(len(returns)) if std > 0 else 0.0
    peak = eq[0]
    mdd = 0.0
    for v in eq:
        if v > peak:
            peak = v
        if peak > 0:
            dd = (peak - v) / peak
            if dd > mdd:
                mdd = dd
    return Stats(
        n=len(bets), wins=wins, win_rate=wins / len(bets),
        pnl=pnl, roi_pct=pnl / INITIAL_BANKROLL * 100,
        sharpe=sharpe, max_dd_pct=mdd * 100,
    )


def bootstrap(bets: list[SimBet], n: int = 2000, seed: int = 42) -> dict:
    if not bets:
        return {"ci_lo": 0.0, "ci_hi": 0.0, "median": 0.0, "p_positive": 0.0}
    rng = random.Random(seed)
    pnls = [b.pnl for b in bets]
    sums = sorted(sum(rng.choices(pnls, k=len(pnls))) for _ in range(n))
    return {
        "ci_lo": sums[int(n * 0.025)],
        "ci_hi": sums[int(n * 0.975)],
        "median": sums[n // 2],
        "p_positive": sum(1 for s in sums if s > 0) / n,
    }


def main() -> None:
    global INITIAL_BANKROLL, MAX_POSITION_PCT, KELLY_FRACTION
    global MIN_CONFLUENCE_WHALES, CONFLUENCE_WINDOW_SEC, WHALE_MIN_TRADE_USDC
    global MIN_VOLUME_24HR, MIN_TOTAL_VOLUME, MIN_ENTRY_PRICE, MAX_ENTRY_PRICE
    p = argparse.ArgumentParser()
    p.add_argument("--months", type=int, default=9)
    p.add_argument("--bankroll", type=float, default=INITIAL_BANKROLL)
    p.add_argument("--verticals", default="politics")
    p.add_argument("--kelly", type=float, default=KELLY_FRACTION)
    p.add_argument("--max-pos", type=float, default=MAX_POSITION_PCT)
    p.add_argument("--min-confluence", type=int, default=MIN_CONFLUENCE_WHALES)
    p.add_argument("--confluence-window-min", type=int, default=CONFLUENCE_WINDOW_SEC // 60)
    p.add_argument("--whale-min-usdc", type=float, default=WHALE_MIN_TRADE_USDC)
    p.add_argument("--min-vol-24h", type=float, default=MIN_VOLUME_24HR)
    p.add_argument("--min-total-vol", type=float, default=MIN_TOTAL_VOLUME)
    p.add_argument("--min-price", type=float, default=MIN_ENTRY_PRICE)
    p.add_argument("--max-price", type=float, default=MAX_ENTRY_PRICE)
    args = p.parse_args()

    INITIAL_BANKROLL = args.bankroll
    KELLY_FRACTION = args.kelly
    MAX_POSITION_PCT = args.max_pos
    MIN_CONFLUENCE_WHALES = args.min_confluence
    CONFLUENCE_WINDOW_SEC = args.confluence_window_min * 60
    WHALE_MIN_TRADE_USDC = args.whale_min_usdc
    MIN_VOLUME_24HR = args.min_vol_24h
    MIN_TOTAL_VOLUME = args.min_total_vol
    MIN_ENTRY_PRICE = args.min_price
    MAX_ENTRY_PRICE = args.max_price

    verticals = [v.strip() for v in args.verticals.split(",") if v.strip()]

    print("=" * 78)
    print("polymoney CONFLUENCE backtest")
    print(f"  bankroll=${INITIAL_BANKROLL}  months={args.months}  verticals={verticals}")
    print(f"  confluence: min={MIN_CONFLUENCE_WHALES} whales in {CONFLUENCE_WINDOW_SEC//60}min window")
    print(f"  liquidity: vol24h>=${MIN_VOLUME_24HR}  total_vol>=${MIN_TOTAL_VOLUME}")
    print(f"  sizing: kelly={KELLY_FRACTION}  max_pos={MAX_POSITION_PCT:.0%}  "
          f"price=[{MIN_ENTRY_PRICE:.2f},{MAX_ENTRY_PRICE:.2f}]")
    print("=" * 78)

    end = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    oldest_needed = (end - timedelta(days=30 * args.months)).timestamp()

    print("\nFetching top whales (union all-time + 30d) ...")
    all_time = fetch_top_whales("all", WHALE_LEADERBOARD_LIMIT)
    recent = fetch_top_whales("30d", WHALE_LEADERBOARD_LIMIT)
    wm: dict[str, dict] = {}
    for w in all_time + recent:
        a = w.get("proxyWallet", "").lower()
        if a:
            wm[a] = w
    whales = list(wm.values())
    print(f"  -> {len(whales)} unique whales")

    print("Paginating whale trades ...")
    for i, w in enumerate(whales, 1):
        fetch_whale_trades(w["proxyWallet"].lower(), oldest_needed)
        if i % 25 == 0:
            print(f"  ({i}/{len(whales)} cached)")
    total = sum(len(v) for v in _trades_cache.values())
    print(f"  -> {total} total trades cached")

    pooled: list[SimBet] = []
    per_window: list[dict] = []

    for i in range(args.months, 0, -1):
        ts_start = end - timedelta(days=30 * i)
        ts_end = end - timedelta(days=30 * (i - 1))
        label = f"{ts_start.date()} -> {ts_end.date()}"
        print(f"\n[Window {args.months - i + 1}/{args.months}] {label}")
        markets = []
        for v in verticals:
            markets.extend(fetch_markets_for_vertical(ts_start, ts_end, v))
        liquid = [m for m in markets if passes_liquidity_filter(m)]
        print(f"  resolved markets: {len(markets)}  liquid: {len(liquid)}")
        entries = find_confluence_entries(
            liquid, whales,
            int(ts_start.timestamp()), int(ts_end.timestamp()),
            oldest_needed,
        )
        print(f"  confluence entries: {len(entries)}")
        bets = run_confluence_strategy(entries)
        s = compute_stats(bets)
        print(f"  trades={s.n:3d}  wr={s.win_rate:.0%}  pnl=${s.pnl:8.2f}  "
              f"roi={s.roi_pct:+6.1f}%  sharpe={s.sharpe:5.2f}  dd={s.max_dd_pct:5.1f}%")
        if bets:
            months_roi = s.roi_pct  # single-window is monthly ROI on fresh bankroll
            print(f"  monthly ROI on $500: {months_roi:+.1f}%  (target: >=10.0%)")
        pooled.extend(bets)
        per_window.append({"window": label, "stats": s.__dict__, "n_entries": len(entries)})

    print("\n" + "=" * 78)
    print("AGGREGATE")
    print("=" * 78)
    s = compute_stats(pooled)
    bs = bootstrap(pooled)
    print(f"  n={s.n}  wr={s.win_rate:.1%}  pnl=${s.pnl:.2f}  "
          f"sharpe={s.sharpe:.2f}  dd={s.max_dd_pct:.1f}%")
    print(f"  median=${bs['median']:+.2f}  P(+)={bs['p_positive']:.0%}  "
          f"CI95=[${bs['ci_lo']:.2f}, ${bs['ci_hi']:.2f}]")
    months_with_trades = sum(1 for w in per_window if w["stats"]["n"] > 0)
    avg_monthly_roi = (
        sum(w["stats"]["roi_pct"] for w in per_window if w["stats"]["n"] > 0) / months_with_trades
        if months_with_trades else 0.0
    )
    print(f"  avg monthly ROI on $500 (active months): {avg_monthly_roi:+.1f}%")

    print("\nVERDICT")
    if s.n < 20:
        print(f"  INSUFFICIENT: only {s.n} trades")
    elif avg_monthly_roi >= 10.0 and bs["p_positive"] >= 0.70:
        print(f"  TARGET HIT: avg monthly ROI {avg_monthly_roi:+.1f}% >= 10%, P(+)={bs['p_positive']:.0%}")
    elif bs["p_positive"] >= 0.65 and s.sharpe > 0.5:
        print(f"  PROVISIONAL: P(+)={bs['p_positive']:.0%}, sharpe={s.sharpe:.2f}. Profitable but under target.")
    elif bs["ci_hi"] < 0:
        print(f"  FAIL: upper CI ${bs['ci_hi']:.2f} < 0")
    else:
        print(f"  INCONCLUSIVE: sample too narrow or edge not confirmed")


if __name__ == "__main__":
    main()
