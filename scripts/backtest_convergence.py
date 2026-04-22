#!/usr/bin/env python3
"""Convergence hunter: near-resolution whale-consensus vs market-price divergence.

Different trigger type than smart-whale or smart-flow. Instead of reacting
to recent whale activity, we look at markets CLOSE TO RESOLUTION and check
whether the cumulative historical whale position on that market disagrees
with the current market price. If yes, enter in the direction of whale
consensus and hold to resolution.

Why this fills the gap:
- smart-whale fires on a recent (last 30min) big whale trade
- smart-flow fires on a recent (last 48h) cumulative flow dominance
- convergence fires ANY TIME if an old whale thesis is still in force but
  price has drifted away from it, regardless of when whales last traded

Every day many political markets resolve. Always something to trade.

Algorithm:
1. For each market resolving in the test window, pick an entry time
   T_entry = resolution_time - 48h.
2. At T_entry, fetch the market's price (prices-history API).
3. From all whale trades on this market BEFORE T_entry, compute weighted
   consensus: net_flow_yes_minus_no / total_whale_notional.
4. If |consensus| >= 0.50 AND current price differs from consensus-implied
   price by >= 10 percentage points, enter in consensus direction.
5. Hold to resolution. Settle at actual outcome.

Sizing: Kelly 0.5 x max 10 pct position (safer -- many trades, cumulative risk).
"""

from __future__ import annotations

import argparse
import bisect
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
CLOB = "https://clob.polymarket.com"

INITIAL_BANKROLL = 500.0
MAX_POSITION_PCT = 0.10
KELLY_FRACTION = 0.5
FEE_PCT = 0.0
SLIPPAGE_PCT = 0.005
GAS_USDC = 0.05

MIN_ENTRY_PRICE = 0.10
MAX_ENTRY_PRICE = 0.80

ENTRY_WINDOW_HOURS = 48           # enter this many hours before resolution
MIN_CONSENSUS = 0.50              # abs(net_flow/total) needed from whales
MIN_PRICE_GAP = 0.10              # consensus-implied side vs actual side divergence
MIN_WHALE_VOLUME_USDC = 1000.0
MIN_VOLUME_24HR = 500.0
MIN_TOTAL_VOLUME = 5000.0

WHALE_LEADERBOARD_LIMIT = 100
TRADES_PAGE_SIZE = 500
TRADES_MAX_PAGES = 30
REQUEST_DELAY_SEC = 0.1
MAX_MARKETS_PER_WINDOW = 1200     # cap to control prices-history API load

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
            req = urllib.request.Request(url, headers={"User-Agent": "polymoney-conv/0.1"})
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


def extract_yes_token(m):
    raw = m.get("clobTokenIds")
    outs = m.get("outcomes")
    if not raw or not outs:
        return None
    try:
        ids = json.loads(raw) if isinstance(raw, str) else raw
        names = json.loads(outs) if isinstance(outs, str) else outs
    except (json.JSONDecodeError, TypeError):
        return None
    for name, tid in zip(names, ids):
        if str(name).strip().upper() == "YES":
            return str(tid)
    return None


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


_prices_cache = {}


def fetch_price_history(token_id):
    if token_id in _prices_cache:
        return _prices_cache[token_id]
    try:
        data = _http_json(f"{CLOB}/prices-history", {
            "market": token_id, "interval": "max", "fidelity": 60,
        })
    except Exception:
        _prices_cache[token_id] = []
        return []
    pts = data.get("history", []) if isinstance(data, dict) else []
    out = sorted((int(p["t"]), float(p["p"])) for p in pts if "t" in p and "p" in p)
    _prices_cache[token_id] = out
    return out


def price_at(token_id, ts, max_gap_sec=3 * 3600):
    hist = fetch_price_history(token_id)
    if not hist:
        return None
    idx = bisect.bisect_left(hist, (ts, 0.0))
    cand = []
    if idx < len(hist):
        cand.append(hist[idx])
    if idx > 0:
        cand.append(hist[idx - 1])
    if not cand:
        return None
    best = min(cand, key=lambda p: abs(p[0] - ts))
    return best[1] if abs(best[0] - ts) <= max_gap_sec else None


