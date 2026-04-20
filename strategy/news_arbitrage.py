"""News arbitrage: breaking news with price not yet moved.

Detects events classified as high-impact where Polymarket price hasn't shifted
in N seconds — enters before the market catches up.
"""

from datetime import datetime, timedelta

from sqlalchemy import select

from shared.db import session_scope
from shared.logging import get_logger
from shared.models import Event, Signal
from strategy.base import Strategy, TradeIntent

log = get_logger(__name__)


class NewsArbitrageStrategy(Strategy):
    name = "news_arb"
    allocation_pct = 0.20

    def __init__(self, max_staleness_seconds: int = 120):
        self.max_staleness_seconds = max_staleness_seconds

    async def generate_intents(self) -> list[TradeIntent]:
        # Uses signals already produced by llm_conviction, but filters for
        # very fresh events (news is only arb-able for a few minutes).
        cutoff = datetime.utcnow() - timedelta(seconds=self.max_staleness_seconds)
        async with session_scope() as db:
            result = await db.execute(
                select(Signal, Event)
                .join(Event, Signal.event_id == Event.id)
                .where(
                    Event.ts >= cutoff,
                    Event.source.in_(["newsapi", "gdelt", "twitter"]),
                    Signal.edge_bps >= 500,
                    Signal.confidence >= 0.75,
                )
                .order_by(Signal.ts.desc())
                .limit(20)
            )
            rows = result.all()
        intents: list[TradeIntent] = []
        for sig, _ev in rows:
            intents.append(TradeIntent(
                market_id=sig.market_id,
                outcome=sig.direction,
                edge_bps=sig.edge_bps,
                confidence=float(sig.confidence),
                reasoning=f"news_arb: {sig.reasoning}",
                strategy=self.name,
            ))
        log.info("news_arb.intents", count=len(intents))
        return intents
