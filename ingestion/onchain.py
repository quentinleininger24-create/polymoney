"""On-chain whale tracker: identify top Polymarket wallets and mirror their trades.

This is the highest-edge source for a small bankroll: you piggyback on traders
who have done the research. Data source: Dune Analytics queries (or direct
Polygon RPC event logs on Polymarket contracts).
"""

from datetime import datetime

import httpx

from shared.config import settings
from shared.logging import get_logger

log = get_logger(__name__)

DUNE_API = "https://api.dune.com/api/v1"
# Pre-built Dune query returning top Polymarket traders by realized PnL.
# Replace with a query you own after forking: https://dune.com/browse/queries
TOP_TRADERS_QUERY_ID = 3456789  # TODO(quentin): fork a public query and paste its ID


class WhaleTracker:
    def __init__(self) -> None:
        self._client = httpx.AsyncClient(
            timeout=30.0,
            headers={"X-Dune-API-Key": settings.dune_api_key} if settings.dune_api_key else {},
        )

    async def close(self) -> None:
        await self._client.aclose()

    async def refresh_top_wallets(self, limit: int = 50) -> list[dict]:
        if not settings.dune_api_key:
            log.warning("whale.no_dune_key")
            return []
        r = await self._client.get(
            f"{DUNE_API}/query/{TOP_TRADERS_QUERY_ID}/results",
            params={"limit": limit},
        )
        r.raise_for_status()
        rows = r.json().get("result", {}).get("rows", [])
        log.info("whale.top_fetched", count=len(rows))
        return rows

    async def get_recent_trades(self, wallet: str, since: datetime) -> list[dict]:
        """Fetch recent trades for a wallet via Polygon RPC (Alchemy)."""
        # Implementation: query Polymarket OrderFilled events via web3 / Alchemy
        # filtered by maker == wallet. Kept as a stub until query ID is set.
        _ = (wallet, since)
        return []


async def sync_whales() -> int:
    from sqlalchemy.dialects.postgresql import insert

    from shared.db import session_scope
    from shared.models import WhaleWallet

    tracker = WhaleTracker()
    try:
        rows = await tracker.refresh_top_wallets()
        async with session_scope() as db:
            for row in rows:
                stmt = insert(WhaleWallet).values(
                    address=row["wallet"].lower(),
                    label=row.get("label"),
                    total_pnl_usdc=row.get("pnl_usdc", 0),
                    trades_count=row.get("trades", 0),
                    active=True,
                    last_seen=datetime.utcnow(),
                    raw=row,
                )
                stmt = stmt.on_conflict_do_update(
                    index_elements=["address"],
                    set_={
                        "total_pnl_usdc": stmt.excluded.total_pnl_usdc,
                        "trades_count": stmt.excluded.trades_count,
                        "last_seen": datetime.utcnow(),
                        "raw": stmt.excluded.raw,
                    },
                )
                await db.execute(stmt)
        return len(rows)
    finally:
        await tracker.close()
