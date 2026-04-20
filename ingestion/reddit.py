"""Reddit ingestion (PRAW). Political subreddits."""

from datetime import datetime, timedelta

import praw

from shared.config import settings
from shared.logging import get_logger

log = get_logger(__name__)

SUBREDDITS = ["politics", "PoliticalDiscussion", "Ask_Politics", "neoliberal", "moderatepolitics"]


async def ingest_reddit(hours_back: int = 2, min_score: int = 50) -> int:
    if not (settings.reddit_client_id and settings.reddit_client_secret):
        log.warning("reddit.no_creds")
        return 0
    from sqlalchemy.dialects.postgresql import insert

    from shared.db import session_scope
    from shared.models import Event

    reddit = praw.Reddit(
        client_id=settings.reddit_client_id,
        client_secret=settings.reddit_client_secret,
        user_agent=settings.reddit_user_agent,
    )
    since_ts = (datetime.utcnow() - timedelta(hours=hours_back)).timestamp()
    count = 0
    async with session_scope() as db:
        for sub in SUBREDDITS:
            for post in reddit.subreddit(sub).new(limit=100):
                if post.created_utc < since_ts or post.score < min_score:
                    continue
                stmt = insert(Event).values(
                    source="reddit",
                    source_id=post.id,
                    ts=datetime.utcfromtimestamp(post.created_utc),
                    url=f"https://reddit.com{post.permalink}",
                    author=str(post.author) if post.author else None,
                    title=post.title,
                    content=post.selftext or post.title,
                    raw={"sub": sub, "score": post.score, "num_comments": post.num_comments},
                ).on_conflict_do_nothing(index_elements=["source", "source_id"])
                r = await db.execute(stmt)
                if r.rowcount:
                    count += 1
    log.info("reddit.ingested", new=count)
    return count
