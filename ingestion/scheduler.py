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


async def _safe(name: str, coro_fn):
    try:
        await coro_fn()
    except Exception as e:  # noqa: BLE001
        log.error("ingestion.task_failed", task=name, err=str(e))


async def job_markets():   await _safe("markets",   snapshot_markets)
async def job_prices():    await _safe("prices",    snapshot_current_prices)
async def job_news():      await _safe("news",      ingest_news)
async def job_twitter():   await _safe("twitter",   ingest_tweets)
async def job_reddit():    await _safe("reddit",    ingest_reddit)
async def job_whales():    await _safe("whales",    sync_whales)
async def job_scoring():   await _safe("scoring",   score_newly_resolved)


async def main() -> None:
    configure_logging()
    sched = AsyncIOScheduler()

    # Polymarket markets refresh (10 min)
    sched.add_job(job_markets, "interval", minutes=10, next_run_time=None)

    # Price snapshot (5 min) -- feeds PriceTick used by reflection decisive-move detection
    sched.add_job(job_prices, "interval", minutes=5)

    # News ingestion (5 min -- politics moves fast on breaking news)
    sched.add_job(job_news, "interval", minutes=5)

    # Twitter (2 min -- real-time edge source)
    sched.add_job(job_twitter, "interval", minutes=2)

    # Reddit (15 min)
    sched.add_job(job_reddit, "interval", minutes=15)

    # Whales (hourly -- leaderboard does not shift fast)
    sched.add_job(job_whales, "interval", hours=1)

    # Post-resolution scoring -- the feedback loop that powers reflection
    sched.add_job(job_scoring, "interval", hours=1)

    sched.start()
    log.info("ingestion.scheduler_started")

    # Kick off one run of each on startup so we do not wait an hour for whales
    await asyncio.gather(
        job_markets(), job_whales(),
        return_exceptions=True,
    )

    stop = asyncio.Event()
    await stop.wait()


if __name__ == "__main__":
    asyncio.run(main())
