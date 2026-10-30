"""Releases that must not be grabbed again.

Without this, a release that failed or turned out to be junk is the very one
the next search ranks highest, because nothing about it changed. Blocklisting
it lets the re-search move on to the next best release.

A release matches by Prowlarr's id for it, or by its exact name, since the same
upload often turns up on several indexers.
"""

import uuid

from rapidfuzz import utils
from sqlmodel import Session, col, desc, select

from app.internal.models import BaseSource, BlocklistedRelease, LibraryImport
from app.util.log import logger


def _key(title: str) -> str:
    return utils.default_process(title)


class Blocklist:
    """A snapshot of the blocklist, taken once per ranking."""

    def __init__(self, entries: list[BlocklistedRelease]):
        self.guids: set[str] = {e.guid for e in entries if e.guid}
        self.titles: set[str] = {_key(e.title) for e in entries}

    def blocks(self, source: BaseSource) -> bool:
        return source.guid in self.guids or _key(source.title) in self.titles


def load(session: Session) -> Blocklist:
    return Blocklist(list(session.exec(select(BlocklistedRelease)).all()))


def entries(session: Session) -> list[BlocklistedRelease]:
    return list(
        session.exec(
            select(BlocklistedRelease).order_by(
                desc(col(BlocklistedRelease.created_at))
            )
        ).all()
    )


def add(
    session: Session,
    *,
    title: str,
    guid: str | None = None,
    indexer: str | None = None,
    asin_or_uuid: str | None = None,
    book_title: str | None = None,
    reason: str | None = None,
) -> BlocklistedRelease:
    """Adds a release, or returns the entry that already covers it."""
    for entry in entries(session):
        if (guid and entry.guid == guid) or _key(entry.title) == _key(title):
            return entry
    entry = BlocklistedRelease(
        title=title,
        guid=guid,
        indexer=indexer,
        asin_or_uuid=asin_or_uuid,
        book_title=book_title,
        reason=reason,
    )
    session.add(entry)
    session.commit()
    logger.info("Blocklisted release", title=title, reason=reason)
    return entry


def add_import(
    session: Session, entry: LibraryImport, reason: str | None
) -> BlocklistedRelease:
    return add(
        session,
        title=entry.release_title,
        guid=entry.guid,
        indexer=entry.indexer,
        asin_or_uuid=entry.asin_or_uuid,
        book_title=entry.book_title,
        reason=reason,
    )


def remove(session: Session, entry_id: uuid.UUID) -> bool:
    entry = session.get(BlocklistedRelease, entry_id)
    if entry is None:
        return False
    session.delete(entry)
    session.commit()
    return True
