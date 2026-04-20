"""Retrospective analysis: replay recent losses to find what we should have seen.

Produces a diagnosis dict consumed by the adapter:
  {
    "losses_analyzed": 12,
    "top_unheeded_sources": [("twitter", "NateSilver538", 0.82, 240)],
        # (type, id, accuracy, avg_lead_minutes) — sources that were RIGHT but we ignored
    "top_misleading_sources": [("reddit", "politics", 0.22)],
        # sources that kept pushing us wrong direction
    "worst_strategies": [("news_arb", 0.21)],
    "recommended_changes": {
        "disable_strategies": ["news_arb"],
        "raise_min_confidence_to": 0.75,
        "raise_confluence_requirement": True,
        "reweight_sources": {...}
    }
  }
"""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta
from decimal import Decimal

from sqlalchemy import select

from shared.db import session_scope
from shared.logging import get_logger
from shared.models import Bet, BetStatus, Event, Market, Signal, SourceScore, StrategyScore
from reflection.source_scorer import _source_identifier

log = get_logger(__name__)


async def analyze_recent_losses(lookback_days: int = 14) -> dict:
    cutoff = datetime.utcnow() - timedelta(days=lookback_days)
    async with session_scope() as db:
        losses = (await db.execute(
            select(Bet, Market)
            .join(Market, Bet.market_id == Market.id)
            .where(
                Bet.status == BetStatus.CLOSED_LOSS,
                Bet.closed_at >= cutoff,
            )
            .order_by(Bet.closed_at.desc())
        )).all()

    unheeded: dict[tuple[str, str], int] = defaultdict(int)
    misleading: dict[tuple[str, str], int] = defaultdict(int)

    async with session_scope() as db:
        for bet, market in losses:
            winning_dir = bet.outcome
            winning_dir = winning_dir  # the side we bet on, which lost -> winner is the OTHER side
            loser_side = bet.outcome
            winner_side = "NO" if loser_side.value == "YES" else "YES"

            rows = (await db.execute(
                select(Signal, Event)
                .join(Event, Signal.event_id == Event.id)
                .where(
                    Signal.market_id == market.id,
                    Signal.ts <= bet.opened_at,
                    Signal.ts >= bet.opened_at - timedelta(hours=24),
                )
            )).all()
            for sig, ev in rows:
                src = _source_identifier(ev)
                if sig.direction.value == winner_side:
                    unheeded[src] += 1
                elif sig.direction.value == loser_side.value:
                    misleading[src] += 1

        # Pull current reliability to enrich
        scores = {
            (s.source_type, s.identifier): s
            for s in (await db.execute(select(SourceScore))).scalars().all()
        }
        strat_scores = {
            s.name: s
            for s in (await db.execute(select(StrategyScore))).scalars().all()
        }

    def decorate(counter: dict) -> list[tuple]:
        out = []
        for (t, ident), hits in counter.items():
            sc = scores.get((t, ident))
            out.append((t, ident, hits, float(sc.accuracy) if sc else None, float(sc.avg_lead_minutes) if sc else None))
        out.sort(key=lambda x: -x[2])
        return out[:10]

    worst_strategies = [
        (name, float(s.win_rate)) for name, s in strat_scores.items() if not s.enabled
    ]

    diagnosis = {
        "losses_analyzed": len(losses),
        "top_unheeded_sources": decorate(unheeded),
        "top_misleading_sources": decorate(misleading),
        "worst_strategies": worst_strategies,
    }
    log.info("reflection.diagnosis", **{k: v if isinstance(v, int) else len(v) for k, v in diagnosis.items()})
    return diagnosis
