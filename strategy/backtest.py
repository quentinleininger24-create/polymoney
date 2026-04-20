"""Backtesting engine.

Two modes:

1. **Bet-replay** (default, used by reflection): take every bet actually placed
   in [start, end], filter through the *current* DB state (enabled strategies,
   source weights, min thresholds, max position pct), recompute sizing, and
   simulate PnL using each market's actual resolution. This answers
   "with the current config, would we have done better/worse on the same
   period?" and lets the reflection orchestrator validate adaptations
   deterministically.

2. **Signal-replay** (`use_signals=True`): instead of bets, replay every
   *signal* we ever produced -- including ones below threshold -- against
   the current config. Lets you ask "if we lowered min_confidence to 0.55
   on this past month, what happens?". Requires a PriceTick within
   `price_window_minutes` of the signal time; otherwise skipped.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal

from sqlalchemy import select

from risk.kelly import edge_prob_from_bps, sized_bet_usdc
from shared.config import settings
from shared.db import session_scope
from shared.logging import get_logger
from shared.models import (
    Bet,
    BetStatus,
    Market,
    Outcome,
    PriceTick,
    Signal,
    SourceScore,
    StrategyScore,
)

log = get_logger(__name__)


@dataclass
class BacktestResult:
    label: str = ""
    total_trades: int = 0
    wins: int = 0
    losses: int = 0
    skipped: int = 0
    total_pnl_usdc: Decimal = Decimal(0)
    final_bankroll: Decimal = Decimal(0)
    roi_pct: float = 0.0
    win_rate: float = 0.0
    sharpe: float = 0.0
    max_drawdown_pct: float = 0.0
    by_strategy: dict[str, dict] = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {
            "label": self.label,
            "total_trades": self.total_trades,
            "wins": self.wins,
            "losses": self.losses,
            "skipped": self.skipped,
            "total_pnl_usdc": float(self.total_pnl_usdc),
            "final_bankroll": float(self.final_bankroll),
            "roi_pct": self.roi_pct,
            "win_rate": self.win_rate,
            "sharpe": self.sharpe,
            "max_drawdown_pct": self.max_drawdown_pct,
            "by_strategy": self.by_strategy,
        }


@dataclass
class ConfigSnapshot:
    enabled_strategies: set[str]
    source_weights: dict[tuple[str, str], float]
    min_confidence: float
    min_edge_bps: int
    max_position_pct: float
    kelly_fraction: float

    def weight_for_strategy(self, name: str) -> float:
        return self.source_weights.get(("strategy", name), 1.0)


async def snapshot_config() -> ConfigSnapshot:
    async with session_scope() as db:
        strats = (await db.execute(
            select(StrategyScore.name).where(StrategyScore.enabled == True)  # noqa: E712
        )).scalars().all()
        sources = (await db.execute(select(SourceScore))).scalars().all()
    weights = {(s.source_type, s.identifier): float(s.weight) for s in sources}
    return ConfigSnapshot(
        enabled_strategies=set(strats),
        source_weights=weights,
        min_confidence=settings.min_confidence,
        min_edge_bps=settings.min_edge_bps,
        max_position_pct=settings.max_position_pct,
        kelly_fraction=settings.kelly_fraction,
    )


def _sharpe(returns: list[float]) -> float:
    if len(returns) < 2:
        return 0.0
    mean = sum(returns) / len(returns)
    var = sum((r - mean) ** 2 for r in returns) / (len(returns) - 1)
    std = math.sqrt(var) if var > 0 else 0.0
    return (mean / std) * math.sqrt(len(returns)) if std else 0.0


def _max_drawdown(equity: list[Decimal]) -> float:
    if not equity:
        return 0.0
    peak = equity[0]
    max_dd = 0.0
    for v in equity:
        if v > peak:
            peak = v
        if peak > 0:
            dd = float((peak - v) / peak)
            if dd > max_dd:
                max_dd = dd
    return max_dd


def _settle_bet(bet: Bet, market: Market) -> Decimal:
    """PnL for a bet given the market's resolution."""
    if not market.resolution:
        return Decimal(0)
    won = bet.outcome.value.upper() == market.resolution.upper()
    if won:
        # Each share pays $1 at resolution.
        payout = bet.size_shares  # shares * $1
        return Decimal(payout) - bet.cost_basis_usdc
    return -bet.cost_basis_usdc


