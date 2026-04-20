"""Circuit breakers: daily drawdown stop, error-rate kill switch, manual panic."""

from datetime import datetime, timedelta
from decimal import Decimal

from sqlalchemy import func, select

from shared.config import settings
from shared.db import session_scope
from shared.logging import get_logger
from shared.models import Bet, BetStatus, CircuitBreakerState

log = get_logger(__name__)

DAILY_DRAWDOWN = "daily_drawdown"
MANUAL_PANIC = "manual_panic"


async def _is_tripped(name: str) -> bool:
    async with session_scope() as db:
        cb = (await db.execute(
            select(CircuitBreakerState).where(CircuitBreakerState.name == name)
        )).scalar_one_or_none()
    return bool(cb and cb.tripped)


async def any_tripped() -> tuple[bool, str | None]:
    async with session_scope() as db:
        cb = (await db.execute(
            select(CircuitBreakerState).where(CircuitBreakerState.tripped == True)  # noqa: E712
        )).scalar_one_or_none()
    return (bool(cb), cb.name if cb else None)


async def trip(name: str, reason: str) -> None:
    from sqlalchemy.dialects.postgresql import insert

    async with session_scope() as db:
        stmt = insert(CircuitBreakerState).values(
            name=name,
            tripped=True,
            tripped_at=datetime.utcnow(),
            reason=reason,
        ).on_conflict_do_update(
            index_elements=["name"],
            set_={"tripped": True, "tripped_at": datetime.utcnow(), "reason": reason, "cleared_at": None},
        )
        await db.execute(stmt)
    log.warning("circuit_breaker.tripped", name=name, reason=reason)


async def clear(name: str) -> None:
    from sqlalchemy import update

    async with session_scope() as db:
        await db.execute(
            update(CircuitBreakerState)
            .where(CircuitBreakerState.name == name)
            .values(tripped=False, cleared_at=datetime.utcnow())
        )
    log.info("circuit_breaker.cleared", name=name)


async def check_daily_drawdown() -> None:
    """Trip if today's realized PnL drops below -daily_drawdown_stop_pct."""
    today_start = datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
    async with session_scope() as db:
        realized = (await db.execute(
            select(func.coalesce(func.sum(Bet.pnl_usdc), 0))
            .where(Bet.closed_at >= today_start, Bet.status != BetStatus.OPEN)
        )).scalar_one()
    realized = Decimal(realized)
    limit = Decimal(str(settings.initial_bankroll_usdc)) * Decimal(str(-settings.daily_drawdown_stop_pct))
    if realized <= limit:
        await trip(DAILY_DRAWDOWN, f"realized PnL {realized} below limit {limit}")
