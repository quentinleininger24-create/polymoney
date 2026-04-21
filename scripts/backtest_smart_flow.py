#!/usr/bin/env python3
"""Smart-flow strategy: trade on cumulative whale flow dominance.

Problem with the 'smart-whale' trigger strategy: it fires once per market
(first big whale trade), so 5/9 months had 0 trades. Monthly consistency is
bad even when aggregate ROI is great.

Fix: treat every whale trade as a flow contribution, not a trigger. As
whales pile into a side on a market, cumulative flow imbalance grows. We
enter the moment that imbalance clears a dominance threshold. This gives:

- More trades (every market where consensus forms, not just the first
  whale's trade)
- Better entry timing (we enter at the flow-confirmation point, not a
  potentially premature single whale)
- Smoother monthly curve (most active political months have >=1 dominance
  event; quiet months generate fewer but still some signals)

Algorithm per market:
1. Collect all top-100 whale trades on the market in chronological order.
2. Walk forward, maintaining running net_flow_usd = cumulative(sum_YES) -
   cumulative(sum_NO). Each trade also updates total_whale_volume =
   cumulative(|sum_YES| + |sum_NO|).
3. At each step, if abs(net_flow) / total_whale_volume >= DOMINANCE_THRESHOLD
   AND total_whale_volume >= MIN_WHALE_VOLUME_USDC, fire entry on
   dominant side using that trade's price as entry.
4. Only one entry per (market, side). Hold to resolution.

Sizing: 0.5 Kelly, 15 pct max position (safer than smart-whale's 0.75/25).
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

# Flow dominance threshold: how asymmetric does cumulative whale flow need
# to be before we trigger? 0.6 means 80/20 side split.
# Locked at 0.6 after a 12-month walk-forward: avg +45%/mo vs +63% at 0.5
# but worst month -22% vs -27% and max DD 21.8% vs 26.7%. Safer profile
# is worth the 18 point monthly avg hit.
DOMINANCE_THRESHOLD = 0.6
MIN_WHALE_VOLUME_USDC = 2000.0  # need at least $2k of whale activity to trust

MIN_VOLUME_24HR = 1000.0
MIN_TOTAL_VOLUME = 10000.0

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
            req = urllib.request.Request(url, headers={"User-Agent": "polymoney-flow/0.1"})
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


def find_flow_entries(markets, whales, test_start_ts, test_end_ts, oldest_ts):
    """For each liquid market, find the first point in the test window where
    cumulative whale flow dominance crosses threshold."""
    whale_addrs = {w["proxyWallet"].lower() for w in whales if w.get("proxyWallet")}
    per_market = defaultdict(list)  # conditionId -> list of (ts, side, notional_usdc, price)

    for addr in whale_addrs:
        for t in fetch_whale_trades(addr, oldest_ts):
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
            per_market[cond].append((ts, side, notional, price))

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
        entered_sides = set()
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
            dominant_side = "YES" if imbalance > 0 else "NO"
            if dominant_side in entered_sides:
                continue
            if not (test_start_ts <= ts <= test_end_ts):
                continue
            entered_sides.add(dominant_side)
            entries.append({
                "market": m,
                "entry_ts": ts,
                "side": dominant_side,
                "price": price,
                "dominance": dom,
                "whale_vol": vol,
            })
    entries.sort(key=lambda e: e["entry_ts"])
    return entries


def run_flow(entries):
    bets = []
    bankroll = INITIAL_BANKROLL
    for e in entries:
        m = e["market"]
        winner = resolution(m)
        if winner is None:
            continue
        entry_yes = e["price"]
        side_price = entry_yes if e["side"] == "YES" else 1 - entry_yes
        if side_price < MIN_ENTRY_PRICE or side_price > MAX_ENTRY_PRICE:
            continue
        # Dominance-weighted multiplier: 0.5 dom = 1.0x, 0.7 = 1.4x, 0.9 = 1.8x
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
        bets.append(Bet(
            market_id=m["conditionId"],
            question=(m.get("question") or "")[:80],
            entry_ts=e["entry_ts"], side=e["side"], price=side_price,
            size=size, dominance=e["dominance"], whale_vol=e["whale_vol"],
            won=won, pnl=pnl,
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
    p.add_argument("--min-price", type=float, default=MIN_ENTRY_PRICE)
    p.add_argument("--max-price", type=float, default=MAX_ENTRY_PRICE)
    args = p.parse_args()

    INITIAL_BANKROLL = args.bankroll
    KELLY_FRACTION = args.kelly
    MAX_POSITION_PCT = args.max_pos
    DOMINANCE_THRESHOLD = args.dominance
    MIN_WHALE_VOLUME_USDC = args.min_whale_vol
    MIN_VOLUME_24HR = args.min_vol_24h
    MIN_TOTAL_VOLUME = args.min_total_vol
    MIN_ENTRY_PRICE = args.min_price
    MAX_ENTRY_PRICE = args.max_price

    verticals = [v.strip() for v in args.verticals.split(",") if v.strip()]

    print("=" * 78)
    print("polymoney SMART-FLOW backtest")
    print(f"  bankroll=${INITIAL_BANKROLL}  months={args.months}  verticals={verticals}")
    print(f"  dominance>={DOMINANCE_THRESHOLD:.2f}  min_whale_vol=${MIN_WHALE_VOLUME_USDC:.0f}")
    print(f"  liquidity: vol24h>=${MIN_VOLUME_24HR:.0f}  total_vol>=${MIN_TOTAL_VOLUME:.0f}")
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
    print(f"  whales: {len(whales)}")
    for i, w in enumerate(whales, 1):
        fetch_whale_trades(w["proxyWallet"].lower(), oldest_ts)
        if i % 25 == 0:
            print(f"  ({i}/{len(whales)})")
    print(f"  total trades cached: {sum(len(v) for v in _trades_cache.values())}")

    pooled = []
    per_win = []

    for i in range(args.months, 0, -1):
        ts_start = end - timedelta(days=30 * i)
        ts_end = end - timedelta(days=30 * (i - 1))
        label = f"{ts_start.date()} -> {ts_end.date()}"
        print(f"\n[Window {args.months - i + 1}/{args.months}] {label}")
        markets = []
        for v in verticals:
            markets.extend(fetch_markets(ts_start, ts_end, v))
        liq = [m for m in markets if passes_liquidity(m)]
        print(f"  resolved: {len(markets)}  liquid: {len(liq)}")
        entries = find_flow_entries(
            liq, whales, int(ts_start.timestamp()), int(ts_end.timestamp()), oldest_ts
        )
        print(f"  dominance entries: {len(entries)}")
        bets = run_flow(entries)
        s = stats(bets)
        if bets:
            print(f"  trades={s.n:3d}  wr={s.win_rate:.0%}  pnl=${s.pnl:8.2f}  "
                  f"roi={s.roi_pct:+6.1f}%  sharpe={s.sharpe:5.2f}  dd={s.max_dd_pct:5.1f}%")
            print(f"  monthly ROI on ${INITIAL_BANKROLL:.0f}: {s.roi_pct:+.1f}%  (target: >=10%)")
        else:
            print(f"  no trades")
        pooled.extend(bets)
        per_win.append({"window": label, "stats": s.__dict__, "entries": len(entries)})

    print("\n" + "=" * 78)
    print("AGGREGATE")
    print("=" * 78)
    s = stats(pooled)
    bs = bootstrap(pooled)
    active_wins = [w for w in per_win if w["stats"]["n"] > 0]
    consistency = len(active_wins) / len(per_win) if per_win else 0.0
    avg_monthly = sum(w["stats"]["roi_pct"] for w in active_wins) / len(active_wins) if active_wins else 0.0
    worst_monthly = min((w["stats"]["roi_pct"] for w in active_wins), default=0.0)
    best_monthly = max((w["stats"]["roi_pct"] for w in active_wins), default=0.0)

    print(f"  n={s.n}  wr={s.win_rate:.1%}  pnl=${s.pnl:.2f}  roi={s.roi_pct:+.1f}%")
    print(f"  sharpe={s.sharpe:.2f}  max_dd={s.max_dd_pct:.1f}%")
    print(f"  median=${bs['median']:+.2f}  P(+)={bs['p_positive']:.0%}  "
          f"CI95=[${bs['ci_lo']:.2f}, ${bs['ci_hi']:.2f}]")
    print(f"  active months: {len(active_wins)}/{len(per_win)} ({consistency:.0%} consistency)")
    print(f"  monthly: avg {avg_monthly:+.1f}%  worst {worst_monthly:+.1f}%  best {best_monthly:+.1f}%")

    print("\nVERDICT")
    if s.n < 20:
        v = f"INSUFFICIENT: n={s.n} < 20"
    elif avg_monthly >= 10 and consistency >= 0.6 and worst_monthly >= -10 and bs["p_positive"] >= 0.80:
        v = f"SOLID: avg {avg_monthly:+.1f}%/mo, consistency {consistency:.0%}, worst {worst_monthly:+.1f}%, P(+)={bs['p_positive']:.0%}"
    elif avg_monthly >= 10 and bs["p_positive"] >= 0.70:
        v = f"PASS: avg monthly {avg_monthly:+.1f}% but inconsistent ({consistency:.0%} active months)"
    elif bs["ci_hi"] < 0:
        v = f"FAIL: upper CI ${bs['ci_hi']:.2f} < 0"
    else:
        v = f"INCONCLUSIVE: avg {avg_monthly:+.1f}%/mo, consistency {consistency:.0%}"
    print(f"  {v}")


if __name__ == "__main__":
    main()
