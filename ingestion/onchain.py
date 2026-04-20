"""Whale tracker via Polymarket public data API (no Dune key needed).

Endpoints (public, no auth):
- Leaderboard:  https://lb-api.polymarket.com/profit?window={1d|7d|30d|all}&limit=N
- User trades:  https://data-api.polymarket.com/trades?user=<wallet>&limit=N

Strategy: union of top-50 30d and top-50 all-time -> our whale set.
For each whale, pull recent trades and only keep ones for markets we already
track (politics-focused via ingestion.polymarket.snapshot_markets).
"""

from datetime import datetime, timedelta

import httpx
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert

from shared.db import session_scope
from shared.logging import get_logger
from shared.models import Market, Outcome, OrderSide, WhaleTrade, WhaleWallet

log = get_logger(__name__)

LB_URL = "https://lb-api.polymarket.com/profit"
TRADES_URL = "https://data-api.polymarket.com/trades"


class WhaleTracker:
    def __init__(self) -> None:
        self._client = httpx.AsyncClient(timeout=20.0)

    async def close(self) -> None:
        await self._client.aclose()

    async def fetch_top(self, window: str = "30d", limit: int = 50) -> list[dict]:
        r = await self._client.get(LB_URL, params={"window": window, "limit": limit})
        r.raise_for_status()
        return r.json()

    async def fetch_user_trades(self, wallet: str, limit: int = 100) -> list[dict]:
        r = await self._client.get(TRADES_URL, params={"user": wallet, "limit": limit})
        r.raise_for_status()
        return r.json()


async def _upsert_whale(db, row: dict) -> None:
    stmt = insert(WhaleWallet).values(
        address=row["proxyWallet"].lower(),
        label=row.get("pseudonym") or row.get("name"),
        total_pnl_usdc=float(row.get("amount", 0)),
        active=True,
        last_seen=datetime.utcnow(),
        raw=row,
    )
    stmt = stmt.on_conflict_do_update(
        index_elements=["address"],
        set_={
            "total_pnl_usdc": stmt.excluded.total_pnl_usdc,
            "label": stmt.excluded.label,
            "last_seen": datetime.utcnow(),
            "raw": stmt.excluded.raw,
        },
    )
    await db.execute(stmt)


def _map_outcome(trade: dict) -> Outcome | None:
    idx = trade.get("outcomeIndex")
    if idx == 0:
        return Outcome.YES
    if idx == 1:
        return Outcome.NO
    name = (trade.get("outcome") or "").strip().lower()
    if name == "yes":
        return Outcome.YES
    if name == "no":
        return Outcome.NO
    return None


async def _persist_trade(db, t: dict, known_markets: set[str]) -> bool:
    condition_id = t.get("conditionId")
    if not condition_id or condition_id not in known_markets:
        return False
    outcome = _map_outcome(t)
    if outcome is None:
        return False
    tx = t.get("transactionHash")
    if not tx:
        return False
    stmt = insert(WhaleTrade).values(
        wallet=t["proxyWallet"].lower(),
        market_id=condition_id,
        ts=datetime.utcfromtimestamp(int(t["timestamp"])),
        side=OrderSide.BUY if t.get("side", "").upper() == "BUY" else OrderSide.SELL,
        outcome=outcome,
        size_usdc=float(t.get("size", 0)) * float(t.get("price", 0)),
        price=float(t.get("price", 0)),
        tx_hash=tx,
    ).on_conflict_do_nothing(index_elements=["tx_hash"])
    r = await db.execute(stmt)
    return bool(r.rowcount)


async def sync_whales(
    lookback_minutes: int = 60,
    leaderboard_limit: int = 50,
) -> dict:
    """Refresh whale set from leaderboard, then ingest their recent trades on
    markets we already track. Returns counts for logging."""
    tracker = WhaleTracker()
    try:
        recent = await tracker.fetch_top(window="30d", limit=leaderboard_limit)
        all_time = await tracker.fetch_top(window="all", limit=leaderboard_limit)
        seen: dict[str, dict] = {}
        for row in recent + all_time:
            seen[row["proxyWallet"].lower()] = row

        cutoff = datetime.utcnow() - timedelta(minutes=lookback_minutes)

        async with session_scope() as db:
            for row in seen.values():
                await _upsert_whale(db, row)
            markets_rows = (await db.execute(select(Market.id))).scalars().all()
            known_markets = set(markets_rows)

        trades_new = 0
        for wallet in seen:
            try:
                trades = await tracker.fetch_user_trades(wallet, limit=50)
            except Exception as e:  # noqa: BLE001
                log.warning("whale.trades_failed", wallet=wallet[:8], err=str(e))
                continue
            async with session_scope() as db:
                for t in trades:
                    try:
                        ts = datetime.utcfromtimestamp(int(t["timestamp"]))
                    except (KeyError, ValueError, TypeError):
                        continue
                    if ts < cutoff:
                        continue
                    if await _persist_trade(db, t, known_markets):
                        trades_new += 1

        log.info("whale.sync", whales=len(seen), trades_new=trades_new, known_markets=len(known_markets))
        return {"whales": len(seen), "trades_new": trades_new}
    finally:
        await tracker.close()