def kelly_size(edge_prob, price, bankroll):
    if price <= 0 or price >= 1 or edge_prob <= price:
        return 0.0
    b = (1 - price) / price
    f = (b * edge_prob - (1 - edge_prob)) / b
    f = max(0.0, min(1.0, f)) * KELLY_FRACTION
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
    price_yes: float
    side_price: float
    size: float
    consensus: float
    whale_vol: float
    won: bool
    pnl: float


def compute_whale_consensus(market_id: str, cutoff_ts: int, whales: list[str]) -> tuple[float, float]:
    """Return (net_yes - net_no) / total, total_volume_usdc
    considering only whale trades before cutoff_ts."""
    net_yes = 0.0
    net_no = 0.0
    total = 0.0
    for addr in whales:
        for t in _trades_cache.get(addr, []):
            if t.get("conditionId") != market_id:
                continue
            ts = int(t.get("timestamp", 0) or 0)
            if ts >= cutoff_ts:
                continue
            side = str(t.get("outcome", "")).strip().upper()
            if side not in {"YES", "NO"}:
                continue
            notional = float(t.get("size", 0) or 0) * float(t.get("price", 0) or 0)
            if notional <= 0:
                continue
            if side == "YES":
                net_yes += notional
            else:
                net_no += notional
            total += notional
    if total <= 0:
        return 0.0, 0.0
    return (net_yes - net_no) / total, total


