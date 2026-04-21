"""APScheduler-based ingestion loop. Runs all collectors on cadence."""

import asyncio

from apscheduler.schedulers.asyncio import AsyncIOScheduler

from ingestion.news import ingest_news
from ingestion.onchain import sync_whales
from ingestion.polymarket import snapshot_markets
from ingestion.prices_history import snapshot_current_prices
from ingestion.reddit import ingest_reddit
from ingestion.twitter import ingest_tweets
from reflection.scoring_loop import score_newly_resolved
from shared.logging import configure_logging, get_logger

log = get_logger(__name__)


async def _safe(name: str, coro):
    try:
        await coro
    except Exception as e:  # noqa: BLE001
        log.error("ingestion.task_failed", task=name, err=str(e))


async def main() -> None:
    configure_logging()
    sched = AsyncIOScheduler()

    # Polymarket markets refresh (10 min)
    sched.add_job(lambda: asyncio.create_task(_safe("markets", snapshot_markets())), "interval", minutes=10)

    # Price snapshot (5 min) — fuels PriceTick table used by reflection decisive-move detection
    sched.add_job(lambda: asyncio.create_task(_safe("prices", snapshot_current_prices())), "interval", minutes=5)

    # News ingestion (5 min — politics moves fast on breaking news)
    sched.add_job(lambda: asyncio.create_task(_safe("news", ingest_news())), "interval", minutes=5)

    # Twitter (2 min — real-time edge source)
    sched.add_job(lambda: asyncio.create_task(_safe("twitter", ingest_tweets())), "interval", minutes=2)

    # Reddit (15 min)
    sched.add_job(lambda: asyncio.create_task(_safe("reddit", ingest_reddit())), "interval", minutes=15)

    # Whales (hourly — leaderboard doesn't shift fast)
    sched.add_job(lambda: asyncio.create_task(_safe("whales", sync_whales())), "interval", hours=1)

    # Post-resolution scoring — the feedback loop that powers reflection
    sched.add_job(lambda: asyncio.create_task(_safe("scoring", score_newly_resolved())), "interval", hours=1)

    sched.start()
    log.info("ingestion.scheduler_started")

    # Run forever
    stop = asyncio.Event()
    await stop.wait()


if __name__ == "__main__":
    asyncio.run(main())
