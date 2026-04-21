"""Smart-whale strategy: the backtest-validated winner, wired for live runs.

Replaces the v1 WhaleCopyStrategy with tighter liquidity and size filters
that produced +40 percent per active month on a 9-month walk-forward on
$500 bankroll (see scripts/backtest_confluence.py and CLAUDE.md).

Differences from WhaleCopyStrategy:
- Requires whale trade notional >= $1000 (v1 used $500)
- Requires market to be liquid (has an associated `volume24hr` signal)
- Entry price gated to [0.10, 0.75]
- Passes per-strategy Kelly multiplier (0.75) via TradeIntent.max_size_usdc
  hint, letting the risk layer size aggressively without us editing global
  caps that apply to all strategies.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal

from sqlalchemy import func, select

from shared.config import settings
from shared.db import session_scope
from shared.logging import get_logger
from shared.models import Bet, BetStatus, Market, Outcome, WhaleTrade, WhaleWallet
from strategy.base import Strategy, TradeIntent

log = get_logger(__name__)


class SmartWhaleStrategy(Strategy):
    """Follow top-whale trades on liquid political markets, single-whale trigger.

    Validated params (9-month walk-forward, $500 bankroll, +40 pct/month):
      min whale notional: $1000
      min market volume:  $2000 (24hr) OR $20k (total)
      price range:        [0.10, 0.75]
      kelly:              0.75
      max position:       25 pct
    """

    name = "smart_whale"
    allocation_pct = 0.60  # primary strategy, most of bankroll
    effective_kelly = 0.75
    effective_max_position_pct = 0.25

    def __init__(
        self,
        copy_window_minutes: int = 30,
        min_whale_trade_usdc: float = 1000.0,
        min_market_volume_24h: float = 2000.0,
        min_market_total_volume: float = 20_000.0,
        min_price: float = 0.10,
        max_price: float = 0.75,
    ):
        self.copy_window_minutes = copy_window_minutes
        self.min_whale_trade_usdc = Decimal(str(min_whale_trade_usdc))
        self.min_vol24 = min_market_volume_24h
        self.min_total_vol = min_market_total_volume
        self.min_price = min_price
        self.max_price = max_price

    @staticmethod
    def _market_volume24(market: Market) -> float:
        raw = market.raw or {}
        try:
            return float(raw.get("volume24hr") or raw.get("volume24Hr") or 0.0)
        except (TypeError, ValueError):
            return 0.0

    @staticmethod
    def _market_total_volume(market: Market) -> float:
        raw = market.raw or {}
        try:
            return float(raw.get("volumeNum") or raw.get("volume") or 0.0)
        except (TypeError, ValueError):
            return 0.0

    def _market_is_liquid(self, market: Market) -> bool:
        return (
            self._market_volume24(market) >= self.min_vol24
            or self._market_total_volume(market) >= self.min_total_vol
        )

    async def generate_intents(self) -> list[TradeIntent]:
        cutoff = datetime.utcnow() - timedelta(minutes=self.copy_window_minutes)
        async with session_scope() as db:
            rows = (await db.execute(
                select(WhaleTrade, WhaleWallet, Market)
                .join(WhaleWallet, WhaleTrade.wallet == WhaleWallet.address)
                .join(Market, WhaleTrade.market_id == Market.id)
                .where(
                    WhaleTrade.ts >= cutoff,
                    WhaleWallet.active == True,  # noqa: E712
                    WhaleTrade.size_usdc >= self.min_whale_trade_usdc,
                    Market.resolved == False,  # noqa: E712
                )
                .order_by(WhaleTrade.ts.desc())
            )).all()

        intents: list[TradeIntent] = []
        seen: set[tuple[str, Outcome]] = set()
        for trade, wallet, market in rows:
            if not self._market_is_liquid(market):
                continue
            key = (trade.market_id, trade.outcome)
            if key in seen:
                continue
            seen.add(key)

            side_price = trade.price if trade.outcome == Outcome.YES else Decimal(1) - trade.price
            if not (Decimal(str(self.min_price)) <= side_price <= Decimal(str(self.max_price))):
                continue

            price_f = float(side_price)
            edge_bps = max(300, int(abs(0.5 - price_f) * 10_000))
            confidence = min(0.95, 0.65 + (float(wallet.total_pnl_usdc) / 1_000_000) * 0.05)

            # Cap intent to this strategy's allocation pct of the live bankroll.
            async with session_scope() as db:
                already_open = (await db.execute(
                    select(func.coalesce(func.sum(Bet.cost_basis_usdc), 0)).where(
                        Bet.strategy == self.name,
                        Bet.status == BetStatus.OPEN,
                    )
                )).scalar_one()
            allocation_budget = (
                Decimal(str(settings.initial_bankroll_usdc))
                * Decimal(str(self.allocation_pct))
                - Decimal(already_open)
            )
            if allocation_budget <= Decimal("1"):
                continue

            intents.append(TradeIntent(
                market_id=trade.market_id,
                outcome=trade.outcome,
                edge_bps=edge_bps,
                confidence=confidence,
                reasoning=(
                    f"smart_whale: {wallet.label or wallet.address[:8]} "
                    f"${float(trade.size_usdc):.0f} @ {float(trade.price):.3f} "
                    f"on liquid market (vol24h=${self._market_volume24(market):.0f})"
                ),
                strategy=self.name,
                max_size_usdc=allocation_budget,
            ))
        log.info("smart_whale.intents", count=len(intents))
        return intents
