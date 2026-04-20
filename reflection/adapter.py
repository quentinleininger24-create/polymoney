"""Translate a diagnosis into runtime adjustments.

Writes:
- StrategyScore.enabled = False for worst-performing strategies
- SourceScore.weight adjusted (floor for misleading, boost for unheeded-but-accurate)
- Returns a dict of side effects so the order manager can raise its confidence
  threshold and flip confluence to 'stressed' mode until next review.
"""

from __future__ import annotations

from sqlalchemy import update

from reflection.config import config
from shared.db import session_scope
from shared.logging import get_logger
from shared.models import SourceScore, StrategyScore

log = get_logger(__name__)


async def apply(diagnosis: dict) -> dict:
    adjustments: dict = {
        "disabled_strategies": [],
        "sources_boosted": 0,
        "sources_penalized": 0,
        "stressed_mode": True,
    }

    # Disable strategies flagged by the scorer
    for name, _wr in diagnosis.get("worst_strategies", []):
        async with session_scope() as db:
            await db.execute(
                update(StrategyScore).where(StrategyScore.name == name).values(enabled=False)
            )
        adjustments["disabled_strategies"].append(name)

    # Boost the sources that have been right while being ignored
    for t, ident, _hits, acc, _lead in diagnosis.get("top_unheeded_sources", []):
        if acc is None or acc < 0.6:
            continue
        async with session_scope() as db:
            await db.execute(
                update(SourceScore)
                .where(SourceScore.source_type == t, SourceScore.identifier == ident)
                .values(weight=min(config.weight_ceiling, float(acc) * 2.0 + 0.2))
            )
        adjustments["sources_boosted"] += 1

    # Penalize sources that keep pushing us into losses
    for t, ident, _hits, acc, _lead in diagnosis.get("top_misleading_sources", []):
        if acc is not None and acc > 0.5:
            continue  # don't penalize a source that's fine overall; it just happened to miss here
        async with session_scope() as db:
            await db.execute(
                update(SourceScore)
                .where(SourceScore.source_type == t, SourceScore.identifier == ident)
                .values(weight=config.weight_floor)
            )
        adjustments["sources_penalized"] += 1

    log.info("reflection.adjustments_applied", **adjustments)
    return adjustments
