"""Background jobs that keep requests moving without anyone clicking anything:
checking followed authors and series, searching for pre-orders on release
day, and taking scheduled backups."""

from contextlib import asynccontextmanager
from datetime import datetime, timedelta

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from fastapi import FastAPI

from app.internal.backups import scheduled_backup
from app.internal.follows import CHECK_INTERVAL, check_all_follows
from app.internal.research import search_new_releases
from app.util.log import logger

RELEASE_CHECK_INTERVAL = 60 * 60
BACKUP_INTERVAL = 24 * 60 * 60

_scheduler: AsyncIOScheduler | None = None


def run_follow_check_soon() -> None:
    """Moves the next follow check to now, e.g. after the language changed."""
    if _scheduler is None:
        return
    _scheduler.modify_job("follows", next_run_time=datetime.now())


@asynccontextmanager
async def lifespan(app: FastAPI):
    _ = app
    global _scheduler

    now = datetime.now()
    _scheduler = AsyncIOScheduler()
    _ = _scheduler.add_job(
        check_all_follows,
        "interval",
        seconds=CHECK_INTERVAL,
        id="follows",
        max_instances=1,
        coalesce=True,
        # not straight away, so a restart loop can't hammer Audible
        next_run_time=now + timedelta(minutes=5),
    )
    _ = _scheduler.add_job(
        search_new_releases,
        "interval",
        seconds=RELEASE_CHECK_INTERVAL,
        id="release_day",
        max_instances=1,
        coalesce=True,
        next_run_time=now + timedelta(minutes=2),
    )
    _ = _scheduler.add_job(
        scheduled_backup,
        "interval",
        seconds=BACKUP_INTERVAL,
        id="backup",
        max_instances=1,
        coalesce=True,
        next_run_time=now + timedelta(minutes=10),
    )
    _scheduler.start()
    logger.debug("Automation scheduler started")
    yield
    _scheduler.shutdown()
    _scheduler = None
