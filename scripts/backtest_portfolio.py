#!/usr/bin/env python3
"""Portfolio backtest: smart-whale + smart-flow + convergence, with a
portfolio-level -15 percent monthly drawdown circuit.

Goal: always-on performance (every month fires), worst month >= -15 pct,
avg monthly >= 30 pct. No single strat gets us there alone, but three
decorrelated triggers plus a portfolio circuit should.

Allocations (of bankroll):
- smart-whale        30 pct  (high alpha per trade, rare)
- smart-flow         30 pct  (medium freq, dominance trigger)
- convergence        40 pct  (always-on near-resolution trigger)

Portfolio circuit: once portfolio month-to-date pnl drops 15 pct below
bankroll start-of-month, halt all new entries from any strat until next
month. Individual strats still enforce their own caps/stops on top.

All three strategies share the same whale trade cache and market index
so running combined is just doing each strat's filtered entries in
chronological order, sized against the shared rolling bankroll.
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
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

GAMMA = "https://gamma-api.polymarket.com"
LB = "https://lb-api.polymarket.com"
DATA = "https://data-api.polymarket.com"
CLOB = "https://clob.polymarket.com"

INITIAL_BANKROLL = 500.0
FEE_PCT = 0.0
SLIPPAGE_PCT = 0.005
GAS_USDC = 0.05

MIN_ENTRY_PRICE = 0.10
MAX_ENTRY_PRICE = 0.80

PORTFOLIO_MONTHLY_STOP_PCT = 0.15  # user's explicit worst-case floor

# Per-strategy allocations and params (locked from individual backtests)
SW_ALLOC = 0.30
SW_KELLY = 0.75
SW_MAX_POS = 0.25
SW_MIN_WHALE_USDC = 1000.0
SW_MIN_VOL24 = 2000.0
SW_MIN_TOTAL_VOL = 20000.0
SW_MIN_PRICE = 0.10
SW_MAX_PRICE = 0.75

SF_ALLOC = 0.30
SF_KELLY = 0.5
SF_MAX_POS = 0.12
SF_DOMINANCE = 0.60
SF_MIN_WHALE_VOL = 2000.0
SF_MIN_VOL24 = 1000.0
SF_MIN_TOTAL_VOL = 10000.0
SF_MONTHLY_STOP = 0.10

CV_ALLOC = 0.40
CV_KELLY = 0.5
CV_MAX_POS = 0.10
CV_ENTRY_HOURS = 48
CV_MIN_CONSENSUS = 0.50
CV_MIN_GAP = 0.10
CV_MIN_WHALE_VOL = 1000.0
CV_MIN_VOL24 = 500.0
CV_MIN_TOTAL_VOL = 5000.0

WHALE_LEADERBOARD_LIMIT = 100
TRADES_PAGE_SIZE = 500
TRADES_MAX_PAGES = 30
REQUEST_DELAY_SEC = 0.1
MAX_MARKETS_FOR_CONVERGENCE = 1200

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
            req = urllib.request.Request(url, headers={"User-Agent": "polymoney-portfolio/0.1"})
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


def liq_vol24(m):
    return _num(m.get("volume24hr"))


def liq_total(m):
    return _num(m.get("volumeNum") or m.get("volume"))


def fetch_top_whales(window, limit):
    rows = _http_json(f"{LB}/profit", {"window": window, "limit": limit})
    return rows if isinstance(rows, list) else []


_trades_cache: dict[str, list[dict]] = {}


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


_prices_cache: dict[str, list[tuple[int, float]]] = {}


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


def kelly(edge_prob, price, bankroll, kelly_frac, max_pos):
    if price <= 0 or price >= 1 or edge_prob <= price:
        return 0.0
    b = (1 - price) / price
    f = (b * edge_prob - (1 - edge_prob)) / b
    f = max(0.0, min(1.0, f)) * kelly_frac
    f = min(f, max_pos)
    return bankroll * f


def settle(entry_price, size, won):
    eff = min(0.99, entry_price * (1 + SLIPPAGE_PCT))
    shares = size / eff
    cost = size * (1 + FEE_PCT) + GAS_USDC
    return shares - cost if won else -cost


@dataclass
class Bet:
    strategy: str
    market_id: str
    entry_ts: int
    side: str
    price: float
    size: float
    won: bool
    pnl: float


# --- build entries per strategy ---

def build_smart_whale_entries(markets, whale_set, test_start_ts, test_end_ts):
    mbc = {m["conditionId"]: m for m in markets if m.get("conditionId")}
    entries = []
    for addr in whale_set:
        for t in _trades_cache.get(addr, []):
            cond = t.get("conditionId")
            if not cond or cond not in mbc:
                continue
            ts = int(t.get("timestamp", 0) or 0)
            if not (test_start_ts <= ts <= test_end_ts):
                continue
            side = str(t.get("outcome", "")).strip().upper()
            if side not in {"YES", "NO"}:
                continue
            price = float(t.get("price", 0) or 0)
            size_units = float(t.get("size", 0) or 0)
            notional = size_units * price
            if notional < SW_MIN_WHALE_USDC:
                continue
            m = mbc[cond]
            if liq_vol24(m) < SW_MIN_VOL24 and liq_total(m) < SW_MIN_TOTAL_VOL:
                continue
            side_price = price if side == "YES" else 1 - price
            if not (SW_MIN_PRICE <= side_price <= SW_MAX_PRICE):
                continue
            entries.append(("smart_whale", ts, m, side, side_price, 1.0))
    # Dedupe per (market, side): take earliest
    entries.sort(key=lambda e: e[1])
    seen = set()
    out = []
    for e in entries:
        k = (e[2]["conditionId"], e[3])
        if k in seen:
            continue
        seen.add(k)
        out.append(e)
    return out


def build_smart_flow_entries(markets, whale_set, test_start_ts, test_end_ts):
    per_market = defaultdict(list)
    for addr in whale_set:
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
            per_market[cond].append((ts, side, notional, price))

    mbc = {m["conditionId"]: m for m in markets if m.get("conditionId")}
    entries = []
    for cond, trades in per_market.items():
        if cond not in mbc:
            continue
        m = mbc[cond]
        if liq_vol24(m) < SF_MIN_VOL24 and liq_total(m) < SF_MIN_TOTAL_VOL:
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
            if vol < SF_MIN_WHALE_VOL:
                continue
            imbalance = net_yes - net_no
            dom = abs(imbalance) / vol if vol > 0 else 0.0
            if dom < SF_DOMINANCE:
                continue
            dominant = "YES" if imbalance > 0 else "NO"
            if dominant in entered:
                continue
            if not (test_start_ts <= ts <= test_end_ts):
                continue
            entered.add(dominant)
            side_price = price if dominant == "YES" else 1 - price
            if not (MIN_ENTRY_PRICE <= side_price <= MAX_ENTRY_PRICE):
                continue
            mult = 1.0 + (dom - SF_DOMINANCE) * 2.0
            mult = max(0.5, min(2.0, mult))
            entries.append(("smart_flow", ts, m, dominant, side_price, mult))
    entries.sort(key=lambda e: e[1])
    return entries


def build_convergence_entries(markets, whale_set, test_start_ts, test_end_ts):
    entries = []
    scheduled = []
    for m in markets[:MAX_MARKETS_FOR_CONVERGENCE]:
        end_d = _parse_iso(m.get("endDate"))
        token = extract_yes_token(m)
        if not end_d or not token:
            continue
        entry_ts = int(end_d.timestamp()) - CV_ENTRY_HOURS * 3600
        if not (test_start_ts <= entry_ts <= test_end_ts):
            continue
        if liq_vol24(m) < CV_MIN_VOL24 and liq_total(m) < CV_MIN_TOTAL_VOL:
            continue
        scheduled.append((entry_ts, m, token))
    scheduled.sort(key=lambda r: r[0])

    for entry_ts, m, token in scheduled:
        net_yes = 0.0
        net_no = 0.0
        vol = 0.0
        for addr in whale_set:
            for t in _trades_cache.get(addr, []):
                if t.get("conditionId") != m["conditionId"]:
                    continue
                ts = int(t.get("timestamp", 0) or 0)
                if ts >= entry_ts:
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
                vol += notional
        if vol < CV_MIN_WHALE_VOL:
            continue
        consensus = (net_yes - net_no) / vol
        if abs(consensus) < CV_MIN_CONSENSUS:
            continue
        yes_price = price_at(token, entry_ts)
        if yes_price is None:
            continue
        implied = 0.5 + 0.25 * consensus
        gap = implied - yes_price
        if abs(gap) < CV_MIN_GAP:
            continue
        direction = "YES" if gap > 0 else "NO"
        side_price = yes_price if direction == "YES" else 1 - yes_price
        if not (MIN_ENTRY_PRICE <= side_price <= MAX_ENTRY_PRICE):
            continue
        gap_mult = 1.0 + abs(gap) * 4.0
        entries.append(("convergence", entry_ts, m, direction, side_price, gap_mult))
    entries.sort(key=lambda e: e[1])
    return entries


# --- portfolio simulator ---

def run_portfolio(all_entries, bankroll_start):
    bets: list[Bet] = []
    bankroll = bankroll_start
    # Per-strategy MTD tracking and internal monthly stops
    mtd = defaultdict(float)   # strat -> pnl this month
    portfolio_mtd = 0.0
    current_month_key = None
    halted_strats: set[str] = set()
    portfolio_halted = False

    params = {
        "smart_whale": (SW_KELLY, SW_MAX_POS, SW_ALLOC, None),          # no per-strat stop
        "smart_flow": (SF_KELLY, SF_MAX_POS, SF_ALLOC, SF_MONTHLY_STOP),
        "convergence": (CV_KELLY, CV_MAX_POS, CV_ALLOC, None),
    }

    # Track per-strategy capital used (open positions settle immediately at
    # resolution in backtest, so track via allocation budget vs cumulative
    # pnl -- a simpler proxy since we don't hold overlapping positions).
    alloc_used = defaultdict(float)  # strat -> sum(size of current "open" bets)

    for e in all_entries:
        strat, ts, m, side, side_price, mult = e
        dt = datetime.fromtimestamp(ts, tz=timezone.utc)
        month_key = (dt.year, dt.month)
        if month_key != current_month_key:
            current_month_key = month_key
            mtd.clear()
            portfolio_mtd = 0.0
            halted_strats = set()
            portfolio_halted = False

        if portfolio_halted:
            continue
        if strat in halted_strats:
            continue

        kelly_f, max_pos, alloc, per_strat_stop = params[strat]
        edge_bonus = 0.05 + (mult - 1.0) * 0.03
        edge_prob = min(0.95, side_price + edge_bonus)
        # Strategy sees its own alloc-adjusted bankroll
        strat_bankroll = bankroll * alloc
        size = kelly(edge_prob, side_price, strat_bankroll, kelly_f, max_pos)
        size *= mult
        size = min(size, strat_bankroll - GAS_USDC, bankroll - GAS_USDC)
        if size < 1.0:
            continue

        winner = resolution(m)
        if winner is None:
            continue
        won = side == winner
        pnl = settle(side_price, size, won)
        bankroll += pnl
        mtd[strat] += pnl
        portfolio_mtd += pnl
        bets.append(Bet(strategy=strat, market_id=m["conditionId"],
                        entry_ts=ts, side=side, price=side_price,
                        size=size, won=won, pnl=pnl))
        if per_strat_stop is not None:
            if mtd[strat] <= -per_strat_stop * bankroll_start:
                halted_strats.add(strat)
        if portfolio_mtd <= -PORTFOLIO_MONTHLY_STOP_PCT * bankroll_start:
            portfolio_halted = True
        if bankroll <= 1:
            break
    return bets


# --- stats ---

@dataclass
class Stats:
    n: int
    wins: int
    win_rate: float
    pnl: float
    roi_pct: float
    sharpe: float
    max_dd_pct: float


def stats(bets, initial):
    if not bets:
        return Stats(0, 0, 0, 0, 0, 0, 0)
    wins = sum(1 for b in bets if b.won)
    pnl = sum(b.pnl for b in bets)
    br = initial
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
    return Stats(len(bets), wins, wins / len(bets), pnl, pnl / initial * 100, sh, mdd * 100)


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
    global INITIAL_BANKROLL, PORTFOLIO_MONTHLY_STOP_PCT
    p = argparse.ArgumentParser()
    p.add_argument("--months", type=int, default=12)
    p.add_argument("--bankroll", type=float, default=INITIAL_BANKROLL)
    p.add_argument("--verticals", default="politics")
    p.add_argument("--monthly-stop", type=float, default=PORTFOLIO_MONTHLY_STOP_PCT)
    p.add_argument("--max-markets", type=int, default=MAX_MARKETS_FOR_CONVERGENCE)
    args = p.parse_args()

    INITIAL_BANKROLL = args.bankroll
    PORTFOLIO_MONTHLY_STOP_PCT = args.monthly_stop

    verticals = [v.strip() for v in args.verticals.split(",") if v.strip()]

    print("=" * 78)
    print("polymoney PORTFOLIO backtest (smart_whale + smart_flow + convergence)")
    print(f"  bankroll=${INITIAL_BANKROLL}  months={args.months}  verticals={verticals}")
    print(f"  allocations: sw={SW_ALLOC:.0%}  sf={SF_ALLOC:.0%}  cv={CV_ALLOC:.0%}")
    print(f"  portfolio monthly stop: {PORTFOLIO_MONTHLY_STOP_PCT:.0%}")
    print(f"  per-strat monthly stops: sw=none  sf={SF_MONTHLY_STOP:.0%}  cv=none")
    print("=" * 78)

    end = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    oldest_ts = (end - timedelta(days=30 * args.months + 30)).timestamp()

    print("\nFetching whales + trades ...")
    at = fetch_top_whales("all", WHALE_LEADERBOARD_LIMIT)
    rc = fetch_top_whales("30d", WHALE_LEADERBOARD_LIMIT)
    wm = {}
    for w in at + rc:
        a = w.get("proxyWallet", "").lower()
        if a:
            wm[a] = w
    whale_addrs = [w["proxyWallet"].lower() for w in wm.values()]
    print(f"  whales: {len(whale_addrs)}")
    for i, addr in enumerate(whale_addrs, 1):
        fetch_whale_trades(addr, oldest_ts)
        if i % 25 == 0:
            print(f"  ({i}/{len(whale_addrs)} cached)")

    all_bets: list[Bet] = []
    per_win: list[dict] = []

    for i in range(args.months, 0, -1):
        ts_start = end - timedelta(days=30 * i)
        ts_end = end - timedelta(days=30 * (i - 1))
        label = f"{ts_start.date()} -> {ts_end.date()}"
        print(f"\n[Window {args.months - i + 1}/{args.months}] {label}")
        markets = []
        for v in verticals:
            markets.extend(fetch_markets(ts_start, ts_end, v))

        sw_e = build_smart_whale_entries(markets, whale_addrs, int(ts_start.timestamp()), int(ts_end.timestamp()))
        sf_e = build_smart_flow_entries(markets, whale_addrs, int(ts_start.timestamp()), int(ts_end.timestamp()))
        cv_e = build_convergence_entries(markets, whale_addrs, int(ts_start.timestamp()), int(ts_end.timestamp()))
        print(f"  entries: sw={len(sw_e)}  sf={len(sf_e)}  cv={len(cv_e)}")

        merged = sw_e + sf_e + cv_e
        merged.sort(key=lambda x: x[1])

        # Reset bankroll per window for clean monthly ROI reporting
        bets = run_portfolio(merged, INITIAL_BANKROLL)
        s = stats(bets, INITIAL_BANKROLL)
        print(f"  trades={s.n:3d}  wr={s.win_rate:.0%}  pnl=${s.pnl:8.2f}  "
              f"roi={s.roi_pct:+6.1f}%  sharpe={s.sharpe:5.2f}  dd={s.max_dd_pct:5.1f}%")
        per_strat_pnl = defaultdict(float)
        per_strat_n = defaultdict(int)
        for b in bets:
            per_strat_pnl[b.strategy] += b.pnl
            per_strat_n[b.strategy] += 1
        for strat in ("smart_whale", "smart_flow", "convergence"):
            if per_strat_n[strat]:
                print(f"    {strat:12}  n={per_strat_n[strat]:3d}  pnl=${per_strat_pnl[strat]:+.2f}")
        all_bets.extend(bets)
        per_win.append({"window": label, "stats": s.__dict__})

    print("\n" + "=" * 78)
    print("AGGREGATE (monthly bankroll resets -- pooled bets stats)")
    print("=" * 78)
    s = stats(all_bets, INITIAL_BANKROLL)
    bs = bootstrap(all_bets)
    active = [w for w in per_win if w["stats"]["n"] > 0]
    consistency = len(active) / len(per_win) if per_win else 0.0
    avg_m = sum(w["stats"]["roi_pct"] for w in active) / len(active) if active else 0.0
    worst = min((w["stats"]["roi_pct"] for w in active), default=0.0)
    best = max((w["stats"]["roi_pct"] for w in active), default=0.0)
    print(f"  n={s.n}  wr={s.win_rate:.1%}  sharpe={s.sharpe:.2f}  max_dd={s.max_dd_pct:.1f}%")
    print(f"  P(+)={bs['p_positive']:.0%}  "
          f"CI95=[${bs['ci_lo']:.2f}, ${bs['ci_hi']:.2f}]")
    print(f"  active months: {len(active)}/{len(per_win)} ({consistency:.0%})")
    print(f"  monthly: avg {avg_m:+.1f}%  worst {worst:+.1f}%  best {best:+.1f}%")

    print("\nVERDICT vs user targets (avg >=40%/mo, worst >=-15%, always-on):")
    if avg_m >= 40 and worst >= -15 and consistency >= 0.95:
        print(f"  ALL TARGETS HIT")
    elif avg_m >= 30 and worst >= -15 and consistency >= 0.85:
        print(f"  CLOSE: {avg_m:+.1f}%/mo {worst:+.1f}% worst {consistency:.0%} active")
    else:
        print(f"  SHORT OF TARGETS: {avg_m:+.1f}%/mo  worst {worst:+.1f}%  {consistency:.0%} active")


if __name__ == "__main__":
    main()
