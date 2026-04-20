"""Backtesting engine. Replay historical events + prices against a strategy.

Critical for a 100 EUR bankroll: no strategy gets real capital until it shows
a credible Sharpe on a sample of resolved markets.
"""

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal

from sqlalchemy import select

from shared.db import session_scope
from shared.logging import get_logger
from shared.models import Market, PriceTick

log = get_logger(__name__)


@dataclass
class BacktestResult:
    total_trades: int = 0
    wins: int = 0
    losses: int = 0
    total_pnl_usdc: Decimal = Decimal(0)
    roi_pct: float = 0.0
    sharpe: float | None = None
    max_drawdown_pct: float = 0.0
    by_strategy: dict[str, "BacktestResult"] = field(default_factory=dict)


async def replay(
    strategy_name: str,
    start: datetime,
    end: datetime,
    initial_bankroll: Decimal = Decimal(100),
) -> BacktestResult:
    """Replay resolved markets in [start, end] as if we traded with the strategy."""
    async with session_scope() as db:
        markets = (await db.execute(
            select(Market).where(
                Market.resolved == True,  # noqa: E712
                Market.end_date >= start,
                Market.end_date <= end,
            )
        )).scalars().all()

    # TODO(quentin): reconstruct signals from Event+Signal tables at each historical
    # timestamp, size positions via Kelly, mark-to-resolution, compute stats.
    # Placeholder skeleton — runs but returns empty result until implemented.
    result = BacktestResult()
    log.info("backtest.run", strategy=strategy_name, markets=len(markets), bankroll=str(initial_bankroll))
    return result