async def _resolved_markets_index(start: datetime, end: datetime) -> dict[str, Market]:
    async with session_scope() as db:
        rows = (await db.execute(
            select(Market).where(
                Market.resolved == True,  # noqa: E712
                Market.end_date >= start,
                Market.end_date <= end,
            )
        )).scalars().all()
    return {m.id: m for m in rows}


async def replay(
    label: str,
    start: datetime,
    end: datetime,
    initial_bankroll: Decimal = Decimal(100),
) -> BacktestResult:
    """Bet-replay against the current DB config snapshot."""
    cfg = await snapshot_config()
    markets = await _resolved_markets_index(start, end)
    result = BacktestResult(label=label, final_bankroll=initial_bankroll)

    if not markets:
        log.info("backtest.no_resolved_markets", label=label)
        return result

    async with session_scope() as db:
        bets = (await db.execute(
            select(Bet)
            .where(
                Bet.opened_at >= start,
                Bet.opened_at <= end,
                Bet.market_id.in_(list(markets.keys())),
            )
            .order_by(Bet.opened_at)
        )).scalars().all()

    bankroll = Decimal(initial_bankroll)
    equity = [bankroll]
    returns: list[float] = []
    by_strat: dict[str, dict] = {}

    for bet in bets:
        market = markets[bet.market_id]
        # Apply current config filters
        if cfg.enabled_strategies and bet.strategy not in cfg.enabled_strategies:
            result.skipped += 1
            continue
        adj_conf = float(bet.confidence_at_entry) * cfg.weight_for_strategy(bet.strategy)
        if adj_conf < cfg.min_confidence or bet.edge_bps_at_entry < cfg.min_edge_bps:
            result.skipped += 1
            continue

        # Re-size with current Kelly + caps using the original entry price.
        edge_prob = edge_prob_from_bps(float(bet.entry_price), bet.edge_bps_at_entry)
        size_usdc = sized_bet_usdc(
            edge_prob=edge_prob,
            current_price=float(bet.entry_price),
            bankroll_usdc=Decimal(initial_bankroll),
            kelly_multiplier=cfg.kelly_fraction,
            max_pct=cfg.max_position_pct,
        )
        size_usdc = min(size_usdc, bankroll)
        if size_usdc < Decimal("1"):
            result.skipped += 1
            continue

        shares = (size_usdc / bet.entry_price).quantize(Decimal("0.0001"))
        synthetic = Bet(
            market_id=bet.market_id,
            strategy=bet.strategy,
            outcome=bet.outcome,
            cost_basis_usdc=size_usdc,
            size_shares=shares,
            entry_price=bet.entry_price,
            edge_bps_at_entry=bet.edge_bps_at_entry,
            confidence_at_entry=bet.confidence_at_entry,
            status=BetStatus.OPEN,
        )
        pnl = _settle_bet(synthetic, market)
        bankroll += pnl
        equity.append(bankroll)
        returns.append(float(pnl / size_usdc))

        result.total_trades += 1
        if pnl > 0:
            result.wins += 1
        else:
            result.losses += 1
        result.total_pnl_usdc += pnl

        ent = by_strat.setdefault(bet.strategy, {"trades": 0, "wins": 0, "pnl": 0.0})
        ent["trades"] += 1
        if pnl > 0:
            ent["wins"] += 1
        ent["pnl"] += float(pnl)

    result.final_bankroll = bankroll
    result.roi_pct = float((bankroll - initial_bankroll) / initial_bankroll * 100) if initial_bankroll else 0.0
    result.win_rate = (result.wins / result.total_trades) if result.total_trades else 0.0
    result.sharpe = _sharpe(returns)
    result.max_drawdown_pct = _max_drawdown(equity)
    result.by_strategy = by_strat

    log.info("backtest.done",
             label=label,
             trades=result.total_trades,
             wins=result.wins,
             pnl=float(result.total_pnl_usdc),
             roi=result.roi_pct,
             sharpe=round(result.sharpe, 2),
             dd=round(result.max_drawdown_pct, 4))
    return result


# --- Signal-replay variant -------------------------------------------------

