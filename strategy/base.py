"""Strategy base class. Each strat consumes signals and emits trade intents."""

from abc import ABC, abstractmethod
from dataclasses import dataclass
from decimal import Decimal

from shared.models import Outcome


@dataclass
class TradeIntent:
    market_id: str
    outcome: Outcome
    edge_bps: int
    confidence: float
    reasoning: str
    strategy: str
    max_size_usdc: Decimal | None = None  # strat-specific cap; risk layer enforces global


class Strategy(ABC):
    name: str = "base"
    allocation_pct: float = 0.0  # fraction of bankroll allocated to this strat

    @abstractmethod
    async def generate_intents(self) -> list[TradeIntent]:
        """Produce trade intents from recent signals/state. Called each tick."""

    async def on_fill(self, market_id: str, filled_usdc: Decimal) -> None:
        """Hook after an order fills (optional override)."""

    async def on_resolution(self, market_id: str, won: bool, pnl_usdc: Decimal) -> None:
        """Hook when a bet this strat opened resolves (optional override)."""
