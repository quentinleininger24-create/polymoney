"""Order-book anomaly detection: volume spikes, imbalance, rapid price moves."""

from datetime import datetime, timedelta
from decimal import Decimal

from sqlalchemy import select

from shared.db import session_scope
from shared.logging import get_logger
from shared.models import PriceTick

log = get_logger(__name__)


async def detect_price_spike(market_id: str, window_minutes: int = 10, threshold_bps: int = 500) -> dict | None:
    """Flag markets where mid price moved > threshold_bps in window."""
    cutoff = datetime.utcnow() - timedelta(minutes=window_minutes)
    async with session_scope() as db:
        result = await db.execute(
            select(PriceTick)
            .where(PriceTick.market_id == market_id, PriceTick.ts >= cutoff)
            .order_by(PriceTick.ts)
        )
        ticks = result.scalars().all()
    if len(ticks) < 2:
        return None
    first = ticks[0].yes_mid
    last = ticks[-1].yes_mid
    if not (first and last) or first == 0:
        return None
    move_bps = int((last - first) / first * 10000)
    if abs(move_bps) >= threshold_bps:
        return {"market_id": market_id, "move_bps": move_bps, "from": float(first), "to": float(last)}
    return None


async def detect_book_imbalance(bids: list[dict], asks: list[dict]) -> Decimal | None:
    """Return imbalance ratio > 0 (bid-heavy) or < 0 (ask-heavy) in top-5 levels."""
    bid_vol = sum(Decimal(b["size"]) for b in bids[:5])
    ask_vol = sum(Decimal(a["size"]) for a in asks[:5])
    total = bid_vol + ask_vol
    if total == 0:
        return None
    return (bid_vol - ask_vol) / total
