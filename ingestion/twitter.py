"""Twitter/X ingestion via tweepy. Curated political accounts list."""

from datetime import datetime, timedelta

import tweepy

from shared.config import settings
from shared.logging import get_logger

log = get_logger(__name__)

POLITICAL_ACCOUNTS = [
    "nytpolitics", "politico", "axios", "PunchBowlNews",
    "NateSilver538", "harryenten", "Redistrict",
    "maggieNYT", "JakeSherman", "marcacaputo",
    "NatePrzepra538", "GElliottMorris",
]


async def ingest_tweets(hours_back: int = 1) -> int:
    if not settings.twitter_bearer_token:
        log.warning("twitter.no_token")
        return 0
    from sqlalchemy.dialects.postgresql import insert

    from shared.db import session_scope
    from shared.models import Event

    client = tweepy.Client(bearer_token=settings.twitter_bearer_token, wait_on_rate_limit=False)
    since = datetime.utcnow() - timedelta(hours=hours_back)
    count = 0
    async with session_scope() as db:
        for handle in POLITICAL_ACCOUNTS:
            try:
                user = client.get_user(username=handle)
                if not user.data:
                    continue
                tweets = client.get_users_tweets(
                    id=user.data.id,
                    start_time=since,
                    max_results=50,
                    tweet_fields=["created_at", "public_metrics"],
                )
                for t in tweets.data or []:
                    stmt = insert(Event).values(
                        source="twitter",
                        source_id=str(t.id),
                        ts=t.created_at,
                        url=f"https://x.com/{handle}/status/{t.id}",
                        author=handle,
                        content=t.text,
                        raw={"metrics": t.public_metrics},
                    ).on_conflict_do_nothing(index_elements=["source", "source_id"])
                    r = await db.execute(stmt)
                    if r.rowcount:
                        count += 1
            except Exception as e:  # noqa: BLE001
                log.warning("twitter.account_failed", handle=handle, err=str(e))
    log.info("twitter.ingested", new=count)
    return count
