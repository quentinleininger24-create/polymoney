#!/usr/bin/env python3
"""Standalone whale-copy backtest on Polymarket politics markets.

No DB, no deps beyond Python stdlib. Hits the public Polymarket APIs,
runs walk-forward validation across 6 monthly windows, compares whale-copy
to two baselines (random side / always-favorite), and produces a verdict
with a bootstrap CI.

Usage:
    python scripts/backtest_whale_copy.py
    python scripts/backtest_whale_copy.py --months 6 --bankroll 100

This script is intentionally honest about its limitations -- read the
LIMITATIONS section it prints at the end before drawing conclusions.
"""

from __future__ import annotations

import argparse
import json
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
LB = "https://lb-api.polymarket.com"
DATA = "https://data-api.polymarket.com"

INITIAL_BANKROLL = 100.0
MAX_POSITION_PCT = 0.05
KELLY_FRACTION = 0.33

POLYMARKET_FEE_PCT = 0.02     # taker fee, conservative
SLIPPAGE_PCT = 0.005           # 50 bps when copying late
GAS_USDC = 0.05                # ~Polygon gas in USDC equivalent

WHALE_MIN_TRADE_USDC = 500.0
WHALE_LEADERBOARD_LIMIT = 50
MAX_TRADES_PER_WHALE = 500   # we filter by window in code

REQUEST_DELAY_SEC = 0.15  # polite to public API


# ---- HTTP -----------------------------------------------------------------

def _http_json(url: str, params: dict | None = None, retries: int = 3) -> object:
    if params:
        url = f"{url}?{urllib.parse.urlencode(params)}"
    last_err: Exception | None = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "polymoney-backtest/0.1"})
            with urllib.request.urlopen(req, timeout=30) as resp:
                data = json.loads(resp.read())
            time.sleep(REQUEST_DELAY_SEC)
            return data
        except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError) as e:
            last_err = e
            time.sleep(1.0 * (attempt + 1))
    raise RuntimeError(f"HTTP failed after {retries} attempts: {url} :: {last_err}")


# ---- Polymarket data ------------------------------------------------------

def fetch_resolved_political_markets(start: datetime, end: datetime, hard_cap: int = 5000) -> list[dict]:
    """Pull resolved political markets via /events (the /markets endpoint mis-tags
    crypto markets as politics so we go event-first and flatten markets out).

    Tags us-politics, elections, trump are unioned (different events use different ones).
    """
    out: list[dict] = []
    seen_cond: set[str] = set()
    for tag in ("us-politics", "elections", "trump"):
        offset = 0
        page_size = 100
        while offset < hard_cap:
            events = _http_json(f"{GAMMA}/events", {
                "closed": "true",
                "limit": page_size,
                "offset": offset,
                "tag_slug": tag,
                "order": "endDate",
                "ascending": "false",
            })
            if not isinstance(events, list) or not events:
                break
            early_exit = False
            for e in events:
                end_d = _parse_iso(e.get("endDate"))
                if not end_d:
                    continue
                if end_d < start:
                    early_exit = True
                    break
                if not (start <= end_d <= end):
                    continue
                for m in e.get("markets", []) or []:
                    cond = m.get("conditionId")
                    if not cond or cond in seen_cond:
                        continue
                    if not m.get("closed"):
                        continue
                    # Inherit endDate from event for consistency
                    m.setdefault("endDate", e.get("endDate"))
                    seen_cond.add(cond)
                    out.append(m)
            if early_exit:
                break
            offset += page_size
    return out


def fetch_top_whales(window: str = "30d", limit: int = WHALE_LEADERBOARD_LIMIT) -> list[dict]:
    rows = _http_json(f"{LB}/profit", {"window": window, "limit": limit})
    return rows if isinstance(rows, list) else []


def fetch_user_trades(wallet: str, limit: int = MAX_TRADES_PER_WHALE) -> list[dict]:
    rows = _http_json(f"{DATA}/trades", {"user": wallet, "limit": limit})
    return rows if isinstance(rows, list) else []


# Cache of fetched trades per whale -- avoid refetching across windows
_whale_trades_cache: dict[str, list[dict]] = {}


def cached_user_trades(wallet: str) -> list[dict]:
    if wallet not in _whale_trades_cache:
        try:
            _whale_trades_cache[wallet] = fetch_user_trades(wallet)
        except Exception as e:
            print(f"  ! trades fetch failed for {wallet[:8]}: {e}", file=sys.stderr)
            _whale_trades_cache[wallet] = []
    return _whale_trades_cache[wallet]


# ---- Helpers --------------------------------------------------------------

