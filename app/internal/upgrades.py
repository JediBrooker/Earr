"""Replacing a downloaded book with a better release, up to a format you set.

The first release that clears the quality bar is often an MP3, because it is
what exists on the day. A better one, an M4B with chapters say, can turn up
weeks later. With upgrades on, Earr looks again for books downloaded within
the last few weeks whose format is below the cutoff, and grabs a release in a
better format when one clears the same bar. When it is in, the old audio
files are removed from the book's folder.
"""

from datetime import datetime, timedelta

import aiohttp
from aiohttp import ClientSession
from pydantic import BaseModel
from sqlmodel import Session, col, desc, select

from app.internal.models import (
    Audiobook,
    DownloadStatusEnum,
    LibraryImport,
    LibraryImportStatusEnum,
    ProwlarrSource,
)
from app.internal.prowlarr.prowlarr import start_download
from app.internal.prowlarr.util import prowlarr_config
from app.internal.query import query_sources
from app.internal.ranking.quality import FILE_FORMATS, FileFormat, quality_config
from app.internal.ranking.quality_extract import format_from_title
from app.util.db import get_session
from app.util.log import logger

BATCH_SIZE = 3
"""Books looked at per run, to go easy on the indexers."""
RETRY_AFTER = timedelta(hours=24)
INTERVAL_SECONDS = 6 * 60 * 60


class Upgradable(BaseModel):
    book: Audiobook
    current: FileFormat


def _rank(session: Session, file_format: FileFormat) -> int:
    return quality_config.calculate_quality_rank(session, file_format)


def current_format(session: Session, book: Audiobook) -> FileFormat:
    """What the book was last downloaded as, from its grab or its release name."""
    if book.quality_format in FILE_FORMATS:
        return book.quality_format
    latest = session.exec(
        select(LibraryImport)
        .where(
            LibraryImport.asin_or_uuid == book.asin,
            LibraryImport.status == LibraryImportStatusEnum.imported,
        )
        .order_by(desc(col(LibraryImport.updated_at)))
    ).first()
    return format_from_title(latest.release_title) if latest else "unknown"


def find_upgradable(session: Session) -> list[Upgradable]:
    cutoff = _rank(session, quality_config.get_upgrade_cutoff(session))
    since = datetime.now() - timedelta(days=quality_config.get_upgrade_days(session))
    pending = select(LibraryImport.asin_or_uuid).where(
        LibraryImport.status == LibraryImportStatusEnum.pending
    )
    recent = select(LibraryImport.asin_or_uuid).where(
        LibraryImport.status == LibraryImportStatusEnum.imported,
        col(LibraryImport.updated_at) >= since,
    )
    books = session.exec(
        select(Audiobook).where(
            col(Audiobook.download_status) == DownloadStatusEnum.downloaded,
            col(Audiobook.asin).in_(recent),
            col(Audiobook.asin).not_in(pending),
        )
    ).all()
    found: list[Upgradable] = []
    for book in books:
        if (
            book.last_searched_at
            and datetime.now() - book.last_searched_at < RETRY_AFTER
        ):
            continue
        current = current_format(session, book)
        if _rank(session, current) > cutoff:
            found.append(Upgradable(book=book, current=current))
    return found[:BATCH_SIZE]


def better_source(
    session: Session,
    sources: list[ProwlarrSource],
    valid: list[bool],
    current: FileFormat,
) -> ProwlarrSource | None:
    """The best release that clears the bar and is in a better format."""
    current_rank = _rank(session, current)
    for source, ok in zip(sources, valid, strict=True):
        if ok and _rank(session, format_from_title(source.title)) < current_rank:
            return source
    return None


async def try_upgrade(
    session: Session, client_session: ClientSession, candidate: Upgradable
) -> bool:
    book = candidate.book
    book.last_searched_at = datetime.now()
    session.add(book)
    session.commit()
    try:
        result = await query_sources(
            asin_or_uuid=book.asin,
            session=session,
            client_session=client_session,
            force_refresh=True,
        )
    except Exception as e:
        logger.warning("Upgrade search failed", asin=book.asin, error=str(e))
        return False
    sources = result.sources or []
    verdicts = result.verdicts or []
    if len(verdicts) != len(sources):
        return False
    best = better_source(
        session, sources, [v.meets_bar for v in verdicts], candidate.current
    )
    if best is None:
        return False
    response = await start_download(
        session=session,
        client_session=client_session,
        guid=best.guid,
        indexer_id=best.indexer_id,
        asin_or_uuid=book.asin,
        prowlarr_source=best,
    )
    if response.ok:
        logger.info(
            "Grabbed an upgrade",
            title=book.title,
            was=candidate.current,
            now=format_from_title(best.title),
        )
    return response.ok


async def run_upgrades(session: Session, client_session: ClientSession) -> int:
    if not quality_config.get_upgrade_enabled(session):
        return 0
    if not prowlarr_config.is_valid(session):
        return 0
    started = 0
    for candidate in find_upgradable(session):
        if await try_upgrade(session, client_session, candidate):
            started += 1
    return started


async def look_for_upgrades() -> None:
    """Scheduled entry point. Never raises so the scheduler keeps running."""
    try:
        with next(get_session()) as session:
            async with ClientSession(
                timeout=aiohttp.ClientTimeout(total=120)
            ) as client_session:
                started = await run_upgrades(session, client_session)
        if started:
            logger.info("Upgrades started", count=started)
    except Exception as e:
        logger.error("Looking for upgrades failed", error=str(e))
