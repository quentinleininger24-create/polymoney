"""Polymarket CLOB client wrapper using py-clob-client.

Supports paper mode (simulated fills) and live mode (real orders on Polygon).
"""

from decimal import Decimal

from shared.config import Mode, settings
from shared.logging import get_logger
from shared.models import OrderSide, Outcome

log = get_logger(__name__)


class PolymarketExecutor:
    def __init__(self) -> None:
        self.mode = settings.mode
        self._live_client = None
        if self.mode == Mode.LIVE:
            self._init_live()

    def _init_live(self) -> None:
        from py_clob_client.client import ClobClient
        from py_clob_client.clob_types import ApiCreds

        if not settings.wallet_private_key:
            raise RuntimeError("WALLET_PRIVATE_KEY missing for live mode")
        creds = ApiCreds(
            api_key=settings.polymarket_api_key,
            api_secret=settings.polymarket_api_secret,
            api_passphrase=settings.polymarket_api_passphrase,
        )
        self._live_client = ClobClient(
            host="https://clob.polymarket.com",
            key=settings.wallet_private_key,
            chain_id=137,
            creds=creds,
            signature_type=2,
            funder=settings.wallet_address,
        )
        log.info("polymarket.live_client_ready")

    async def place_limit(
        self,
        token_id: str,
        side: OrderSide,
        size_shares: Decimal,
        price: Decimal,
    ) -> dict:
        if self.mode == Mode.PAPER:
            log.info("paper.order", token=token_id, side=side.value, size=str(size_shares), price=str(price))
            return {
                "success": True,
                "orderID": f"paper-{token_id[:8]}-{int(price * 10000)}",
                "filled": str(size_shares),
                "mode": "paper",
            }
        from py_clob_client.clob_types import OrderArgs

        args = OrderArgs(
            token_id=token_id,
            price=float(price),
            size=float(size_shares),
            side=side.value,
        )
        signed = self._live_client.create_order(args)  # type: ignore[union-attr]
        return self._live_client.post_order(signed)  # type: ignore[union-attr]

    def shares_from_usdc(self, usdc: Decimal, price: Decimal) -> Decimal:
        if price <= 0:
            return Decimal(0)
        return (usdc / price).quantize(Decimal("0.0001"))

    def token_for_outcome(self, market_tokens: dict, outcome: Outcome) -> str | None:
        return market_tokens.get(outcome.value)