async def _price_at(market_id: str, ts: datetime, window_minutes: int = 30) -> Decimal | None:
    delta = timedelta(minutes=window_minutes)
    async with session_scope() as db:
        tick = (await db.execute(
            select(PriceTick)
            .where(
                PriceTick.market_id == market_id,
                PriceTick.ts >= ts - delta,
                PriceTick.ts <= ts + delta,
            )
            .order_by(PriceTick.ts)
            .limit(1)
        )).scalar_one_or_none()
    return tick.yes_mid if tick else None


async def replay_signals(
    label: str,
    start: datetime,
    end: datetime,
    initial_bankroll: Decimal = Decimal(100),
    price_window_minutes: int = 30,
) -> BacktestResult:
    """What-if replay using every Signal in the window, not just placed bets."""
    cfg = await snapshot_config()
    markets = await _resolved_markets_index(start, end)
    result = BacktestResult(label=label, final_bankroll=initial_bankroll)
    if not markets:
        return result

    async with session_scope() as db:
        signals = (await db.execute(
            select(Signal)
            .where(
                Signal.ts >= start,
                Signal.ts <= end,
                Signal.market_id.in_(list(markets.keys())),
            )
            .order_by(Signal.ts)
        )).scalars().all()

    bankroll = Decimal(initial_bankroll)
    equity = [bankroll]
    returns: list[float] = []
    seen_market_strat: set[tuple[str, str]] = set()

    for sig in signals:
        market = markets[sig.market_id]
        key = (sig.market_id, sig.strategy)
        if key in seen_market_strat:
            continue  # only first signal triggers an entry
        if cfg.enabled_strategies and sig.strategy not in cfg.enabled_strategies:
            continue
        weight = cfg.weight_for_strategy(sig.strategy)
        if float(sig.confidence) * weight < cfg.min_confidence or sig.edge_bps < cfg.min_edge_bps:
            continue
        yes_mid = await _price_at(sig.market_id, sig.ts, price_window_minutes)
        if yes_mid is None:
            result.skipped += 1
            continue
        seen_market_strat.add(key)
        price_for_side = yes_mid if sig.direction == Outcome.YES else Decimal(1) - yes_mid
        if price_for_side <= 0 or price_for_side >= 1:
            continue
        edge_prob = edge_prob_from_bps(float(price_for_side), sig.edge_bps)
        size_usdc = sized_bet_usdc(
            edge_prob=edge_prob,
            current_price=float(price_for_side),
            bankroll_usdc=Decimal(initial_bankroll),
            kelly_multiplier=cfg.kelly_fraction,
            max_pct=cfg.max_position_pct,
        )
        size_usdc = min(size_usdc, bankroll)
        if size_usdc < Decimal("1"):
            continue
        shares = (size_usdc / price_for_side).quantize(Decimal("0.0001"))
        won = sig.direction.value.upper() == (market.resolution or "").upper()
        pnl = (Decimal(shares) - size_usdc) if won else -size_usdc
        bankroll += pnl
        equity.append(bankroll)
        returns.append(float(pnl / size_usdc))
        result.total_trades += 1
        if won:
            result.wins += 1
        else:
            result.losses += 1
        result.total_pnl_usdc += pnl

    result.final_bankroll = bankroll
    result.roi_pct = float((bankroll - initial_bankroll) / initial_bankroll * 100) if initial_bankroll else 0.0
    result.win_rate = (result.wins / result.total_trades) if result.total_trades else 0.0
    result.sharpe = _sharpe(returns)
    result.max_drawdown_pct = _max_drawdown(equity)
    return result


# --- CLI -----------------------------------------------------------------

async def _cli(argv: list[str]) -> None:
    import argparse

    p = argparse.ArgumentParser("polymoney backtest")
    p.add_argument("--start", required=True, help="ISO date/datetime")
    p.add_argument("--end", required=True)
    p.add_argument("--bankroll", type=float, default=100.0)
    p.add_argument("--mode", choices=["bets", "signals"], default="bets")
    p.add_argument("--label", default="manual")
    args = p.parse_args(argv)

    start = datetime.fromisoformat(args.start)
    end = datetime.fromisoformat(args.end)
    runner = replay if args.mode == "bets" else replay_signals
    result = await runner(args.label, start, end, Decimal(str(args.bankroll)))
    import json
    print(json.dumps(result.as_dict(), indent=2, default=str))


if __name__ == "__main__":
    import asyncio
    import sys

    asyncio.run(_cli(sys.argv[1:]))
