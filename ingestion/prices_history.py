"""CLOB prices-history fetcher.

GET https://clob.polymarket.com/prices-history
Query params:
  market   — token_id (one of the YES/NO clob tokens, not conditionId)
  interval — '1m' | '1h' | '1d' | 'max'
  fidelity — granularity in minutes (e.g. 60 -> hourly points)

Returns: {"history": [{"t": unix_seconds, "p": price_0_to_1}, ...]}

Used for:
1. Hourly snapshots of our tracked markets, persisted as PriceTick.
2. Historical price lookup for backtesting LLM signals: given (token_id, ts),
   return the price that was quoted on the market at that moment.
"""

from __future__ import annotations

import bisect
from datetime import datetime, timezone
from decimal import Decimal

import httpx

from shared.db import session_scope
from shared.logging import get_logger
from shared.models import Market, PriceTick

log = get_logger(__name__)

CLOB_HISTORY_URL = "https://clob.polymarket.com/prices-history"


class PricesHistoryClient:
    def __init__(self) -> None:
        self._client = httpx.AsyncClient(timeout=20.0)

    async def close(self) -> None:
        await self._client.aclose()

    async def fetch(self, token_id: str, interval: str = "1d", fidelity: int = 60) -> list[dict]:
        r = await self._client.get(CLOB_HISTORY_URL, params={
            "market": token_id, "interval": interval, "fidelity": fidelity,
        })
        r.raise_for_status()
        data = r.json()
        return data.get("history", []) or []


async def snapshot_current_prices() -> int:
    """For every tracked unresolved market, fetch the latest price and append
    a PriceTick. Called on cadence by the ingestion scheduler."""
    from sqlalchemy import select
    from sqlalchemy.dialects.postgresql import insert

    client = PricesHistoryClient()
    count = 0
    try:
        async with session_scope() as db:
            markets = (await db.execute(
                select(Market).where(Market.resolved == False)  # noqa: E712
            )).scalars().all()

        for m in markets:
            yes_token = (m.tokens or {}).get("YES")
            if not yes_token:
                continue
            try:
                points = await client.fetch(yes_token, interval="1h", fidelity=60)
            except Exception as e:  # noqa: BLE001
                log.warning("prices.fetch_failed", market=m.id[:10], err=str(e))
                continue
            if not points:
                continue
            last = points[-1]
            yes_mid = Decimal(str(last["p"]))
            ts = datetime.fromtimestamp(int(last["t"]), tz=timezone.utc).replace(tzinfo=None)
            async with session_scope() as db:
                db.add(PriceTick(
                    market_id=m.id,
                    ts=ts,
                    yes_mid=yes_mid,
                    yes_bid=None,
                    yes_ask=None,
                ))
                count += 1
        log.info("prices.snapshot", markets=len(markets), points=count)
    finally:
        await client.close()
    return count


async def backfill_market_history(
    market_id: str, interval: str = "max", fidelity: int = 60
) -> int:
    """One-off: pull the full price history of a resolved market so LLM
    analyst replays can look up what the price was at any past timestamp."""
    from sqlalchemy import select

    async with session_scope() as db:
        m = (await db.execute(
            select(Market).where(Market.id == market_id)
        )).scalar_one_or_none()
    if not m:
        return 0
    yes_token = (m.tokens or {}).get("YES")
    if not yes_token:
        return 0

    client = PricesHistoryClient()
    try:
        points = await client.fetch(yes_token, interval=interval, fidelity=fidelity)
    finally:
        await client.close()

    async with session_scope() as db:
        for pt in points:
            db.add(PriceTick(
                market_id=market_id,
                ts=datetime.fromtimestamp(int(pt["t"]), tz=timezone.utc).replace(tzinfo=None),
                yes_mid=Decimal(str(pt["p"])),
            ))
    return len(points)


# --- In-memory price-at-time lookup for backtests ---

_history_cache: dict[str, list[tuple[int, float]]] = {}


async def price_at(token_id: str, ts: datetime, max_gap_seconds: int = 3600) -> float | None:
    """Return the market price at timestamp `ts` (or within max_gap).
    Caches the full history per token so repeated lookups are O(log n)."""
    if token_id not in _history_cache:
        client = PricesHistoryClient()
        try:
            points = await client.fetch(token_id, interval="max", fidelity=60)
        finally:
            await client.close()
        _history_cache[token_id] = sorted((int(p["t"]), float(p["p"])) for p in points)
    hist = _history_cache[token_id]
    if not hist:
        return None
    target = int(ts.timestamp())
    idx = bisect.bisect_left(hist, (target, 0))
    candidates: list[tuple[int, float]] = []
    if idx < len(hist):
        candidates.append(hist[idx])
    if idx > 0:
        candidates.append(hist[idx - 1])
    if not candidates:
        return None
    best_ts, best_p = min(candidates, key=lambda p: abs(p[0] - target))
    if abs(best_ts - target) > max_gap_seconds:
        return None
    return best_p
