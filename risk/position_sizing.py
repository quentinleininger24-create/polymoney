"""Position sizing + pre-trade risk checks."""

from decimal import Decimal

from sqlalchemy import func, select

from shared.config import settings
from shared.db import session_scope
from shared.logging import get_logger
from shared.models import Bet, BetStatus, Outcome
from risk.kelly import edge_prob_from_bps, sized_bet_usdc

log = get_logger(__name__)


async def compute_bankroll() -> Decimal:
    async with session_scope() as db:
        opened = (await db.execute(
            select(func.coalesce(func.sum(Bet.cost_basis_usdc), 0))
            .where(Bet.status == BetStatus.OPEN)
        )).scalar_one()
        realized = (await db.execute(
            select(func.coalesce(func.sum(Bet.pnl_usdc), 0))
            .where(Bet.status != BetStatus.OPEN)
        )).scalar_one()
    initial = Decimal(str(settings.initial_bankroll_usdc))
    return initial + Decimal(realized) - Decimal(opened)  # free cash


async def current_event_exposure(market_id: str) -> Decimal:
    async with session_scope() as db:
        total = (await db.execute(
            select(func.coalesce(func.sum(Bet.cost_basis_usdc), 0))
            .where(Bet.market_id == market_id, Bet.status == BetStatus.OPEN)
        )).scalar_one()
    return Decimal(total)


async def size_intent(
    market_id: str,
    outcome: Outcome,
    edge_bps: int,
    confidence: float,
    current_yes_price: float,
) -> Decimal:
    """Return USDC size for this intent, or 0 if blocked by risk rules."""
    if confidence < settings.min_confidence:
        return Decimal(0)
    if abs(edge_bps) < settings.min_edge_bps:
        return Decimal(0)

    cash = await compute_bankroll()
    if cash <= Decimal("1"):
        log.warning("risk.insufficient_cash", cash=str(cash))
        return Decimal(0)

    price_for_side = current_yes_price if outcome == Outcome.YES else 1 - current_yes_price
    edge_prob = edge_prob_from_bps(price_for_side, edge_bps)

    size = sized_bet_usdc(
        edge_prob=edge_prob,
        current_price=price_for_side,
        bankroll_usdc=Decimal(str(settings.initial_bankroll_usdc)),
        kelly_multiplier=settings.kelly_fraction,
        max_pct=settings.max_position_pct,
    )

    # Event exposure cap
    existing = await current_event_exposure(market_id)
    cap_total = Decimal(str(settings.initial_bankroll_usdc)) * Decimal(str(settings.max_event_exposure_pct))
    headroom = max(Decimal(0), cap_total - existing)
    size = min(size, headroom, cash)
    return size
