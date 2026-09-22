"""Runs the library watcher on an interval that can be changed from settings."""

from contextlib import asynccontextmanager

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from fastapi import FastAPI

from app.internal.library.config import library_config
from app.internal.library.watcher import scan_downloads
from app.util.db import get_session
from app.util.log import logger

JOB_ID = "library_scan"

_scheduler: AsyncIOScheduler | None = None


def reschedule(seconds: int) -> None:
    """Applies a new scan interval without a restart."""
    if _scheduler is None:
        return
    _scheduler.reschedule_job(JOB_ID, trigger="interval", seconds=seconds)
    logger.info("Library: scan interval updated", seconds=seconds)


@asynccontextmanager
async def lifespan(app: FastAPI):
    _ = app
    global _scheduler

    with next(get_session()) as session:
        interval = library_config.get_scan_interval(session)

    _scheduler = AsyncIOScheduler()
    _ = _scheduler.add_job(
        scan_downloads,
        "interval",
        seconds=interval,
        id=JOB_ID,
        max_instances=1,
        coalesce=True,
    )
    _scheduler.start()
    logger.debug("Library: watcher started", interval_seconds=interval)
    yield
    _scheduler.shutdown()
    _scheduler = None
