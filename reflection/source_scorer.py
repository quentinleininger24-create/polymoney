"""Per-source reliability scoring.

For each resolved market:
1. Find the decisive-move timestamp (first sustained crossing past 0.75 toward winner).
2. For every Signal produced on that market BEFORE the decisive move:
   - correct? == (signal.direction == winning outcome)
   - lead_minutes = minutes between signal.ts and decisive_move_ts
3. Attribute each signal to its underlying source via its Event:
   - Event.source = "twitter" / "newsapi" / etc
   - identifier = Event.author (twitter handle) or parsed URL host (newsapi) or whale address
4. Update SourceScore with rolling accuracy + avg lead time, compute weight.

The weight formula biases toward sources that are both accurate AND early:
    weight = accuracy * (1 + lead_bonus_per_hour * avg_lead_hours)
clamped to [floor, ceiling].
"""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime
from decimal import Decimal
from urllib.parse import urlparse

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert

from reflection.config import config
from shared.db import session_scope
from shared.logging import get_logger
from shared.models import (
    Event,
    Market,
    Outcome,
    PriceTick,
    Signal,
    SignalResolution,
    SourceScore,
)

log = get_logger(__name__)


def _host(url: str | None) -> str | None:
    if not url:
        return None
    try:
        return urlparse(url).netloc.replace("www.", "") or None
    except ValueError:
        return None


def _source_identifier(ev: Event) -> tuple[str, str]:
    """Return (source_type, identifier) for attribution."""
    src = ev.source
    if src == "twitter":
        return ("twitter", ev.author or "unknown")
    if src == "reddit":
        return ("reddit", (ev.raw or {}).get("sub") or "unknown")
    if src in {"newsapi", "gdelt"}:
        return (src, _host(ev.url) or "unknown")
    return (src, ev.author or "unknown")


async def _decisive_move_ts(market_id: str, winning_outcome: Outcome) -> datetime | None:
    """First tick where the winning side price crossed 0.75 and didn't come back below 0.60."""
    async with session_scope() as db:
        ticks = (await db.execute(
            select(PriceTick).where(PriceTick.market_id == market_id).order_by(PriceTick.ts)
        )).scalars().all()
    if not ticks:
        return None
    threshold_hi = Decimal("0.75")
    threshold_lo = Decimal("0.60")
    candidate: datetime | None = None
    for t in ticks:
        mid = t.yes_mid or Decimal(0)
        winning_price = mid if winning_outcome == Outcome.YES else Decimal(1) - mid
        if candidate is None and winning_price >= threshold_hi:
            candidate = t.ts
        elif candidate is not None and winning_price < threshold_lo:
            candidate = None  # reverted, reset
    return candidate


async def score_resolved_market(market_id: str) -> int:
    """Score all signals on a resolved market. Returns count of newly-scored signals."""
    async with session_scope() as db:
        market = (await db.execute(
            select(Market).where(Market.id == market_id)
        )).scalar_one_or_none()
    if not market or not market.resolved or not market.resolution:
        return 0

    winning = Outcome.YES if market.resolution.upper() == "YES" else Outcome.NO
    decisive = await _decisive_move_ts(market_id, winning)
    cutoff = decisive or market.end_date or datetime.utcnow()

    async with session_scope() as db:
        sig_rows = (await db.execute(
            select(Signal, Event)
            .join(Event, Signal.event_id == Event.id)
            .outerjoin(SignalResolution, SignalResolution.signal_id == Signal.id)
            .where(Signal.market_id == market_id, SignalResolution.id.is_(None), Signal.ts <= cutoff)
        )).all()

        per_source: dict[tuple[str, str], list[tuple[bool, float]]] = defaultdict(list)
        new_count = 0
        for sig, ev in sig_rows:
            correct = sig.direction == winning
            lead_minutes = max(0.0, (cutoff - sig.ts).total_seconds() / 60.0)
            db.add(SignalResolution(
                signal_id=sig.id,
                market_id=market_id,
                correct=correct,
                lead_minutes=lead_minutes,
                price_at_resolution=Decimal(1) if correct else Decimal(0),
            ))
            src_type, ident = _source_identifier(ev)
            per_source[(src_type, ident)].append((correct, lead_minutes))
            # Also attribute to strategy
            per_source[("strategy", sig.strategy)].append((correct, lead_minutes))
            new_count += 1

        for (src_type, ident), outcomes in per_source.items():
            correct_n = sum(1 for c, _ in outcomes if c)
            total_n = len(outcomes)
            avg_lead = sum(l for _, l in outcomes) / total_n if total_n else 0.0
            stmt = insert(SourceScore).values(
                source_type=src_type,
                identifier=ident,
                signals_total=total_n,
                signals_correct=correct_n,
                accuracy=correct_n / total_n if total_n else 0.0,
                avg_lead_minutes=avg_lead,
                weight=1.0,
                last_updated=datetime.utcnow(),
            )
            stmt = stmt.on_conflict_do_update(
                index_elements=["source_type", "identifier"],
                set_={
                    "signals_total": SourceScore.signals_total + stmt.excluded.signals_total,
                    "signals_correct": SourceScore.signals_correct + stmt.excluded.signals_correct,
                    "avg_lead_minutes": stmt.excluded.avg_lead_minutes,
                    "last_updated": datetime.utcnow(),
                },
            )
            await db.execute(stmt)

    await _recompute_weights()
    log.info("reflection.scored_market", market=market_id[:10], signals=new_count)
    return new_count


async def _recompute_weights() -> None:
    """Refresh accuracy + weight for all rows from their cumulative counters."""
    from sqlalchemy import update

    async with session_scope() as db:
        rows = (await db.execute(select(SourceScore))).scalars().all()
        for r in rows:
            acc = (r.signals_correct / r.signals_total) if r.signals_total else 0.0
            lead_hours = float(r.avg_lead_minutes) / 60.0
            if r.signals_total < config.min_signals_to_weight:
                weight = 1.0
            else:
                raw = acc * (1 + config.lead_time_bonus_per_hour * lead_hours)
                # Center around 1.0: accuracy 0.5 with 0 lead -> weight 0.5
                weight = max(config.weight_floor, min(config.weight_ceiling, raw * 2))
            await db.execute(
                update(SourceScore)
                .where(SourceScore.id == r.id)
                .values(accuracy=acc, weight=weight)
            )


async def top_sources(
    source_type: str | None = None, limit: int = 20, min_signals: int = 5
) -> list[SourceScore]:
    async with session_scope() as db:
        q = select(SourceScore).where(SourceScore.signals_total >= min_signals)
        if source_type:
            q = q.where(SourceScore.source_type == source_type)
        q = q.order_by(SourceScore.weight.desc()).limit(limit)
        rows = (await db.execute(q)).scalars().all()
    return list(rows)


async def score_for(source_type: str, identifier: str) -> SourceScore | None:
    async with session_scope() as db:
        return (await db.execute(
            select(SourceScore).where(
                SourceScore.source_type == source_type,
                SourceScore.identifier == identifier,
            )
        )).scalar_one_or_none()