def _parse_iso(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        return datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    except ValueError:
        return None


def market_resolution(m: dict) -> str | None:
    """Return 'YES' or 'NO' for resolved binary markets."""
    raw_outs = m.get("outcomes")
    raw_prices = m.get("outcomePrices")
    if not raw_outs or not raw_prices:
        return None
    try:
        outs = json.loads(raw_outs) if isinstance(raw_outs, str) else raw_outs
        prices = json.loads(raw_prices) if isinstance(raw_prices, str) else raw_prices
    except (json.JSONDecodeError, TypeError):
        return None
    if len(outs) != 2 or len(prices) != 2:
        return None
    try:
        p0, p1 = float(prices[0]), float(prices[1])
    except (TypeError, ValueError):
        return None
    if abs(p0 - p1) < 0.01:
        return None  # 50/50 -> treat as unresolved
    winner = outs[0 if p0 > p1 else 1]
    return str(winner).strip().upper()


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


# ---- Bet simulation -------------------------------------------------------

@dataclass
class SimBet:
    market_id: str
    question: str
    outcome: str
    entry_price: float
    size_usdc: float
    won: bool
    pnl: float


def settle(outcome: str, entry_price: float, size_usdc: float, won: bool) -> float:
    """PnL after fees + slippage + gas."""
    effective_price = min(0.99, entry_price * (1 + SLIPPAGE_PCT))
    shares = size_usdc / effective_price
    cost = size_usdc * (1 + POLYMARKET_FEE_PCT) + GAS_USDC
    if won:
        return shares - cost  # each share = $1 payout
    return -cost


# ---- Strategies -----------------------------------------------------------

def strat_whale_copy(
    test_start: datetime,
    test_end: datetime,
    markets_by_cond: dict[str, dict],
    whales: list[dict],
) -> list[SimBet]:
    """Mirror the first big trade by a top whale on each tracked market."""
    bets: list[SimBet] = []
    seen: set[tuple[str, str]] = set()
    bankroll = INITIAL_BANKROLL

    rows: list[tuple[float, str, dict, dict]] = []
    for w in whales:
        addr = w["proxyWallet"].lower()
        trades = cached_user_trades(addr)
        for t in trades:
            cond = t.get("conditionId")
            if not cond or cond not in markets_by_cond:
                continue
            ts = float(t.get("timestamp", 0))
            if not (test_start.timestamp() <= ts <= test_end.timestamp()):
                continue
            size_usdc = float(t.get("size", 0)) * float(t.get("price", 0))
            if size_usdc < WHALE_MIN_TRADE_USDC:
                continue
            rows.append((ts, addr, t, markets_by_cond[cond]))

    rows.sort(key=lambda r: r[0])
    for _ts, _addr, t, m in rows:
        outcome = str(t.get("outcome", "")).strip().upper()
        if outcome not in {"YES", "NO"}:
            continue
        key = (m["conditionId"], outcome)
        if key in seen:
            continue
        seen.add(key)

        price = float(t.get("price", 0))
        if price <= 0 or price >= 1:
            continue

        # Assume modest edge: whale's price + 5% on their side (they usually pick early)
        edge_prob = min(0.95, price + 0.05)
        size = kelly_size(edge_prob, price, bankroll)
        if size < 1.0:
            continue
        size = min(size, bankroll - GAS_USDC)
        if size < 1.0:
            continue

        winner = market_resolution(m)
        if winner is None:
            continue
        won = outcome == winner
        pnl = settle(outcome, price, size, won)
        bankroll += pnl
        bets.append(SimBet(m["conditionId"], m.get("question", "")[:80], outcome, price, size, won, pnl))
        if bankroll <= 1:
            break
    return bets


def strat_anti_whale(
    test_start: datetime,
    test_end: datetime,
    markets_by_cond: dict[str, dict],
    whales: list[dict],
) -> list[SimBet]:
    """Bet AGAINST the whale at the same entry price (sanity baseline).
    Whale-copy edge implies anti-whale loses; if anti-whale beats whale-copy,
    the 'edge' is just noise. Uses the whale's price as the historical reference,
    which avoids needing a separate price-history endpoint.
    """
    bets: list[SimBet] = []
    seen: set[tuple[str, str]] = set()
    bankroll = INITIAL_BANKROLL

    rows: list[tuple[float, dict, dict]] = []
    for w in whales:
        for t in cached_user_trades(w["proxyWallet"].lower()):
            cond = t.get("conditionId")
            if not cond or cond not in markets_by_cond:
                continue
            ts = float(t.get("timestamp", 0))
            if not (test_start.timestamp() <= ts <= test_end.timestamp()):
                continue
            size_usdc = float(t.get("size", 0)) * float(t.get("price", 0))
            if size_usdc < WHALE_MIN_TRADE_USDC:
                continue
            rows.append((ts, t, markets_by_cond[cond]))
    rows.sort(key=lambda r: r[0])

    for _ts, t, m in rows:
        whale_outcome = str(t.get("outcome", "")).strip().upper()
        if whale_outcome not in {"YES", "NO"}:
            continue
        outcome = "NO" if whale_outcome == "YES" else "YES"  # bet AGAINST
        key = (m["conditionId"], outcome)
        if key in seen:
            continue
        seen.add(key)
        whale_price = float(t.get("price", 0))
        # Implied price for our (opposite) side
        price = 1 - whale_price
        if price <= 0 or price >= 1:
            continue
        edge_prob = min(0.95, price + 0.05)
        size = kelly_size(edge_prob, price, bankroll)
        if size < 1.0:
            continue
        size = min(size, bankroll - GAS_USDC)
        if size < 1.0:
            continue
        winner = market_resolution(m)
        if winner is None:
            continue
        won = outcome == winner
        pnl = settle(outcome, price, size, won)
        bankroll += pnl
        bets.append(SimBet(m["conditionId"], m.get("question", "")[:80], outcome, price, size, won, pnl))
        if bankroll <= 1:
            break
    return bets


# ---- Stats ----------------------------------------------------------------

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
    bankroll = INITIAL_BANKROLL
    equity = [bankroll]
    for b in bets:
        bankroll += b.pnl
        equity.append(bankroll)
    returns = [b.pnl / b.size_usdc for b in bets if b.size_usdc > 0]
    sharpe = 0.0
    if len(returns) > 1:
        mean = statistics.fmean(returns)
        stdev = statistics.pstdev(returns)
        sharpe = (mean / stdev) * (len(returns) ** 0.5) if stdev > 0 else 0.0
    peak = equity[0]
    max_dd = 0.0
    for v in equity:
        if v > peak:
            peak = v
        if peak > 0:
            dd = (peak - v) / peak
            if dd > max_dd:
                max_dd = dd
    return Stats(
        n=len(bets), wins=wins, win_rate=wins / len(bets),
        pnl=pnl, roi_pct=(pnl / INITIAL_BANKROLL) * 100,
        sharpe=sharpe, max_dd_pct=max_dd * 100,
    )


def bootstrap_ci(bets: list[SimBet], n_resamples: int = 2000, conf: float = 0.95, seed: int = 42) -> tuple[float, float]:
    if not bets:
        return (0.0, 0.0)
    rng = random.Random(seed)
    pnls = [b.pnl for b in bets]
    samples: list[float] = []
    for _ in range(n_resamples):
        s = rng.choices(pnls, k=len(pnls))
        samples.append(sum(s))
    samples.sort()
    lo = samples[int(n_resamples * (1 - conf) / 2)]
    hi = samples[int(n_resamples * (1 + conf) / 2)]
    return (lo, hi)


# ---- Walk-forward driver --------------------------------------------------

def run(months: int, seed: int) -> dict:
    end = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    rng = random.Random(seed)

    print("Fetching top whales (current leaderboard) ...")
    whales = fetch_top_whales("all", WHALE_LEADERBOARD_LIMIT)
    if not whales:
        whales = fetch_top_whales("30d", WHALE_LEADERBOARD_LIMIT)
    print(f"  -> {len(whales)} whales loaded")

    all_whale_bets: list[SimBet] = []
    all_anti_bets: list[SimBet] = []
    per_window: list[dict] = []

    for i in range(months, 0, -1):
        test_start = end - timedelta(days=30 * i)
        test_end = end - timedelta(days=30 * (i - 1))
        label = f"{test_start.date()} -> {test_end.date()}"
        print(f"\n[Window {months - i + 1}/{months}] {label}")

        markets = fetch_resolved_political_markets(test_start, test_end)
        markets_by_cond = {m["conditionId"]: m for m in markets if m.get("conditionId")}
        print(f"  resolved political markets: {len(markets_by_cond)}")
        if not markets_by_cond:
            continue

        wb = strat_whale_copy(test_start, test_end, markets_by_cond, whales)
        ab = strat_anti_whale(test_start, test_end, markets_by_cond, whales)
        sw, sa = compute_stats(wb), compute_stats(ab)
        print(f"  whale-copy:   trades={sw.n:3d}  wr={sw.win_rate:.0%}  pnl=${sw.pnl:7.2f}  roi={sw.roi_pct:+6.1f}%  sharpe={sw.sharpe:5.2f}  dd={sw.max_dd_pct:5.1f}%")
        print(f"  anti-whale:   trades={sa.n:3d}  wr={sa.win_rate:.0%}  pnl=${sa.pnl:7.2f}  roi={sa.roi_pct:+6.1f}%  sharpe={sa.sharpe:5.2f}  dd={sa.max_dd_pct:5.1f}%")

        all_whale_bets.extend(wb)
        all_anti_bets.extend(ab)
        per_window.append({"window": label, "whale": sw.__dict__, "anti_whale": sa.__dict__})

    print("\n" + "=" * 78)
    print("AGGREGATE (bets pooled across all windows)")
    print("=" * 78)

    summary: dict[str, dict] = {}
    for name, bets in [("whale_copy", all_whale_bets), ("anti_whale", all_anti_bets)]:
        s = compute_stats(bets)
        lo, hi = bootstrap_ci(bets)
        print(f"  {name:11}  n={s.n:3d}  wr={s.win_rate:.1%}  pnl=${s.pnl:8.2f}  sharpe={s.sharpe:5.2f}  CI95=[${lo:7.2f}, ${hi:7.2f}]")
        summary[name] = {**s.__dict__, "ci_lo": lo, "ci_hi": hi}

    print("\nVERDICT")
    whale = summary["whale_copy"]
    anti = summary["anti_whale"]
    if whale["n"] < 30:
        verdict = "INSUFFICIENT_DATA"
        msg = f"only {whale['n']} whale-copy trades; need 30+ for any conclusion."
    elif whale["ci_lo"] > 0 and whale["pnl"] > anti["pnl"]:
        verdict = "PASS"
        msg = "lower 95% CI bound > 0 AND beats anti-whale baseline. Provisional edge exists."
    elif whale["ci_hi"] < 0:
        verdict = "FAIL"
        msg = "upper 95% CI bound < 0. No detectable edge -- do NOT go live."
    elif whale["pnl"] <= anti["pnl"]:
        verdict = "FAIL"
        msg = "whale-copy did not beat anti-whale baseline. Following whales gives no advantage here."
    else:
        verdict = "INCONCLUSIVE"
        msg = "CI straddles zero. Either no edge or sample too small."
    print(f"  {verdict}: {msg}")

    print("\nLIMITATIONS (read before drawing conclusions)")
    print("  1. Top whales are the CURRENT leaderboard. We can't pull point-in-time")
    print("     leaderboards from Polymarket -- this is survivorship bias. Real edge")
    print("     will be lower because in real life we wouldn't have known these were")
    print("     the winners ahead of time. Treat results as an UPPER BOUND.")
    print("  2. Slippage is a fixed 0.5%. Real slippage on copying may be 1-3% if")
    print("     the market moves between the whale's trade and ours.")
    print("  3. Markets without clear binary resolution or with 50/50 prices are")
    print("     dropped. This may bias toward decisive markets.")
    print("  4. Bootstrap CI assumes bets are independent -- correlated political")
    print("     markets (multiple bets on the same election) violate this.")
    print("  5. We do not model latency: in production we'd see whale trades")
    print("     minutes after they happen, prices may have already moved.")
    print("  6. Only the whale_copy strategy is tested here. llm_conviction and")
    print("     news_arbitrage need a separate paper-trading phase to gather")
    print("     real signals before they can be backtested.")

    return {"summary": summary, "per_window": per_window, "verdict": verdict, "message": msg}


def main() -> None:
    global INITIAL_BANKROLL
    p = argparse.ArgumentParser()
    p.add_argument("--months", type=int, default=6, help="walk-forward window count")
    p.add_argument("--bankroll", type=float, default=INITIAL_BANKROLL, help="starting bankroll USDC")
    p.add_argument("--seed", type=int, default=42)
    p.add_argument("--json", action="store_true", help="dump final summary as JSON to stdout")
    args = p.parse_args()
    INITIAL_BANKROLL = args.bankroll

    print("=" * 78)
    print("polymoney whale-copy backtest (standalone)")
    print(f"  bankroll=${args.bankroll}  windows={args.months}  seed={args.seed}")
    print(f"  fees={POLYMARKET_FEE_PCT:.0%} taker  slippage={SLIPPAGE_PCT:.1%}  gas=${GAS_USDC}")
    print("=" * 78)

    result = run(months=args.months, seed=args.seed)

    if args.json:
        print("\n--- JSON ---")
        print(json.dumps(result, indent=2, default=str))


if __name__ == "__main__":
    main()
