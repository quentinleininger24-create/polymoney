"""Per-strategy rolling performance."""

from __future__ import annotations

import math
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert

from reflection.config import config
from shared.db import session_scope
from shared.models import Bet, BetStatus, StrategyScore


async def recompute_all() -> list[StrategyScore]:
    async with session_scope() as db:
        strategies = (await db.execute(
            select(Bet.strategy).distinct()
        )).scalars().all()

        snapshots: list[StrategyScore] = []
        for name in strategies:
            # last N closed bets for rolling stats
            recent = (await db.execute(
                select(Bet)
                .where(
                    Bet.strategy == name,
                    Bet.status.in_([BetStatus.CLOSED_WIN, BetStatus.CLOSED_LOSS]),
                )
                .order_by(Bet.closed_at.desc())
                .limit(config.strategy_rolling_window)
            )).scalars().all()

            total_bets = (await db.execute(
                select(func.count()).select_from(Bet).where(
                    Bet.strategy == name,
                    Bet.status != BetStatus.OPEN,
                )
            )).scalar_one()

            wins = sum(1 for b in recent if b.status == BetStatus.CLOSED_WIN)
            win_rate = (wins / len(recent)) if recent else 0.0

            total_pnl = (await db.execute(
                select(func.coalesce(func.sum(Bet.pnl_usdc), 0)).where(
                    Bet.strategy == name,
                    Bet.status != BetStatus.OPEN,
                )
            )).scalar_one()

            # Consecutive losses
            streak = 0
            for b in recent:
                if b.status == BetStatus.CLOSED_LOSS:
                    streak += 1
                else:
                    break

            # Rough Sharpe estimate over the window (daily-return assumption skipped;
            # we use raw bet PnL vs cost_basis as return per trade)
            returns = [
                float(b.pnl_usdc / b.cost_basis_usdc) if b.cost_basis_usdc else 0.0
                for b in recent
            ]
            sharpe = 0.0
            if len(returns) > 1:
                mean = sum(returns) / len(returns)
                var = sum((r - mean) ** 2 for r in returns) / (len(returns) - 1)
                std = math.sqrt(var)
                sharpe = (mean / std) * math.sqrt(len(returns)) if std else 0.0

            # Kill switch: disable if badly underperforming
            enabled = not (
                streak >= config.strategy_consecutive_losses
                or (len(recent) >= config.strategy_rolling_window
                    and win_rate < config.strategy_rolling_winrate_floor)
            )

            stmt = insert(StrategyScore).values(
                name=name,
                bets_total=total_bets,
                bets_won=wins,
                win_rate=win_rate,
                total_pnl_usdc=Decimal(total_pnl),
                sharpe_estimate=sharpe,
                consecutive_losses=streak,
                max_drawdown_pct=0.0,
                enabled=enabled,
                allocation_pct=0.0,
            )
            stmt = stmt.on_conflict_do_update(
                index_elements=["name"],
                set_={
                    "bets_total": stmt.excluded.bets_total,
                    "bets_won": stmt.excluded.bets_won,
                    "win_rate": stmt.excluded.win_rate,
                    "total_pnl_usdc": stmt.excluded.total_pnl_usdc,
                    "sharpe_estimate": stmt.excluded.sharpe_estimate,
                    "consecutive_losses": stmt.excluded.consecutive_losses,
                    "enabled": stmt.excluded.enabled,
                },
            )
            await db.execute(stmt)
            snapshots.append(StrategyScore(
                name=name, win_rate=win_rate, sharpe_estimate=sharpe,
                consecutive_losses=streak, enabled=enabled, bets_total=total_bets,
                bets_won=wins, total_pnl_usdc=Decimal(total_pnl),
            ))
        return snapshots


async def enabled_strategies() -> set[str]:
    async with session_scope() as db:
        rows = (await db.execute(
            select(StrategyScore.name).where(StrategyScore.enabled == True)  # noqa: E712
        )).scalars().all()
    return set(rows)
