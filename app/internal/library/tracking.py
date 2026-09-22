"""Keeps track of grabs that still have to be placed into the library."""

from sqlmodel import Session, select

from app.internal.library.config import library_config
from app.internal.models import (
    Audiobook,
    LibraryImport,
    LibraryImportStatusEnum,
    ManualBookRequest,
)
from app.util.log import logger


def record_grab(
    session: Session,
    book: Audiobook | ManualBookRequest,
    asin_or_uuid: str,
    release_title: str | None,
) -> LibraryImport | None:
    """Queues a started download for the library watcher to pick up.

    Does nothing while the feature is disabled, so no rows pile up for users
    who never turn it on.
    """
    if not library_config.get_enabled(session):
        return None

    existing = session.exec(
        select(LibraryImport).where(
            LibraryImport.asin_or_uuid == asin_or_uuid,
            LibraryImport.status == LibraryImportStatusEnum.pending,
        )
    ).first()
    if existing:
        # a re-grab replaces what the watcher is looking for
        existing.release_title = release_title or book.title
        existing.book_title = book.title
        session.add(existing)
        session.commit()
        return existing

    entry = LibraryImport(
        asin_or_uuid=asin_or_uuid,
        book_title=book.title,
        release_title=release_title or book.title,
    )
    session.add(entry)
    session.commit()
    logger.debug(
        "Library: queued download for organizing",
        asin_or_uuid=asin_or_uuid,
        release_title=entry.release_title,
    )
    return entry
