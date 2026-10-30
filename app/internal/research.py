"""Periodically searches again for requests that have not produced files yet.

A book can be requested before any source exists for it, and a grab can stall
or turn out to be junk. Neither case recovers on its own: the request just sits
on the wishlist. This retries them on a schedule.

Deliberately conservative, because it grabs without anyone watching:

- it only runs when automatic downloading is enabled, and reuses the same
  ranking and validity rules, so it cannot grab anything the + button would not
- only requests from trusted users are retried, matching who is allowed to
  trigger an automatic download in the first place
- every attempt is counted, so a book nothing exists for stops being retried
  rather than searching forever
- searches are forced past the source cache, since a cached result is exactly
  what made the previous attempt fail
- books that are not out yet are left alone until their release day, when
  they are searched for whether or not re-searching is turned on
"""

from datetime import datetime, timedelta

import aiohttp
from aiohttp import ClientSession
from sqlmodel import Session, col, or_, select

from app.internal.db_queries import get_book
from app.internal.models import (
    Audiobook,
    AudiobookRequest,
    GroupEnum,
    ManualBookRequest,
    User,
)
from app.internal.query import query_sources
from app.internal.ranking.quality import quality_config
from app.util.db import get_session
from app.util.log import logger

BATCH_SIZE = 5
"""How many books to search per run, to avoid hammering Prowlarr."""
RELEASE_WINDOW_DAYS = 7
"""How long after release day a pre-order still gets its release-day search."""


def _due_before(interval_seconds: int) -> datetime:
    return datetime.now() - timedelta(seconds=interval_seconds)


def find_audiobooks_to_retry(
    session: Session, interval_seconds: int, max_attempts: int
) -> list[Audiobook]:
    """Requested books that are still outstanding and due another look."""
    cutoff = _due_before(interval_seconds)
    trusted_usernames = select(User.username).where(
        col(User.group).in_([GroupEnum.trusted, GroupEnum.admin])
    )
    return list(
        session.exec(
            select(Audiobook)
            .where(
                col(Audiobook.downloaded).is_(False),
                col(Audiobook.search_attempts) < max_attempts,
                # nothing to find before it is out
                col(Audiobook.release_date) <= datetime.now(),
                or_(
                    col(Audiobook.last_searched_at).is_(None),
                    col(Audiobook.last_searched_at) < cutoff,
                ),
                col(Audiobook.asin).in_(
                    select(AudiobookRequest.asin).where(
                        or_(
                            col(AudiobookRequest.user_username).in_(trusted_usernames),
                            # an admin approved it, which is as good as trusted
                            col(AudiobookRequest.approved),
                        )
                    )
                ),
            )
            .order_by(col(Audiobook.search_attempts))
            .limit(BATCH_SIZE)
        ).all()
    )


def find_manual_requests_to_retry(
    session: Session, interval_seconds: int, max_attempts: int
) -> list[ManualBookRequest]:
    cutoff = _due_before(interval_seconds)
    trusted_usernames = select(User.username).where(
        col(User.group).in_([GroupEnum.trusted, GroupEnum.admin])
    )
    return list(
        session.exec(
            select(ManualBookRequest)
            .where(
                col(ManualBookRequest.downloaded).is_(False),
                col(ManualBookRequest.search_attempts) < max_attempts,
                or_(
                    col(ManualBookRequest.last_searched_at).is_(None),
                    col(ManualBookRequest.last_searched_at) < cutoff,
                ),
                or_(
                    col(ManualBookRequest.user_username).in_(trusted_usernames),
                    col(ManualBookRequest.approved),
                ),
            )
            .order_by(col(ManualBookRequest.search_attempts))
            .limit(BATCH_SIZE)
        ).all()
    )


def _trusted_or_approved():
    trusted_usernames = select(User.username).where(
        col(User.group).in_([GroupEnum.trusted, GroupEnum.admin])
    )
    return select(AudiobookRequest.asin).where(
        or_(
            col(AudiobookRequest.user_username).in_(trusted_usernames),
            col(AudiobookRequest.approved),
        )
    )


def find_newly_released(session: Session) -> list[Audiobook]:
    """Pre-orders whose release day has come and that haven't been searched since."""
    now = datetime.now()
    books = session.exec(
        select(Audiobook).where(
            col(Audiobook.downloaded).is_(False),
            col(Audiobook.download_status).is_(None),
            col(Audiobook.release_date) <= now,
            col(Audiobook.release_date) >= now - timedelta(days=RELEASE_WINDOW_DAYS),
            col(Audiobook.asin).in_(_trusted_or_approved()),
        )
    ).all()
    return [
        b
        for b in books
        if b.last_searched_at is None or b.last_searched_at < b.release_date
    ][:BATCH_SIZE]


