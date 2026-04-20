"""Whale copy trading: mirror trades of top-performing Polymarket wallets.

Best risk/reward strategy at small bankroll scale: leverage other people's research.
"""

from datetime import datetime, timedelta
from decimal import Decimal

from sqlalchemy import select

from shared.db import session_scope
from shared.logging import get_logger
from shared.models import Outcome, WhaleTrade, WhaleWallet
from strategy.base import Strategy, TradeIntent

log = get_logger(__name__)


class WhaleCopyStrategy(Strategy):
    name = "whale_copy"
    allocation_pct = 0.30

    def __init__(
        self,
        min_whale_pnl_usdc: float = 10_000,
        copy_window_minutes: int = 30,
        min_whale_size_usdc: float = 500,
    ):
        self.min_whale_pnl = min_whale_pnl_usdc
        self.copy_window = copy_window_minutes
        self.min_whale_size = min_whale_size_usdc

    async def generate_intents(self) -> list[TradeIntent]:
        cutoff = datetime.utcnow() - timedelta(minutes=self.copy_window)
        async with session_scope() as db:
            result = await db.execute(
                select(WhaleTrade, WhaleWallet)
                .join(WhaleWallet, WhaleTrade.wallet == WhaleWallet.address)
                .where(
                    WhaleTrade.ts >= cutoff,
                    WhaleWallet.active == True,  # noqa: E712
                    WhaleWallet.total_pnl_usdc >= self.min_whale_pnl,
                    WhaleTrade.size_usdc >= self.min_whale_size,
                )
                .order_by(WhaleTrade.ts.desc())
            )
            rows = result.all()

        intents: list[TradeIntent] = []
        seen: set[tuple[str, Outcome]] = set()
        for trade, wallet in rows:
            key = (trade.market_id, trade.outcome)
            if key in seen:
                continue
            seen.add(key)
            # Edge heuristic: whale entry price vs assumed 50% baseline
            edge_bps = max(0, int(abs(Decimal("0.5") - trade.price) * 10000))
            confidence = min(0.95, 0.6 + (float(wallet.total_pnl_usdc) / 100_000) * 0.05)
            intents.append(TradeIntent(
                market_id=trade.market_id,
                outcome=trade.outcome,
                edge_bps=edge_bps,
                confidence=confidence,
                reasoning=f"copy {wallet.address[:8]} (pnl ${wallet.total_pnl_usdc:.0f})",
                strategy=self.name,
            ))
        log.info("whale_copy.intents", count=len(intents))
        return intents
