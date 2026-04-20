"""Confluence filter: only let a TradeIntent through if independent sources agree.

Applied by the order manager BEFORE sizing. When the system is stressed
(reflection recently triggered), the bar rises from 1 -> 2+ distinct source types.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import select

from reflection.config import config
from shared.db import session_scope
from shared.models import Event, Outcome, Signal


async def distinct_source_types_supporting(
    market_id: str, direction: Outcome, window_minutes: int | None = None
) -> set[str]:
    """Return set of distinct source types (twitter, newsapi, whale, strategy)
    that have emitted a signal for this market+direction in the last window."""
    window = window_minutes or config.confluence_window_minutes
    cutoff = datetime.utcnow() - timedelta(minutes=window)
    async with session_scope() as db:
        rows = (await db.execute(
            select(Signal, Event)
            .join(Event, Signal.event_id == Event.id)
            .where(
                Signal.market_id == market_id,
                Signal.direction == direction,
                Signal.ts >= cutoff,
            )
        )).all()
    types = {ev.source for _sig, ev in rows}
    # The strategy itself counts as a type (e.g. whale_copy without an event is still a source)
    strategies = {sig.strategy for sig, _ev in rows}
    if "whale_copy" in strategies:
        types.add("whale")
    return types


async def has_confluence(
    market_id: str, direction: Outcome, stressed: bool
) -> tuple[bool, set[str]]:
    """Return (passes_gate, supporting_sources)."""
    required = (
        config.stressed_min_distinct_sources if stressed else config.base_min_distinct_sources
    )
    supporting = await distinct_source_types_supporting(market_id, direction)
    return (len(supporting) >= required, supporting)