def may_download(book: Audiobook | ManualBookRequest) -> bool:
    """Whether someone allowed to trigger downloads stands behind this request."""
    if isinstance(book, ManualBookRequest):
        return book.approved or bool(
            book.user and book.user.is_above(GroupEnum.trusted)
        )
    return any(
        request.approved or request.user.is_above(GroupEnum.trusted)
        for request in book.requests
    )


async def search_again(
    session: Session, client_session: ClientSession, identifiers: list[str]
) -> int:
    """Searches straight away for books whose download just failed.

    The failed release is blocklisted by then, so this picks the next best one.
    Returns how many downloads were started.
    """
    if not quality_config.get_auto_download(session):
        return 0
    max_attempts = quality_config.get_research_max_attempts(session)
    started = 0
    for identifier in identifiers:
        book = get_book(session, identifier)
        if book is None or book.downloaded or book.search_attempts >= max_attempts:
            continue
        if isinstance(book, Audiobook) and not book.is_released:
            continue
        if not may_download(book):
            continue
        if await retry_one(session, client_session, book, identifier):
            started += 1
    return started


async def run_release_day(session: Session, client_session: ClientSession) -> int:
    """Searches for pre-orders on the day they come out."""
    if not quality_config.get_auto_download(session):
        return 0
    started = 0
    for book in find_newly_released(session):
        if await retry_one(session, client_session, book, book.asin):
            started += 1
    return started


async def search_new_releases() -> None:
    """Scheduled entry point. Never raises so the scheduler keeps running."""
    try:
        with next(get_session()) as session:
            async with ClientSession(
                timeout=aiohttp.ClientTimeout(total=120)
            ) as client_session:
                started = await run_release_day(session, client_session)
        if started:
            logger.info("Release day searches started downloads", count=started)
    except Exception as e:
        logger.error("Release day search failed", error=str(e))


def record_attempt(session: Session, book: Audiobook | ManualBookRequest) -> None:
    book.search_attempts += 1
    book.last_searched_at = datetime.now()
    session.add(book)
    session.commit()


async def retry_one(
    session: Session,
    client_session: ClientSession,
    book: Audiobook | ManualBookRequest,
    identifier: str,
) -> bool:
    """Searches again for one book. Returns whether it started a download."""
    # counted before the attempt, so a search that throws still burns one and
    # a persistently failing book cannot be retried forever
    record_attempt(session, book)
    try:
        result = await query_sources(
            asin_or_uuid=identifier,
            session=session,
            client_session=client_session,
            start_auto_download=True,
            force_refresh=True,
        )
    except Exception as e:
        logger.warning(
            "Re-search failed", identifier=identifier, title=book.title, error=str(e)
        )
        return False

    session.refresh(book)
    if book.downloaded:
        logger.info("Re-search found a source", identifier=identifier, title=book.title)
        return True

    logger.debug(
        "Re-search found nothing usable",
        identifier=identifier,
        title=book.title,
        attempt=book.search_attempts,
        reason=result.error_message,
    )
    return False


async def run_research(session: Session, client_session: ClientSession) -> int:
    """One pass. Returns how many downloads were started."""
    if not quality_config.get_auto_download(session):
        return 0
    if not quality_config.get_research_enabled(session):
        return 0

    interval = quality_config.get_research_interval(session)
    max_attempts = quality_config.get_research_max_attempts(session)

    started = 0
    for book in find_audiobooks_to_retry(session, interval, max_attempts):
        if await retry_one(session, client_session, book, book.asin):
            started += 1
    for request in find_manual_requests_to_retry(session, interval, max_attempts):
        if await retry_one(session, client_session, request, str(request.id)):
            started += 1
    return started


async def research_outstanding_requests() -> None:
    """Scheduled entry point. Never raises so the scheduler keeps running."""
    try:
        with next(get_session()) as session:
            async with ClientSession(
                timeout=aiohttp.ClientTimeout(total=120)
            ) as client_session:
                started = await run_research(session, client_session)
        if started:
            logger.info("Re-search started downloads", count=started)
    except Exception as e:
        logger.error("Re-search pass failed", error=str(e))
