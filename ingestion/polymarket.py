"""Polymarket CLOB ingestion: fetch active political markets + order books."""

import json
from datetime import datetime
from decimal import Decimal

import httpx

from shared.config import settings
from shared.logging import get_logger

log = get_logger(__name__)

GAMMA_API = "https://gamma-api.polymarket.com"
CLOB_API = "https://clob.polymarket.com"


def _parse_tokens(m: dict) -> dict:
    """Gamma returns clobTokenIds and outcomes as JSON-encoded strings; pair them."""
    raw_ids = m.get("clobTokenIds")
    raw_outcomes = m.get("outcomes")
    if not raw_ids or not raw_outcomes:
        return {}
    try:
        ids = json.loads(raw_ids) if isinstance(raw_ids, str) else raw_ids
        outs = json.loads(raw_outcomes) if isinstance(raw_outcomes, str) else raw_outcomes
    except (json.JSONDecodeError, TypeError):
        return {}
    out: dict[str, str] = {}
    for name, tid in zip(outs, ids):
        key = str(name).strip().upper()
        if key == "YES":
            out["YES"] = str(tid)
        elif key == "NO":
            out["NO"] = str(tid)
    return out


def _parse_end_date(m: dict) -> datetime | None:
    for k in ("endDate", "end_date_iso", "endDateIso"):
        v = m.get(k)
        if v:
            try:
                return datetime.fromisoformat(str(v).replace("Z", "+00:00"))
            except ValueError:
                continue
    return None


class PolymarketReader:
    """Read-only market data fetcher. Execution goes through execution/polymarket_client."""

    def __init__(self) -> None:
        self._client = httpx.AsyncClient(timeout=20.0)

    async def close(self) -> None:
        await self._client.aclose()

    async def list_active_markets(
        self, vertical: str | None = "politics", limit: int = 200
    ) -> list[dict]:
        params: dict = {
            "closed": "false",
            "active": "true",
            "limit": limit,
            "order": "volume24hr",
            "ascending": "false",
        }
        if vertical:
            params["tag_slug"] = vertical
        r = await self._client.get(f"{GAMMA_API}/markets", params=params)
        r.raise_for_status()
        markets = r.json()
        log.info("polymarket.markets_fetched", count=len(markets), vertical=vertical)
        return markets

    async def get_orderbook(self, token_id: str) -> dict:
        r = await self._client.get(f"{CLOB_API}/book", params={"token_id": token_id})
        r.raise_for_status()
        return r.json()

    async def get_midpoint(self, token_id: str) -> Decimal | None:
        book = await self.get_orderbook(token_id)
        bids = book.get("bids", [])
        asks = book.get("asks", [])
        if not bids or not asks:
            return None
        best_bid = Decimal(bids[0]["price"])
        best_ask = Decimal(asks[0]["price"])
        return (best_bid + best_ask) / 2

    async def get_trades(self, market_id: str, since: datetime | None = None) -> list[dict]:
        params: dict = {"market": market_id}
        if since:
            params["from"] = int(since.timestamp())
        r = await self._client.get(f"{CLOB_API}/trades", params=params)
        r.raise_for_status()
        return r.json()


async def snapshot_markets() -> int:
    """Fetch active political markets and upsert into DB. Returns count."""
    from sqlalchemy.dialects.postgresql import insert

    from shared.db import session_scope
    from shared.models import Market

    reader = PolymarketReader()
    try:
        markets = await reader.list_active_markets(vertical=settings.focus_vertical.value)
        async with session_scope() as db:
            for m in markets:
                tokens = _parse_tokens(m)
                if not tokens or "YES" not in tokens:
                    continue  # skip markets we can't trade (no YES/NO binary outcome)
                stmt = insert(Market).values(
                    id=m["conditionId"],
                    slug=m.get("slug", ""),
                    question=m.get("question", ""),
                    category=settings.focus_vertical.value,
                    end_date=_parse_end_date(m),
                    resolved=m.get("closed", False),
                    tokens=tokens,
                    raw=m,
                )
                stmt = stmt.on_conflict_do_update(
                    index_elements=["id"],
                    set_={
                        "question": stmt.excluded.question,
                        "resolved": stmt.excluded.resolved,
                        "tokens": stmt.excluded.tokens,
                        "raw": stmt.excluded.raw,
                        "updated_at": datetime.utcnow(),
                    },
                )
                await db.execute(stmt)
        return len(markets)
    finally:
        await reader.close()
