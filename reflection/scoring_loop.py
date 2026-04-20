"""Periodic task: find markets that just resolved and score their signals."""

from __future__ import annotations

from sqlalchemy import and_, exists, select

from reflection.source_scorer import score_resolved_market
from shared.db import session_scope
from shared.logging import get_logger
from shared.models import Market, SignalResolution

log = get_logger(__name__)


async def score_newly_resolved(max_per_run: int = 20) -> int:
    async with session_scope() as db:
        # Markets resolved but whose signals haven't been scored
        scored_subq = (
            select(SignalResolution.market_id)
            .where(SignalResolution.market_id == Market.id)
            .correlate(Market)
        )
        rows = (await db.execute(
            select(Market.id)
            .where(Market.resolved == True, ~exists(scored_subq))  # noqa: E712
            .limit(max_per_run)
        )).scalars().all()

    total = 0
    for mid in rows:
        try:
            total += await score_resolved_market(mid)
        except Exception as e:  # noqa: BLE001
            log.warning("reflection.score_failed", market=mid[:10], err=str(e))
    log.info("reflection.scored_batch", markets=len(rows), signals=total)
    return total
