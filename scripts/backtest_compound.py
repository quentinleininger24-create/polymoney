#!/usr/bin/env python3
"""Compound-bankroll backtest: one continuous simulation over the full period.

The other scripts reset bankroll to the initial value at the start of every
month window for comparable %ROI reporting. That's conservative -- it
HIDES the compounding that actually happens in live trading.

This script runs the smart-flow strategy (no monthly stop, aggressive
defaults) as one continuous simulation over N months. Bets accumulate
in chronological order, bankroll grows with wins and shrinks with
losses without any monthly reset. The output is the real equity curve
you'd see in live deployment, and the metrics that matter:
  - starting bankroll
  - lowest point reached (how close to ruin?)
  - highest point reached
  - ending bankroll
  - peak-to-trough drawdown
  - monthly trajectory so the user sees what the ride feels like

User's constraint: accept more variance as long as we don't "converge
to 0". This script quantifies exactly that: shows the lowest bankroll
point hit and whether recovery happens.
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
MAX_POSITION_PCT = 0.15
KELLY_FRACTION = 0.5
FEE_PCT = 0.0
SLIPPAGE_PCT = 0.005
GAS_USDC = 0.05

MIN_ENTRY_PRICE = 0.10
MAX_ENTRY_PRICE = 0.75

DOMINANCE_THRESHOLD = 0.5
MIN_WHALE_VOLUME_USDC = 2000.0
MIN_VOLUME_24HR = 1000.0
MIN_TOTAL_VOLUME = 10000.0

# Ruin guard: the live system SHOULD halt if bankroll ever drops to this
# fraction of the starting value. Here we just detect and report.
RUIN_THRESHOLD = 0.20   # 80% drawdown -> halt

WHALE_LEADERBOARD_LIMIT = 100
TRADES_PAGE_SIZE = 500
TRADES_MAX_PAGES = 30
REQUEST_DELAY_SEC = 0.1

VERTICAL_TAGS = {
    "politics": ("us-politics", "elections", "trump"),
}


def _http_json(url, params=None, retries=3):
    if params:
        url = f"{url}?{urllib.parse.urlencode(params)}"
    last = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "polymoney-compound/0.1"})
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
        except Exception:
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
    f = (b * edge_prob - (1 - edge_prob)) / b
    f = max(0.0, min(1.0, f)) * KELLY_FRACTION * mult
    f = min(f, MAX_POSITION_PCT)
    return bankroll * f


def settle(entry_price, size, won):
    eff = min(0.99, entry_price * (1 + SLIPPAGE_PCT))
    shares = size / eff
    cost = size * (1 + FEE_PCT) + GAS_USDC
    return shares - cost if won else -cost


def build_entries(markets, whales, start_ts, end_ts):
    whale_addrs = {w["proxyWallet"].lower() for w in whales if w.get("proxyWallet")}
    per_market = defaultdict(list)
    for addr in whale_addrs:
        for t in _trades_cache.get(addr, []):
            cond = t.get("conditionId")
            if not cond:
                continue
            ts = int(t.get("timestamp", 0) or 0)
            side = str(t.get("outcome", "")).strip().upper()
            if side not in {"YES", "NO"}:
                continue
            price = float(t.get("price", 0) or 0)
            notional = float(t.get("size", 0) or 0) * price
            if notional <= 0:
                continue
            per_market[cond].append((ts, side, notional, price))

    mbc = {m["conditionId"]: m for m in markets if m.get("conditionId")}
    entries = []
    for cond, trades in per_market.items():
        if cond not in mbc or not passes_liquidity(mbc[cond]):
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
            if not (start_ts <= ts <= end_ts):
                continue
            entered.add(dominant)
            entries.append({
                "market": mbc[cond], "ts": ts, "side": dominant,
                "price": price, "dominance": dom,
            })
    entries.sort(key=lambda e: e["ts"])
    return entries


@dataclass
class Bet:
    ts: int
    market_id: str
    question: str
    side: str
    entry_price: float
    size: float
    won: bool
    pnl: float
    bankroll_after: float


def simulate_compound(entries, halt_at_ruin=True):
    """One continuous simulation, NO monthly reset."""
    bankroll = INITIAL_BANKROLL
    trough = INITIAL_BANKROLL
    peak = INITIAL_BANKROLL
    peak_to_trough_dd = 0.0
    curve = [(0, INITIAL_BANKROLL, None)]  # (ts, bankroll, bet_pnl or None)
    bets = []
    ruined = False

    for e in entries:
        side_price = e["price"] if e["side"] == "YES" else 1 - e["price"]
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

        winner = resolution(e["market"])
        if winner is None:
            continue
        won = e["side"] == winner
        pnl = settle(side_price, size, won)
        bankroll += pnl

        if bankroll > peak:
            peak = bankroll
        if bankroll < trough:
            trough = bankroll
        dd_now = (peak - bankroll) / peak if peak > 0 else 0.0
        if dd_now > peak_to_trough_dd:
            peak_to_trough_dd = dd_now

        bets.append(Bet(
            ts=e["ts"], market_id=e["market"]["conditionId"],
            question=(e["market"].get("question") or "")[:80],
            side=e["side"], entry_price=side_price, size=size,
            won=won, pnl=pnl, bankroll_after=bankroll,
        ))
        curve.append((e["ts"], bankroll, pnl))

        if halt_at_ruin and bankroll <= INITIAL_BANKROLL * RUIN_THRESHOLD:
            ruined = True
            break

    return {
        "bets": bets, "curve": curve,
        "starting": INITIAL_BANKROLL, "ending": bankroll,
        "peak": peak, "trough": trough,
        "peak_to_trough_dd": peak_to_trough_dd,
        "ruined": ruined,
    }


def main():
    global INITIAL_BANKROLL, MAX_POSITION_PCT, KELLY_FRACTION
    global DOMINANCE_THRESHOLD, MIN_WHALE_VOLUME_USDC
    global MIN_VOLUME_24HR, MIN_TOTAL_VOLUME, MIN_ENTRY_PRICE, MAX_ENTRY_PRICE
    p = argparse.ArgumentParser()
    p.add_argument("--months", type=int, default=12)
    p.add_argument("--bankroll", type=float, default=INITIAL_BANKROLL)
    p.add_argument("--kelly", type=float, default=KELLY_FRACTION)
    p.add_argument("--max-pos", type=float, default=MAX_POSITION_PCT)
    p.add_argument("--dominance", type=float, default=DOMINANCE_THRESHOLD)
    args = p.parse_args()

    INITIAL_BANKROLL = args.bankroll
    KELLY_FRACTION = args.kelly
    MAX_POSITION_PCT = args.max_pos
    DOMINANCE_THRESHOLD = args.dominance

    print("=" * 78)
    print("polymoney COMPOUND backtest (aggressive smart-flow, NO monthly stop)")
    print(f"  bankroll=${INITIAL_BANKROLL}  months={args.months}  politics only")
    print(f"  dom={DOMINANCE_THRESHOLD}  kelly={KELLY_FRACTION}  max_pos={MAX_POSITION_PCT:.0%}")
    print(f"  bankroll COMPOUNDS across months -- no reset")
    print("=" * 78)

    end = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    start = end - timedelta(days=30 * args.months)
    oldest_ts = start.timestamp()

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
            print(f"  ({i}/{len(whales)} cached)")

    print("\nFetching markets ...")
    markets = []
    for v in ("politics",):
        markets.extend(fetch_markets(start, end, v))
    print(f"  resolved markets: {len(markets)}")

    entries = build_entries(markets, whales, int(start.timestamp()), int(end.timestamp()))
    print(f"  entry candidates: {len(entries)}")

    result = simulate_compound(entries)
    bets = result["bets"]

    print("\n" + "=" * 78)
    print("COMPOUND RESULT")
    print("=" * 78)
    print(f"  trades: {len(bets)}  wins: {sum(1 for b in bets if b.won)}  "
          f"wr: {sum(1 for b in bets if b.won)/len(bets):.1%}" if bets else "  no trades")
    print(f"  starting bankroll: ${result['starting']:.2f}")
    print(f"  ending bankroll:   ${result['ending']:.2f}")
    print(f"  multiple:          {result['ending']/result['starting']:.2f}x")
    print(f"  total ROI:         {(result['ending']/result['starting'] - 1)*100:+.1f}%")
    print(f"  peak:              ${result['peak']:.2f}")
    print(f"  trough:            ${result['trough']:.2f}  ({(result['trough']/result['starting'] - 1)*100:+.1f}% from start)")
    print(f"  peak-to-trough DD: {result['peak_to_trough_dd']*100:.1f}%")
    print(f"  ruined (below {RUIN_THRESHOLD*100:.0f}% of start)? {result['ruined']}")

    # Monthly breakdown: for each calendar month in the window, compute bankroll change
    print("\n  MONTHLY EQUITY CURVE")
    month_starts = {}
    for b in bets:
        dt = datetime.fromtimestamp(b.ts, tz=timezone.utc)
        key = (dt.year, dt.month)
        if key not in month_starts:
            month_starts[key] = []
        month_starts[key].append(b)

    running_br = INITIAL_BANKROLL
    last_br = INITIAL_BANKROLL
    for key in sorted(month_starts.keys()):
        month_bets = month_starts[key]
        pnl = sum(b.pnl for b in month_bets)
        running_br += pnl
        pct = (running_br / last_br - 1) * 100 if last_br > 0 else 0
        date_label = f"{key[0]}-{key[1]:02d}"
        bar = "#" * max(1, min(40, int(abs(pct) / 5)))
        sign = "+" if pct >= 0 else "-"
        print(f"    {date_label}  ${running_br:>8.2f}  {sign}{abs(pct):>5.1f}%  "
              f"n={len(month_bets):3d}  {bar}")
        last_br = running_br

    # Verdict on user's new constraint (no ruin)
    print("\nVERDICT vs user constraint (no convergence to 0):")
    if result["ruined"]:
        print(f"  RUINED at some point (bankroll fell below {RUIN_THRESHOLD*100:.0f}% of start)")
    elif result["peak_to_trough_dd"] > 0.5:
        print(f"  RISKY: {result['peak_to_trough_dd']*100:.0f}% drawdown at worst point")
    elif result["ending"] > result["starting"] * 2:
        print(f"  WORKS: +{(result['ending']/result['starting']-1)*100:.0f}% total, "
              f"max DD {result['peak_to_trough_dd']*100:.0f}%, never ruined, ended "
              f"{result['ending']/result['starting']:.1f}x starting bankroll")
    else:
        print(f"  WEAK: small net gain, check if volatility is worth it")


if __name__ == "__main__":
    main()
