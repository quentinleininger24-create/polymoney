#!/usr/bin/env python3
"""Smart-flow v2: quality-filtered, size-weighted, monthly-capped.

Three improvements over v1 (scripts/backtest_smart_flow.py):

1. WHALE ACCURACY FILTER. Not every top-100 whale is equally good. Compute
   each whale's historical win rate on resolved markets they traded (their
   side at entry vs actual resolution), keep only those with WR >= 60 pct
   and >= 10 resolved trades. Removes noise traders / shills. Walk-forward
   correct: accuracy is computed ONLY from markets resolved before each
   test window's start.

2. QUALITY-WEIGHTED SIZING. Each qualifying whale's trades are weighted by
   their personal win rate when aggregating market flow. A 75 pct-WR whale
   contributes 1.5x more to the flow imbalance than a 60 pct-WR whale. So
   the dominance threshold fires sooner when the strongest whales agree.

3. MONTHLY-DRAWDOWN CIRCUIT. If cumulative month-to-date PnL drops below
   -7.5 pct of bankroll, stop taking new trades this month. Directly caps
   worst-month losses at ~-8 pct instead of the v1's -22 pct.

Locked v1 for comparison: dom 0.6, max-pos 12, kelly 0.5 -> avg +45/mo,
worst -22 pct, DD 22 pct, 75 pct consistency, n=34 on 12 months.

v2 targets: avg >= 30 pct/mo, worst >= -10 pct, DD <= 15 pct, consistency
>= 60 pct.
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
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

GAMMA = "https://gamma-api.polymarket.com"
LB = "https://lb-api.polymarket.com"
DATA = "https://data-api.polymarket.com"

INITIAL_BANKROLL = 500.0
MAX_POSITION_PCT = 0.12
KELLY_FRACTION = 0.5
FEE_PCT = 0.0
SLIPPAGE_PCT = 0.005
GAS_USDC = 0.05

MIN_ENTRY_PRICE = 0.10
MAX_ENTRY_PRICE = 0.75

DOMINANCE_THRESHOLD = 0.6
MIN_WHALE_VOLUME_USDC = 2000.0

MIN_VOLUME_24HR = 1000.0
MIN_TOTAL_VOLUME = 10000.0

# v2 additions
MIN_WHALE_WR = 0.60
MIN_WHALE_RESOLVED_TRADES = 10
MONTHLY_DRAWDOWN_STOP_PCT = 0.075
USE_WR_WEIGHTED_FLOW = True

WHALE_LEADERBOARD_LIMIT = 100
TRADES_PAGE_SIZE = 500
TRADES_MAX_PAGES = 30

REQUEST_DELAY_SEC = 0.1

VERTICAL_TAGS = {
    "politics": ("us-politics", "elections", "trump"),
    "sports": ("sports", "nfl", "nba", "soccer", "mlb"),
    "crypto": ("crypto", "bitcoin", "ethereum"),
    "geopolitics": ("geopolitics", "russia-ukraine", "middle-east"),
}


def _http_json(url, params=None, retries=3):
    if params:
        url = f"{url}?{urllib.parse.urlencode(params)}"
    last = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "polymoney-v2/0.1"})
            with urllib.request.urlopen(req, timeout=30) as r:
                data = json.loads(r.read())
            time.sleep(REQUEST_DELAY_SEC)
            return data
        except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError) as e:
            last = e
            time.sleep(0.5 * (attempt + 1))
    raise RuntimeError(f"HTTP failed: {url} :: {last}")


def _parse_iso(s):
    if not s:
        return None
    try:
        return datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    except ValueError:
        return None


def _num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def fetch_markets(start, end, vertical):
    out, seen = [], set()
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
                for m in e.get("markets") or []:
                    cond = m.get("conditionId")
                    if cond and cond not in seen and m.get("closed"):
                        m.setdefault("endDate", e.get("endDate"))
                        seen.add(cond)
                        out.append(m)
            if stop:
                break
            offset += 100
    return out


def passes_liquidity(m):
    return _num(m.get("volume24hr")) >= MIN_VOLUME_24HR or _num(m.get("volumeNum") or m.get("volume")) >= MIN_TOTAL_VOLUME


def resolution(m):
    raw_o, raw_p = m.get("outcomes"), m.get("outcomePrices")
    if not raw_o or not raw_p:
        return None
    try:
        outs = json.loads(raw_o) if isinstance(raw_o, str) else raw_o
        prices = json.loads(raw_p) if isinstance(raw_p, str) else raw_p
        p0, p1 = float(prices[0]), float(prices[1])
    except (json.JSONDecodeError, TypeError, ValueError):
        return None
    if len(outs) != 2 or abs(p0 - p1) < 0.01:
        return None
    return str(outs[0 if p0 > p1 else 1]).strip().upper()


def fetch_top_whales(window, limit):
    rows = _http_json(f"{LB}/profit", {"window": window, "limit": limit})
    return rows if isinstance(rows, list) else []


_trades_cache = {}


def fetch_whale_trades(wallet, oldest_ts):
    if wallet in _trades_cache:
        return _trades_cache[wallet]
    out = []
    for page in range(TRADES_MAX_PAGES):
        try:
            rows = _http_json(f"{DATA}/trades", {
                "user": wallet, "limit": TRADES_PAGE_SIZE, "offset": page * TRADES_PAGE_SIZE,
            })
        except Exception as e:
            print(f"  ! trades failed for {wallet[:8]}: {e}", file=sys.stderr)
            break
        if not isinstance(rows, list) or not rows:
            break
        out.extend(rows)
        if float(rows[-1].get("timestamp", 0) or 0) < oldest_ts:
            break
        if len(rows) < TRADES_PAGE_SIZE:
            break
    _trades_cache[wallet] = out
    return out


# --- Whale accuracy (walk-forward correct) ---

def compute_whale_accuracy_as_of(wallet: str, cutoff_ts: float, resolved_markets_index: dict) -> tuple[float, int]:
    """Return (win_rate, n_trades) using only trades on markets resolved BEFORE cutoff_ts."""
    trades = _trades_cache.get(wallet, [])
    wins = 0
    total = 0
    for t in trades:
        ts = float(t.get("timestamp", 0) or 0)
        if ts >= cutoff_ts:
            continue
        cond = t.get("conditionId")
        if not cond or cond not in resolved_markets_index:
            continue
        m = resolved_markets_index[cond]
        md = _parse_iso(m.get("endDate"))
        if not md or md.timestamp() >= cutoff_ts:
            continue
        winner = resolution(m)
        if not winner:
            continue
        side = str(t.get("outcome", "")).strip().upper()
        if side not in {"YES", "NO"}:
            continue
        total += 1
        if side == winner:
            wins += 1
    if total == 0:
        return (0.0, 0)
    return (wins / total, total)


def kelly_size(edge_prob, price, bankroll, mult=1.0):
    if price <= 0 or price >= 1 or edge_prob <= price:
        return 0.0
    b = (1 - price) / price
    p = edge_prob
    q = 1 - p
    f = (b * p - q) / b
    f = max(0.0, min(1.0, f)) * KELLY_FRACTION * mult
    f = min(f, MAX_POSITION_PCT)
    return bankroll * f


def settle(entry_price, size, won):
    eff = min(0.99, entry_price * (1 + SLIPPAGE_PCT))
    shares = size / eff
    cost = size * (1 + FEE_PCT) + GAS_USDC
    return shares - cost if won else -cost


@dataclass
class Bet:
    market_id: str
    question: str
    entry_ts: int
    side: str
    price: float
    size: float
    dominance: float
    whale_vol: float
    won: bool
    pnl: float


def find_flow_entries(markets, qualified_whales, whale_wr, test_start_ts, test_end_ts):
    """Now uses WR-weighted notional if USE_WR_WEIGHTED_FLOW is set."""
    per_market = defaultdict(list)
    for addr in qualified_whales:
        w_wr = whale_wr[addr]
        for t in _trades_cache.get(addr, []):
            cond = t.get("conditionId")
            if not cond:
                continue
            ts = int(t.get("timestamp", 0) or 0)
            side = str(t.get("outcome", "")).strip().upper()
            if side not in {"YES", "NO"}:
                continue
            price = float(t.get("price", 0) or 0)
            size_units = float(t.get("size", 0) or 0)
            notional = size_units * price
            if notional <= 0:
                continue
            # WR-weighted: a whale with WR 0.75 contributes 1.5x notional
            weight = (w_wr / 0.5) if USE_WR_WEIGHTED_FLOW and w_wr > 0 else 1.0
            per_market[cond].append((ts, side, notional * weight, price))

    mbc = {m["conditionId"]: m for m in markets if m.get("conditionId")}
    entries = []
    for cond, trades in per_market.items():
        if cond not in mbc:
            continue
        m = mbc[cond]
        if not passes_liquidity(m):
            continue
        trades.sort(key=lambda r: r[0])
        net_yes = 0.0
        net_no = 0.0
        vol = 0.0
        entered = set()
        for ts, side, notional, price in trades:
            if side == "YES":
                net_yes += notional
            else:
                net_no += notional
            vol += notional
            if vol < MIN_WHALE_VOLUME_USDC:
                continue
            imbalance = net_yes - net_no
            dom = abs(imbalance) / vol if vol > 0 else 0.0
            if dom < DOMINANCE_THRESHOLD:
                continue
            dominant = "YES" if imbalance > 0 else "NO"
            if dominant in entered:
                continue
            if not (test_start_ts <= ts <= test_end_ts):
                continue
            entered.add(dominant)
            entries.append({
                "market": m, "entry_ts": ts, "side": dominant,
                "price": price, "dominance": dom, "whale_vol": vol,
            })
    entries.sort(key=lambda e: e["entry_ts"])
    return entries


def run_flow(entries, monthly_stop_threshold: float = -MONTHLY_DRAWDOWN_STOP_PCT * INITIAL_BANKROLL):
    """Monthly-drawdown circuit: stop new bets in a month once MTD PnL <= stop."""
    bets = []
    bankroll = INITIAL_BANKROLL
    month_pnl = 0.0
    month_halted = False
    for e in entries:
        if month_halted:
            continue
        m = e["market"]
        winner = resolution(m)
        if winner is None:
            continue
        entry_yes = e["price"]
        side_price = entry_yes if e["side"] == "YES" else 1 - entry_yes
        if side_price < MIN_ENTRY_PRICE or side_price > MAX_ENTRY_PRICE:
            continue

        mult = 1.0 + (e["dominance"] - DOMINANCE_THRESHOLD) * 2.0
        mult = max(0.5, min(2.0, mult))
        edge_prob = min(0.95, side_price + 0.05 + 0.1 * (e["dominance"] - DOMINANCE_THRESHOLD))
        size = kelly_size(edge_prob, side_price, bankroll, mult)
        if size < 1.0:
            continue
        size = min(size, bankroll - GAS_USDC)
        if size < 1.0:
            continue

        won = e["side"] == winner
        pnl = settle(side_price, size, won)
        bankroll += pnl
        month_pnl += pnl
        bets.append(Bet(
            market_id=m["conditionId"], question=(m.get("question") or "")[:80],
            entry_ts=e["entry_ts"], side=e["side"], price=side_price, size=size,
            dominance=e["dominance"], whale_vol=e["whale_vol"], won=won, pnl=pnl,
        ))
        # Check monthly circuit
        if month_pnl <= monthly_stop_threshold:
            month_halted = True
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


def stats(bets):
    if not bets:
        return Stats(0, 0, 0, 0, 0, 0, 0)
    wins = sum(1 for b in bets if b.won)
    pnl = sum(b.pnl for b in bets)
    br = INITIAL_BANKROLL
    eq = [br]
    for b in bets:
        br += b.pnl
        eq.append(br)
    rets = [b.pnl / b.size for b in bets if b.size > 0]
    sh = 0.0
    if len(rets) > 1:
        std = statistics.pstdev(rets)
        sh = (statistics.fmean(rets) / std) * math.sqrt(len(rets)) if std > 0 else 0.0
    peak = eq[0]
    mdd = 0.0
    for v in eq:
        if v > peak:
            peak = v
        if peak > 0:
            dd = (peak - v) / peak
            if dd > mdd:
                mdd = dd
    return Stats(len(bets), wins, wins / len(bets), pnl, pnl / INITIAL_BANKROLL * 100, sh, mdd * 100)


def bootstrap(bets, n=2000, seed=42):
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


def main():
    global INITIAL_BANKROLL, MAX_POSITION_PCT, KELLY_FRACTION
    global DOMINANCE_THRESHOLD, MIN_WHALE_VOLUME_USDC
    global MIN_VOLUME_24HR, MIN_TOTAL_VOLUME, MIN_ENTRY_PRICE, MAX_ENTRY_PRICE
    global MIN_WHALE_WR, MIN_WHALE_RESOLVED_TRADES, MONTHLY_DRAWDOWN_STOP_PCT
    global USE_WR_WEIGHTED_FLOW
    p = argparse.ArgumentParser()
    p.add_argument("--months", type=int, default=12)
    p.add_argument("--bankroll", type=float, default=INITIAL_BANKROLL)
    p.add_argument("--verticals", default="politics")
    p.add_argument("--kelly", type=float, default=KELLY_FRACTION)
    p.add_argument("--max-pos", type=float, default=MAX_POSITION_PCT)
    p.add_argument("--dominance", type=float, default=DOMINANCE_THRESHOLD)
    p.add_argument("--min-whale-vol", type=float, default=MIN_WHALE_VOLUME_USDC)
    p.add_argument("--min-vol-24h", type=float, default=MIN_VOLUME_24HR)
    p.add_argument("--min-total-vol", type=float, default=MIN_TOTAL_VOLUME)
    p.add_argument("--min-whale-wr", type=float, default=MIN_WHALE_WR)
    p.add_argument("--min-whale-trades", type=int, default=MIN_WHALE_RESOLVED_TRADES)
    p.add_argument("--monthly-stop-pct", type=float, default=MONTHLY_DRAWDOWN_STOP_PCT)
    p.add_argument("--disable-wr-weighting", action="store_true")
    args = p.parse_args()

    INITIAL_BANKROLL = args.bankroll
    KELLY_FRACTION = args.kelly
    MAX_POSITION_PCT = args.max_pos
    DOMINANCE_THRESHOLD = args.dominance
    MIN_WHALE_VOLUME_USDC = args.min_whale_vol
    MIN_VOLUME_24HR = args.min_vol_24h
    MIN_TOTAL_VOLUME = args.min_total_vol
    MIN_WHALE_WR = args.min_whale_wr
    MIN_WHALE_RESOLVED_TRADES = args.min_whale_trades
    MONTHLY_DRAWDOWN_STOP_PCT = args.monthly_stop_pct
    USE_WR_WEIGHTED_FLOW = not args.disable_wr_weighting

    verticals = [v.strip() for v in args.verticals.split(",") if v.strip()]

    print("=" * 78)
    print("polymoney SMART-FLOW v2 backtest (quality-filtered + monthly-capped)")
    print(f"  bankroll=${INITIAL_BANKROLL}  months={args.months}  verticals={verticals}")
    print(f"  dominance>={DOMINANCE_THRESHOLD:.2f}  min_whale_vol=${MIN_WHALE_VOLUME_USDC:.0f}")
    print(f"  whale quality: WR>={MIN_WHALE_WR:.0%}  min_trades={MIN_WHALE_RESOLVED_TRADES}  "
          f"WR-weighted={USE_WR_WEIGHTED_FLOW}")
    print(f"  monthly circuit: stop if MTD PnL <= -{MONTHLY_DRAWDOWN_STOP_PCT:.1%} of bankroll")
    print(f"  sizing: kelly={KELLY_FRACTION}  max_pos={MAX_POSITION_PCT:.0%}")
    print("=" * 78)

    end = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    oldest_ts = (end - timedelta(days=30 * args.months)).timestamp()

    print("\nFetching whales + trades ...")
    at = fetch_top_whales("all", WHALE_LEADERBOARD_LIMIT)
    rc = fetch_top_whales("30d", WHALE_LEADERBOARD_LIMIT)
    wm = {}
    for w in at + rc:
        a = w.get("proxyWallet", "").lower()
        if a:
            wm[a] = w
    whales = list(wm.values())
    print(f"  unique whales: {len(whales)}")
    for i, w in enumerate(whales, 1):
        fetch_whale_trades(w["proxyWallet"].lower(), oldest_ts)
        if i % 25 == 0:
            print(f"  ({i}/{len(whales)} cached)")

    # Fetch ALL resolved markets we'll need for whale accuracy computation
    print("\nFetching resolved markets for whale accuracy calc ...")
    full_start = end - timedelta(days=30 * args.months)
    # Extend back enough to have training data for earliest test window
    history_start = full_start - timedelta(days=365)
    all_resolved: list[dict] = []
    for v in verticals:
        all_resolved.extend(fetch_markets(history_start, end, v))
    resolved_index = {m["conditionId"]: m for m in all_resolved if m.get("conditionId")}
    print(f"  resolved markets indexed: {len(resolved_index)}")

    pooled = []
    per_win = []

    for i in range(args.months, 0, -1):
        ts_start = end - timedelta(days=30 * i)
        ts_end = end - timedelta(days=30 * (i - 1))
        label = f"{ts_start.date()} -> {ts_end.date()}"
        print(f"\n[Window {args.months - i + 1}/{args.months}] {label}")

        # Compute whale accuracy walk-forward: only use resolved markets that
        # resolved BEFORE this test window started.
        cutoff = ts_start.timestamp()
        qualified = []
        wr_map = {}
        for w in whales:
            addr = w["proxyWallet"].lower()
            wr, n = compute_whale_accuracy_as_of(addr, cutoff, resolved_index)
            wr_map[addr] = wr
            if n >= MIN_WHALE_RESOLVED_TRADES and wr >= MIN_WHALE_WR:
                qualified.append(addr)
        print(f"  qualified whales (WR>={MIN_WHALE_WR:.0%}, n>={MIN_WHALE_RESOLVED_TRADES}): "
              f"{len(qualified)}/{len(whales)}")

        markets = []
        for v in verticals:
            markets.extend(fetch_markets(ts_start, ts_end, v))
        liq = [m for m in markets if passes_liquidity(m)]
        print(f"  resolved: {len(markets)}  liquid: {len(liq)}")

        entries = find_flow_entries(
            liq, qualified, wr_map,
            int(ts_start.timestamp()), int(ts_end.timestamp()),
        )
        print(f"  dominance entries: {len(entries)}")
        bets = run_flow(entries)
        s = stats(bets)
        if bets:
            print(f"  trades={s.n:3d}  wr={s.win_rate:.0%}  pnl=${s.pnl:8.2f}  "
                  f"roi={s.roi_pct:+6.1f}%  sharpe={s.sharpe:5.2f}  dd={s.max_dd_pct:5.1f}%")
        else:
            print("  no trades")
        pooled.extend(bets)
        per_win.append({"window": label, "stats": s.__dict__, "entries": len(entries)})

    print("\n" + "=" * 78)
    print("AGGREGATE v2")
    print("=" * 78)
    s = stats(pooled)
    bs = bootstrap(pooled)
    active = [w for w in per_win if w["stats"]["n"] > 0]
    consistency = len(active) / len(per_win) if per_win else 0.0
    avg_m = sum(w["stats"]["roi_pct"] for w in active) / len(active) if active else 0.0
    worst = min((w["stats"]["roi_pct"] for w in active), default=0.0)
    best = max((w["stats"]["roi_pct"] for w in active), default=0.0)

    print(f"  n={s.n}  wr={s.win_rate:.1%}  pnl=${s.pnl:.2f}  roi={s.roi_pct:+.1f}%")
    print(f"  sharpe={s.sharpe:.2f}  max_dd={s.max_dd_pct:.1f}%")
    print(f"  median=${bs['median']:+.2f}  P(+)={bs['p_positive']:.0%}  "
          f"CI95=[${bs['ci_lo']:.2f}, ${bs['ci_hi']:.2f}]")
    print(f"  active months: {len(active)}/{len(per_win)} ({consistency:.0%})")
    print(f"  monthly: avg {avg_m:+.1f}%  worst {worst:+.1f}%  best {best:+.1f}%")

    print("\nVERDICT")
    if s.n < 15:
        v = f"INSUFFICIENT: n={s.n}"
    elif avg_m >= 10 and consistency >= 0.6 and worst >= -10 and bs["p_positive"] >= 0.80:
        v = f"SOLID: avg {avg_m:+.1f}%/mo, consistency {consistency:.0%}, worst {worst:+.1f}%"
    elif avg_m >= 10 and bs["p_positive"] >= 0.70:
        v = f"PASS: avg {avg_m:+.1f}%/mo but worst {worst:+.1f}% / consistency {consistency:.0%}"
    elif bs["ci_hi"] < 0:
        v = f"FAIL: upper CI ${bs['ci_hi']:.2f} < 0"
    else:
        v = f"INCONCLUSIVE: avg {avg_m:+.1f}%/mo, consistency {consistency:.0%}"
    print(f"  {v}")


if __name__ == "__main__":
    main()