def run_convergence(markets, whale_addrs, test_start_ts, test_end_ts):
    bets = []
    bankroll = INITIAL_BANKROLL
    # Sort markets by entry_ts so we progress forward in time (bankroll
    # compounding is forward-in-time correct).
    scheduled: list[tuple[int, dict, str]] = []
    for m in markets[:MAX_MARKETS_PER_WINDOW]:
        end_d = _parse_iso(m.get("endDate"))
        token = extract_yes_token(m)
        if not end_d or not token:
            continue
        entry_ts = int(end_d.timestamp()) - ENTRY_WINDOW_HOURS * 3600
        if not (test_start_ts <= entry_ts <= test_end_ts):
            continue
        if not passes_liquidity(m):
            continue
        scheduled.append((entry_ts, m, token))
    scheduled.sort(key=lambda r: r[0])

    for entry_ts, m, token in scheduled:
        consensus, vol = compute_whale_consensus(m["conditionId"], entry_ts, whale_addrs)
        if vol < MIN_WHALE_VOLUME_USDC:
            continue
        if abs(consensus) < MIN_CONSENSUS:
            continue
        yes_price = price_at(token, entry_ts)
        if yes_price is None:
            continue
        # Consensus-implied YES probability: midrange of observed-heavy-side
        implied_yes = 0.5 + 0.25 * consensus   # consensus 1.0 -> implied 0.75, etc.
        gap = implied_yes - yes_price
        # If consensus says YES but price hasn't caught up -> buy YES
        # If consensus says NO but price hasn't dropped -> buy NO
        if abs(gap) < MIN_PRICE_GAP:
            continue
        direction = "YES" if gap > 0 else "NO"
        side_price = yes_price if direction == "YES" else 1 - yes_price
        if side_price < MIN_ENTRY_PRICE or side_price > MAX_ENTRY_PRICE:
            continue

        edge_prob = min(0.95, side_price + abs(gap) * 0.6)
        size = kelly_size(edge_prob, side_price, bankroll)
        if size < 1.0:
            continue
        size = min(size, bankroll - GAS_USDC)
        if size < 1.0:
            continue

        winner = resolution(m)
        if winner is None:
            continue
        won = direction == winner
        pnl = settle(side_price, size, won)
        bankroll += pnl
        bets.append(Bet(
            market_id=m["conditionId"], question=(m.get("question") or "")[:80],
            entry_ts=entry_ts, side=direction, price_yes=yes_price,
            side_price=side_price, size=size, consensus=consensus, whale_vol=vol,
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
        "ci_lo": sums[int(n * 0.025)], "ci_hi": sums[int(n * 0.975)],
        "median": sums[n // 2],
        "p_positive": sum(1 for s in sums if s > 0) / n,
    }


def main():
    global INITIAL_BANKROLL, MAX_POSITION_PCT, KELLY_FRACTION
    global ENTRY_WINDOW_HOURS, MIN_CONSENSUS, MIN_PRICE_GAP
    global MIN_WHALE_VOLUME_USDC, MIN_VOLUME_24HR, MIN_TOTAL_VOLUME
    global MIN_ENTRY_PRICE, MAX_ENTRY_PRICE, MAX_MARKETS_PER_WINDOW
    p = argparse.ArgumentParser()
    p.add_argument("--months", type=int, default=12)
    p.add_argument("--bankroll", type=float, default=INITIAL_BANKROLL)
    p.add_argument("--verticals", default="politics")
    p.add_argument("--kelly", type=float, default=KELLY_FRACTION)
    p.add_argument("--max-pos", type=float, default=MAX_POSITION_PCT)
    p.add_argument("--entry-hours", type=int, default=ENTRY_WINDOW_HOURS)
    p.add_argument("--min-consensus", type=float, default=MIN_CONSENSUS)
    p.add_argument("--min-gap", type=float, default=MIN_PRICE_GAP)
    p.add_argument("--min-whale-vol", type=float, default=MIN_WHALE_VOLUME_USDC)
    p.add_argument("--max-markets", type=int, default=MAX_MARKETS_PER_WINDOW)
    args = p.parse_args()

    INITIAL_BANKROLL = args.bankroll
    KELLY_FRACTION = args.kelly
    MAX_POSITION_PCT = args.max_pos
    ENTRY_WINDOW_HOURS = args.entry_hours
    MIN_CONSENSUS = args.min_consensus
    MIN_PRICE_GAP = args.min_gap
    MIN_WHALE_VOLUME_USDC = args.min_whale_vol
    MAX_MARKETS_PER_WINDOW = args.max_markets

    verticals = [v.strip() for v in args.verticals.split(",") if v.strip()]

    print("=" * 78)
    print("polymoney CONVERGENCE HUNTER backtest")
    print(f"  bankroll=${INITIAL_BANKROLL}  months={args.months}  verticals={verticals}")
    print(f"  entry_window={ENTRY_WINDOW_HOURS}h before resolution")
    print(f"  min_consensus={MIN_CONSENSUS:.2f}  min_price_gap={MIN_PRICE_GAP:.2f}")
    print(f"  min_whale_vol=${MIN_WHALE_VOLUME_USDC:.0f}  max_markets/window={MAX_MARKETS_PER_WINDOW}")
    print(f"  sizing: kelly={KELLY_FRACTION}  max_pos={MAX_POSITION_PCT:.0%}")
    print("=" * 78)

    end = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    oldest_ts = (end - timedelta(days=30 * args.months + 30)).timestamp()  # extra month for consensus

    print("\nFetching whales + trades ...")
    at = fetch_top_whales("all", WHALE_LEADERBOARD_LIMIT)
    rc = fetch_top_whales("30d", WHALE_LEADERBOARD_LIMIT)
    wm = {}
    for w in at + rc:
        a = w.get("proxyWallet", "").lower()
        if a:
            wm[a] = w
    whales = [w["proxyWallet"].lower() for w in wm.values()]
    print(f"  whales: {len(whales)}")
    for i, addr in enumerate(whales, 1):
        fetch_whale_trades(addr, oldest_ts)
        if i % 25 == 0:
            print(f"  ({i}/{len(whales)} cached)")

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
        bets = run_convergence(liq, whales, int(ts_start.timestamp()), int(ts_end.timestamp()))
        s = stats(bets)
        if bets:
            print(f"  trades={s.n:3d}  wr={s.win_rate:.0%}  pnl=${s.pnl:8.2f}  "
                  f"roi={s.roi_pct:+6.1f}%  sharpe={s.sharpe:5.2f}  dd={s.max_dd_pct:5.1f}%")
        else:
            print("  no trades")
        pooled.extend(bets)
        per_win.append({"window": label, "stats": s.__dict__})

    print("\n" + "=" * 78)
    print("AGGREGATE")
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


if __name__ == "__main__":
    main()
