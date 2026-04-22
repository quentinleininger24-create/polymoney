"""Smart-flow strategy: cumulative whale-flow dominance on liquid markets.

Complements smart-whale by firing more often -- every market where the
cumulative YES/NO whale notional imbalance exceeds the dominance
threshold, not just on the first whale's trade.

Locked defaults (see scripts/backtest_smart_flow_v2.py):
- dominance threshold: 0.6 (80/20 side split on cumulative whale flow)
- min whale cumulative volume on market: $2000
- kelly: 0.5
- max position: 12 pct
- MONTHLY DRAWDOWN CIRCUIT: 10 pct -- halts new entries for rest of month
  once MTD PnL drops 10 pct below the bankroll's start-of-month value.
  Adds +9 pts of worst-month protection (cut -22 pct to -13 pct) and
  halves max DD (22 pct -> 12 pct) at the cost of 8 pts of avg monthly.
  Validated on 12-month walk-forward, $500 bankroll:
    n=25 wr 60 pct pnl +$1485 sharpe 2.75 DD 12 pct P(+) 99 pct
    worst -13 pct avg +37 pct consistency 67 pct

Safer-than-smart-whale design:
- Lower Kelly, lower max-pos = smaller per-trade exposure
- More trades = better diversification over time
- Hold to resolution (no mid-market exit complexity)
- Monthly drawdown stop bounds the tail each month

Note: the walk-forward whale-accuracy-filter variant (v2 with WR gate)
was tested and REDUCED performance drastically vs v1 (n=12 vs 34, avg
monthly +4.9 pct vs +45 pct). Quality filtering combined with flow
weighting over-constrained the signal. Kept as a research branch in
scripts/backtest_smart_flow_v2.py with `--min-whale-wr` flag, but NOT
enabled here. The monthly drawdown circuit was the only v2 feature that
isolated cleanly as a net improvement.
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
    """Two preset profiles available via `profile` param:

    - "safe" (default): dominance 0.60, Kelly 0.5, max 12 pct, monthly-stop 10 pct.
      Validated 12-month: +37 pct/mo, worst -13 pct, DD 12 pct, 67 pct consistency.

    - "aggressive": dominance 0.50, Kelly 0.5, max 15 pct, no monthly stop.
      Validated 12-month compound on $500: ended $677k (1356x), max DD 50 pct,
      trough -44 pct of start, NEVER ruined, 100 pct monthly consistency.
      Worst single month -23 pct. Best months +616 pct and +445 pct.
      Trade this when the user explicitly opts in to high variance for
      high compounding, understanding the drawdown.
    """

    name = "smart_flow"
    allocation_pct = 0.40  # when paired with smart_whale; set to 1.0 if solo

    # Live config defaults (populated by __init__)
    effective_kelly: float
    effective_max_position_pct: float
    monthly_drawdown_stop_pct: float

    def __init__(
        self,
        profile: str = "safe",
        lookback_hours: int = 48,
        min_market_volume_24h: float | None = None,
        min_market_total_volume: float | None = None,
        min_price: float = 0.10,
        max_price: float = 0.75,
    ):
        if profile not in ("safe", "aggressive"):
            raise ValueError(f"profile must be 'safe' or 'aggressive', got {profile}")
        self.profile = profile
        if profile == "safe":
            dominance_threshold = 0.60
            min_whale_volume_usdc = 2000.0
            self.effective_kelly = 0.50
            self.effective_max_position_pct = 0.12
            self.monthly_drawdown_stop_pct = 0.10
            self.allocation_pct = 0.40
            default_min_vol24 = 1000.0
            default_min_total = 10_000.0
        else:  # aggressive
            dominance_threshold = 0.50
            min_whale_volume_usdc = 2000.0
            self.effective_kelly = 0.50
            self.effective_max_position_pct = 0.15
            self.monthly_drawdown_stop_pct = 1.0  # effectively disabled
            self.allocation_pct = 1.0  # solo mode; scale down when combined
            default_min_vol24 = 1000.0
            default_min_total = 10_000.0

        self.lookback_hours = lookback_hours
        self.dominance_threshold = dominance_threshold
        self.min_whale_volume = Decimal(str(min_whale_volume_usdc))
        self.min_vol24 = min_market_volume_24h if min_market_volume_24h is not None else default_min_vol24
        self.min_total_vol = min_market_total_volume if min_market_total_volume is not None else default_min_total
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

    async def _month_is_halted(self) -> bool:
        """True if this strategy's month-to-date PnL is at or below the
        monthly-drawdown stop (pct of initial bankroll)."""
        month_start = datetime.utcnow().replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        async with session_scope() as db:
            mtd = (await db.execute(
                select(func.coalesce(func.sum(Bet.pnl_usdc), 0)).where(
                    Bet.strategy == self.name,
                    Bet.closed_at >= month_start,
                    Bet.status != BetStatus.OPEN,
                )
            )).scalar_one()
        limit = Decimal(str(settings.initial_bankroll_usdc)) * Decimal(str(-self.monthly_drawdown_stop_pct))
        return Decimal(mtd) <= limit

    async def generate_intents(self) -> list[TradeIntent]:
        if await self._month_is_halted():
            log.warning("smart_flow.month_halted", stop_pct=self.monthly_drawdown_stop_pct)
            return []
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
