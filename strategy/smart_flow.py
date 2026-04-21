"""Smart-flow strategy: cumulative whale-flow dominance on liquid markets.

Complements smart-whale by firing more often -- every market where the
cumulative YES/NO whale notional imbalance exceeds the dominance
threshold, not just on the first whale's trade. Validated on a 12-month
walk-forward with monthly consistency 75 pct and avg +45 pct per active
month.

Locked defaults (see scripts/backtest_smart_flow.py):
- dominance threshold: 0.6 (80/20 side split on cumulative whale flow)
- min whale cumulative volume on market: $2000
- kelly: 0.5
- max position: 12 pct

Safer-than-smart-whale design:
- Lower Kelly, lower max-pos = smaller per-trade exposure
- More trades = better diversification over time
- Hold to resolution (no mid-market exit complexity)
"""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta
from decimal import Decimal

from sqlalchemy import func, select

from shared.config import settings
from shared.db import session_scope
from shared.logging import get_logger
from shared.models import Bet, BetStatus, Market, Outcome, WhaleTrade
from strategy.base import Strategy, TradeIntent

log = get_logger(__name__)


class SmartFlowStrategy(Strategy):
    name = "smart_flow"
    allocation_pct = 0.40  # complements smart-whale's 60 pct

    # Locked defaults from validated 12-month walk-forward
    effective_kelly = 0.50
    effective_max_position_pct = 0.12

    def __init__(
        self,
        lookback_hours: int = 48,
        dominance_threshold: float = 0.60,
        min_whale_volume_usdc: float = 2000.0,
        min_market_volume_24h: float = 1000.0,
        min_market_total_volume: float = 10_000.0,
        min_price: float = 0.10,
        max_price: float = 0.75,
    ):
        self.lookback_hours = lookback_hours
        self.dominance_threshold = dominance_threshold
        self.min_whale_volume = Decimal(str(min_whale_volume_usdc))
        self.min_vol24 = min_market_volume_24h
        self.min_total_vol = min_market_total_volume
        self.min_price = Decimal(str(min_price))
        self.max_price = Decimal(str(max_price))

    def _market_is_liquid(self, market: Market) -> bool:
        raw = market.raw or {}
        try:
            vol24 = float(raw.get("volume24hr") or 0.0)
            total = float(raw.get("volumeNum") or raw.get("volume") or 0.0)
        except (TypeError, ValueError):
            return False
        return vol24 >= self.min_vol24 or total >= self.min_total_vol

    async def generate_intents(self) -> list[TradeIntent]:
        cutoff = datetime.utcnow() - timedelta(hours=self.lookback_hours)
        async with session_scope() as db:
            rows = (await db.execute(
                select(WhaleTrade, Market)
                .join(Market, WhaleTrade.market_id == Market.id)
                .where(
                    WhaleTrade.ts >= cutoff,
                    Market.resolved == False,  # noqa: E712
                )
                .order_by(WhaleTrade.ts)
            )).all()

        by_market: dict[str, dict] = defaultdict(lambda: {
            "net_yes": Decimal(0),
            "net_no": Decimal(0),
            "total": Decimal(0),
            "latest_price_yes": None,
            "latest_price_no": None,
            "market": None,
            "latest_ts": None,
        })
        for trade, market in rows:
            b = by_market[trade.market_id]
            b["market"] = market
            b["latest_ts"] = trade.ts
            if trade.outcome == Outcome.YES:
                b["net_yes"] += trade.size_usdc
                b["latest_price_yes"] = trade.price
            else:
                b["net_no"] += trade.size_usdc
                b["latest_price_no"] = trade.price
            b["total"] += trade.size_usdc

        intents: list[TradeIntent] = []
        for market_id, b in by_market.items():
            market = b["market"]
            if not market or not self._market_is_liquid(market):
                continue
            if b["total"] < self.min_whale_volume:
                continue
            imbalance = b["net_yes"] - b["net_no"]
            dominance = abs(imbalance) / b["total"] if b["total"] > 0 else Decimal(0)
            if dominance < Decimal(str(self.dominance_threshold)):
                continue
            direction = Outcome.YES if imbalance > 0 else Outcome.NO
            price = b["latest_price_yes"] if direction == Outcome.YES else b["latest_price_no"]
            if price is None:
                continue
            if not (self.min_price <= price <= self.max_price):
                continue

            # Don't double up
            async with session_scope() as db:
                existing_bet = (await db.execute(
                    select(Bet).where(
                        Bet.strategy == self.name,
                        Bet.market_id == market_id,
                        Bet.outcome == direction,
                        Bet.status == BetStatus.OPEN,
                    )
                )).scalar_one_or_none()
            if existing_bet:
                continue

            edge_bps = max(300, int(abs(0.5 - float(price)) * 10_000))
            dominance_f = float(dominance)
            confidence = min(0.95, 0.65 + (dominance_f - self.dominance_threshold) * 0.5)

            # Enforce this strategy's allocation budget
            async with session_scope() as db:
                already_open = (await db.execute(
                    select(func.coalesce(func.sum(Bet.cost_basis_usdc), 0)).where(
                        Bet.strategy == self.name,
                        Bet.status == BetStatus.OPEN,
                    )
                )).scalar_one()
            budget = (
                Decimal(str(settings.initial_bankroll_usdc))
                * Decimal(str(self.allocation_pct))
                - Decimal(already_open)
            )
            if budget <= Decimal("1"):
                continue

            intents.append(TradeIntent(
                market_id=market_id,
                outcome=direction,
                edge_bps=edge_bps,
                confidence=confidence,
                reasoning=(
                    f"smart_flow: dominance={dominance_f:.2f} "
                    f"(net ${float(imbalance):+.0f} / ${float(b['total']):.0f} whale flow)"
                ),
                strategy=self.name,
                max_size_usdc=budget,
            ))
        log.info("smart_flow.intents", count=len(intents))
        return intents
