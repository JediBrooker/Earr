"""Runs the re-search pass on an interval that can be changed from settings."""

from contextlib import asynccontextmanager

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from fastapi import FastAPI

from app.internal.ranking.quality import MIN_RESEARCH_INTERVAL, quality_config
from app.internal.research import research_outstanding_requests
from app.util.db import get_session
from app.util.log import logger

JOB_ID = "research_outstanding"

_scheduler: AsyncIOScheduler | None = None


def reschedule(seconds: int) -> None:
    """Applies a new interval without a restart."""
    if _scheduler is None:
        return
    _scheduler.reschedule_job(
        JOB_ID, trigger="interval", seconds=max(MIN_RESEARCH_INTERVAL, seconds)
    )
    logger.info("Re-search interval updated", seconds=seconds)


@asynccontextmanager
async def lifespan(app: FastAPI):
    _ = app
    global _scheduler

    with next(get_session()) as session:
        interval = quality_config.get_research_interval(session)

    _scheduler = AsyncIOScheduler()
    _ = _scheduler.add_job(
        research_outstanding_requests,
        "interval",
        seconds=interval,
        id=JOB_ID,
        max_instances=1,
        coalesce=True,
    )
    _scheduler.start()
    logger.debug("Re-search scheduler started", interval_seconds=interval)
    yield
    _scheduler.shutdown()
    _scheduler = None
