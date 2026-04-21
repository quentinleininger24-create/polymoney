#!/usr/bin/env python3
"""Anomaly Hunter backtest.

Thesis: a sudden >X% price move on a political market within 1 hour is almost
never random. Either:
- news dropped that we haven't seen yet (and the market will continue moving),
- insider info leaked (a wallet knows the outcome and is positioning),
- whale took a big position (smart money).

Strategy candidates tested here:
1. momentum       : follow the move (enter same side right after the jump)
2. mean_reversion : fade the move (enter opposite side, bet on overshoot)
3. longshot       : only enter if pre-anomaly price < 0.20 and move is up
                    (asymmetric payoff - small win rate needed to be +EV)

Walk-forward 6-9 months, 1-month windows. No DB required.
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
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

GAMMA = "https://gamma-api.polymarket.com"
CLOB = "https://clob.polymarket.com"

INITIAL_BANKROLL = 100.0
MAX_POSITION_PCT = 0.10
KELLY_FRACTION = 0.5
FEE_PCT = 0.0
SLIPPAGE_PCT = 0.005
GAS_USDC = 0.05

MIN_ENTRY_PRICE = 0.05
MAX_ENTRY_PRICE = 0.85

# Anomaly detection params
ANOMALY_THRESHOLD = 0.15  # 15% price move in 1h window = anomaly
MIN_HOURS_BEFORE_RESOLUTION = 6  # don't enter too close to resolution
MAX_MARKETS_PER_WINDOW = 800

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
            req = urllib.request.Request(url, headers={"User-Agent": "polymoney-anomaly/0.1"})
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


def extract_yes_token(m: dict) -> str | None:
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


def fetch_price_history(token_id: str) -> list[tuple[int, float]]:
    try:
        data = _http_json(f"{CLOB}/prices-history", {
            "market": token_id, "interval": "max", "fidelity": 60,
        })
    except Exception:
        return []
    pts = data.get("history", []) if isinstance(data, dict) else []
    out: list[tuple[int, float]] = []
    for p in pts:
        try:
            out.append((int(p["t"]), float(p["p"])))
        except (KeyError, ValueError, TypeError):
            continue
    out.sort()
    return out


@dataclass
class Anomaly:
    ts: int
    prev_price: float
    new_price: float
    move: float
    direction: str  # YES or NO (which side moved favorably)


def detect_anomalies(history: list[tuple[int, float]], threshold: float) -> list[Anomaly]:
    out: list[Anomaly] = []
    for i in range(1, len(history)):
        prev_ts, prev_p = history[i - 1]
        ts, p = history[i]
        move = p - prev_p
        if abs(move) >= threshold:
            out.append(Anomaly(
                ts=ts,
                prev_price=prev_p,
                new_price=p,
                move=move,
                direction="YES" if move > 0 else "NO",
            ))
    return out


def kelly_size(edge_prob: float, price: float, bankroll: float) -> float:
    if price <= 0 or price >= 1 or edge_prob <= price:
        return 0.0
    b = (1 - price) / price
    p = edge_prob
    q = 1 - p
    f = (b * p - q) / b
    f = max(0.0, min(1.0, f)) * KELLY_FRACTION
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
    mode: str
    entry_price: float
    size: float
    won: bool
    pnl: float


def run_strategy(
    mode: str,
    markets: list[dict],
    test_start_ts: int,
    test_end_ts: int,
) -> list[SimBet]:
    bets: list[SimBet] = []
    bankroll = INITIAL_BANKROLL
    for m in markets[:MAX_MARKETS_PER_WINDOW]:
        yes_token = extract_yes_token(m)
        winner = market_resolution(m)
        if not yes_token or winner is None:
            continue
        end_ts = int(_parse_iso(m.get("endDate")).timestamp()) if _parse_iso(m.get("endDate")) else 0
        history = fetch_price_history(yes_token)
        if not history:
            continue

        anomalies = detect_anomalies(history, ANOMALY_THRESHOLD)
        # Only consider anomalies during the test window and not too close to resolution
        anomalies = [
            a for a in anomalies
            if test_start_ts <= a.ts <= test_end_ts
            and end_ts - a.ts > MIN_HOURS_BEFORE_RESOLUTION * 3600
        ]
        if not anomalies:
            continue
        # First anomaly only (front-run the wave)
        anomaly = anomalies[0]

        if mode == "momentum":
            direction = anomaly.direction
            entry_yes = anomaly.new_price
        elif mode == "mean_reversion":
            direction = "NO" if anomaly.direction == "YES" else "YES"
            entry_yes = anomaly.new_price
        elif mode == "longshot":
            # Only trade when pre-anomaly was a longshot that spiked up
            if anomaly.prev_price > 0.20 or anomaly.move < 0:
                continue
            direction = "YES"
            entry_yes = anomaly.new_price
        else:
            raise ValueError(f"unknown mode: {mode}")

        entry_side = entry_yes if direction == "YES" else 1 - entry_yes
        if entry_side < MIN_ENTRY_PRICE or entry_side > MAX_ENTRY_PRICE:
            continue

        # Edge estimate: momentum assumes the move will continue,
        # so true prob is higher than entry price (e.g. + 8%).
        if mode == "mean_reversion":
            edge_prob = min(0.95, entry_side + 0.05)
        else:
            edge_prob = min(0.95, entry_side + 0.08)

        size = kelly_size(edge_prob, entry_side, bankroll)
        if size < 1.0:
            continue
        size = min(size, bankroll - GAS_USDC)
        if size < 1.0:
            continue

        won = direction == winner
        pnl = settle(entry_side, size, won)
        bankroll += pnl
        bets.append(SimBet(
            market_id=m["conditionId"],
            question=(m.get("question") or "")[:80],
            mode=mode,
            entry_price=entry_side,
            size=size,
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
    returns = [b.pnl / b.size for b in bets if b.size > 0]
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
    global ANOMALY_THRESHOLD, MAX_MARKETS_PER_WINDOW, MIN_ENTRY_PRICE, MAX_ENTRY_PRICE
    p = argparse.ArgumentParser()
    p.add_argument("--months", type=int, default=6)
    p.add_argument("--bankroll", type=float, default=INITIAL_BANKROLL)
    p.add_argument("--verticals", default="politics")
    p.add_argument("--kelly", type=float, default=KELLY_FRACTION)
    p.add_argument("--max-pos", type=float, default=MAX_POSITION_PCT)
    p.add_argument("--threshold", type=float, default=ANOMALY_THRESHOLD, help="price move threshold")
    p.add_argument("--min-price", type=float, default=MIN_ENTRY_PRICE)
    p.add_argument("--max-price", type=float, default=MAX_ENTRY_PRICE)
    p.add_argument("--max-markets", type=int, default=MAX_MARKETS_PER_WINDOW)
    p.add_argument("--modes", default="momentum,mean_reversion,longshot")
    args = p.parse_args()

    INITIAL_BANKROLL = args.bankroll
    KELLY_FRACTION = args.kelly
    MAX_POSITION_PCT = args.max_pos
    ANOMALY_THRESHOLD = args.threshold
    MIN_ENTRY_PRICE = args.min_price
    MAX_ENTRY_PRICE = args.max_price
    MAX_MARKETS_PER_WINDOW = args.max_markets

    verticals = [v.strip() for v in args.verticals.split(",") if v.strip()]
    modes = [m.strip() for m in args.modes.split(",") if m.strip()]

    print("=" * 78)
    print("polymoney anomaly hunter backtest")
    print(f"  bankroll=${args.bankroll}  windows={args.months}  verticals={verticals}")
    print(f"  threshold={ANOMALY_THRESHOLD:.0%} price move in 1h  modes={modes}")
    print(f"  kelly={KELLY_FRACTION}  max_pos={MAX_POSITION_PCT:.0%}  "
          f"price=[{MIN_ENTRY_PRICE:.2f},{MAX_ENTRY_PRICE:.2f}]")
    print("=" * 78)

    end = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    pooled: dict[str, list[SimBet]] = {m: [] for m in modes}

    for i in range(args.months, 0, -1):
        ts_start = end - timedelta(days=30 * i)
        ts_end = end - timedelta(days=30 * (i - 1))
        label = f"{ts_start.date()} -> {ts_end.date()}"
        print(f"\n[Window {args.months - i + 1}/{args.months}] {label}")
        markets: list[dict] = []
        for v in verticals:
            markets.extend(fetch_markets_for_vertical(ts_start, ts_end, v))
        print(f"  resolved markets: {len(markets)} (capped to {MAX_MARKETS_PER_WINDOW})")

        for mode in modes:
            bets = run_strategy(mode, markets, int(ts_start.timestamp()), int(ts_end.timestamp()))
            s = compute_stats(bets)
            print(f"  {mode:14}  trades={s.n:3d}  wr={s.win_rate:.0%}  "
                  f"pnl=${s.pnl:7.2f}  roi={s.roi_pct:+6.1f}%  sharpe={s.sharpe:5.2f}  "
                  f"dd={s.max_dd_pct:5.1f}%")
            pooled[mode].extend(bets)

    print("\n" + "=" * 78)
    print("AGGREGATE")
    print("=" * 78)
    for mode, bets in pooled.items():
        s = compute_stats(bets)
        bs = bootstrap(bets)
        print(f"  {mode:14}  n={s.n:3d}  wr={s.win_rate:.1%}  pnl=${s.pnl:8.2f}  "
              f"sharpe={s.sharpe:5.2f}  median=${bs['median']:+7.2f}  "
              f"P(+)={bs['p_positive']:.0%}  CI95=[${bs['ci_lo']:7.2f}, ${bs['ci_hi']:7.2f}]")


if __name__ == "__main__":
    main()
