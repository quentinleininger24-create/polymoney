"""LLM-driven high-conviction strategy.

Pipeline per tick:
1. Pull recent unprocessed events.
2. Haiku triage for political relevance.
3. Embedding match -> top-10 candidate markets.
4. Sonnet analysis -> structured signals.
5. Emit TradeIntents for signals passing threshold.
"""

from datetime import datetime, timedelta
from decimal import Decimal

from sqlalchemy import select

from shared.db import session_scope
from shared.logging import get_logger
from shared.models import Event, Outcome, Signal
from signals.llm_analyst import analyze_event, load_market_context, triage_event_is_relevant
from signals.matching import top_k
from strategy.base import Strategy, TradeIntent

log = get_logger(__name__)


class LLMConvictionStrategy(Strategy):
    name = "llm_conviction"
    allocation_pct = 0.50  # 50% of bankroll — primary strat for small portfolio

    def __init__(self, event_lookback_minutes: int = 10):
        self.event_lookback_minutes = event_lookback_minutes

    async def generate_intents(self) -> list[TradeIntent]:
        cutoff = datetime.utcnow() - timedelta(minutes=self.event_lookback_minutes)
        async with session_scope() as db:
            result = await db.execute(
                select(Event)
                .where(Event.ts >= cutoff)
                .order_by(Event.ts.desc())
                .limit(50)
            )
            events = result.scalars().all()
            # Skip events that already produced a signal from this strategy
            processed_ids = await db.execute(
                select(Signal.event_id).where(Signal.strategy == self.name)
            )
            processed = {row[0] for row in processed_ids}

        if not events:
            return []

        markets = await load_market_context(limit=80)
        if not markets:
            return []

        intents: list[TradeIntent] = []
        for ev in events:
            if ev.id in processed:
                continue
            text = f"{ev.title or ''}\n{ev.content}"[:3000]
            if not await triage_event_is_relevant(text):
                continue
            candidates = top_k(text, markets, k=10)
            raw_signals = await analyze_event(text, candidates)
            async with session_scope() as db:
                for s in raw_signals:
                    db.add(Signal(
                        event_id=ev.id,
                        market_id=s["market_id"],
                        strategy=self.name,
                        direction=Outcome(s["direction"]),
                        edge_bps=int(s["edge_bps"]),
                        confidence=float(s["confidence"]),
                        reasoning=s.get("reasoning"),
                    ))
                    intents.append(TradeIntent(
                        market_id=s["market_id"],
                        outcome=Outcome(s["direction"]),
                        edge_bps=int(s["edge_bps"]),
                        confidence=float(s["confidence"]),
                        reasoning=s.get("reasoning", ""),
                        strategy=self.name,
                    ))
        log.info("llm_conviction.intents", count=len(intents))
        return intents
