"""News ingestion: NewsAPI, GDELT, RSS feeds. Politics-focused keywords."""

from datetime import datetime, timedelta

import httpx

from shared.config import settings
from shared.logging import get_logger

log = get_logger(__name__)

POLITICS_KEYWORDS = [
    "election", "president", "congress", "senate", "poll",
    "trump", "biden", "harris", "primary", "debate",
    "supreme court", "impeachment", "campaign",
]


class NewsFetcher:
    def __init__(self) -> None:
        self._client = httpx.AsyncClient(timeout=20.0)

    async def close(self) -> None:
        await self._client.aclose()

    async def fetch_newsapi(self, hours_back: int = 1) -> list[dict]:
        if not settings.newsapi_key:
            return []
        since = datetime.utcnow() - timedelta(hours=hours_back)
        q = " OR ".join(f'"{kw}"' for kw in POLITICS_KEYWORDS)
        r = await self._client.get(
            "https://newsapi.org/v2/everything",
            params={
                "q": q,
                "from": since.isoformat(),
                "language": "en",
                "sortBy": "publishedAt",
                "pageSize": 100,
                "apiKey": settings.newsapi_key,
            },
        )
        r.raise_for_status()
        articles = r.json().get("articles", [])
        log.info("news.newsapi_fetched", count=len(articles))
        return articles

    async def fetch_gdelt(self, hours_back: int = 1) -> list[dict]:
        if not settings.gdelt_enabled:
            return []
        r = await self._client.get(
            "https://api.gdeltproject.org/api/v2/doc/doc",
            params={
                "query": "(election OR politics OR congress) sourcelang:english",
                "mode": "ArtList",
                "format": "json",
                "maxrecords": 100,
                "timespan": f"{hours_back}h",
            },
        )
        r.raise_for_status()
        return r.json().get("articles", [])


async def ingest_news() -> int:
    """Fetch and persist recent political news. Returns count inserted."""
    from sqlalchemy.dialects.postgresql import insert

    from shared.db import session_scope
    from shared.models import Event

    fetcher = NewsFetcher()
    try:
        newsapi = await fetcher.fetch_newsapi()
        gdelt = await fetcher.fetch_gdelt()
        count = 0
        async with session_scope() as db:
            for a in newsapi:
                stmt = insert(Event).values(
                    source="newsapi",
                    source_id=a.get("url"),
                    ts=datetime.fromisoformat(a["publishedAt"].replace("Z", "+00:00")),
                    url=a.get("url"),
                    author=a.get("author"),
                    title=a.get("title"),
                    content=a.get("description") or a.get("content") or "",
                    raw=a,
                ).on_conflict_do_nothing(index_elements=["source", "source_id"])
                result = await db.execute(stmt)
                if result.rowcount:
                    count += 1
            for a in gdelt:
                stmt = insert(Event).values(
                    source="gdelt",
                    source_id=a.get("url"),
                    ts=datetime.strptime(a["seendate"], "%Y%m%dT%H%M%SZ") if a.get("seendate") else datetime.utcnow(),
                    url=a.get("url"),
                    title=a.get("title"),
                    content=a.get("title", ""),
                    raw=a,
                ).on_conflict_do_nothing(index_elements=["source", "source_id"])
                result = await db.execute(stmt)
                if result.rowcount:
                    count += 1
        log.info("news.ingested", new=count)
        return count
    finally:
        await fetcher.close()
