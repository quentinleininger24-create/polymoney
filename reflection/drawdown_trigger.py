"""Detects whether the system should enter self-reflection mode.

Fires on any of:
- N consecutive losing bets across all strategies
- Rolling win-rate on last M bets below floor
- Drawdown from 7-day equity peak exceeding threshold
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal

from sqlalchemy import func, select

from reflection.config import config
from shared.db import session_scope
from shared.models import Bet, BetStatus


@dataclass
class TriggerDecision:
    fire: bool
    reasons: list[str]
    stats: dict


async def _recent_closed_bets(limit: int) -> list[Bet]:
    async with session_scope() as db:
        rows = (await db.execute(
            select(Bet)
            .where(Bet.status.in_([BetStatus.CLOSED_WIN, BetStatus.CLOSED_LOSS]))
            .order_by(Bet.closed_at.desc())
            .limit(limit)
        )).scalars().all()
    return list(rows)


async def _consecutive_losses() -> int:
    bets = await _recent_closed_bets(50)
    streak = 0
    for b in bets:
        if b.status == BetStatus.CLOSED_LOSS:
            streak += 1
        else:
            break
    return streak


async def _rolling_winrate(window: int) -> tuple[float, int]:
    bets = await _recent_closed_bets(window)
    if not bets:
        return 1.0, 0
    wins = sum(1 for b in bets if b.status == BetStatus.CLOSED_WIN)
    return wins / len(bets), len(bets)


async def _drawdown_from_peak(days: int = 7) -> float:
    cutoff = datetime.utcnow() - timedelta(days=days)
    async with session_scope() as db:
        rows = (await db.execute(
            select(Bet.closed_at, Bet.pnl_usdc)
            .where(Bet.status != BetStatus.OPEN, Bet.closed_at >= cutoff)
            .order_by(Bet.closed_at)
        )).all()
    running = Decimal(0)
    peak = Decimal(0)
    max_dd = 0.0
    for _ts, pnl in rows:
        running += Decimal(pnl or 0)
        if running > peak:
            peak = running
        if peak > 0:
            dd = float((peak - running) / peak)
            max_dd = max(max_dd, dd)
    return max_dd


async def evaluate() -> TriggerDecision:
    streak = await _consecutive_losses()
    wr, wr_n = await _rolling_winrate(config.rolling_winrate_window)
    dd = await _drawdown_from_peak(7)

    reasons: list[str] = []
    if streak >= config.consecutive_losses_trigger:
        reasons.append(f"{streak} consecutive losses >= {config.consecutive_losses_trigger}")
    if wr_n >= config.rolling_winrate_window and wr < config.rolling_winrate_floor:
        reasons.append(f"rolling win rate {wr:.2%} over {wr_n} bets < floor {config.rolling_winrate_floor:.0%}")
    if dd >= config.drawdown_from_7d_peak_pct:
        reasons.append(f"7d drawdown {dd:.1%} >= {config.drawdown_from_7d_peak_pct:.0%}")

    return TriggerDecision(
        fire=bool(reasons),
        reasons=reasons,
        stats={"streak": streak, "winrate": wr, "winrate_n": wr_n, "drawdown_7d": dd},
    )
