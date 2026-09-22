"""Watches the completed downloads folder and organizes finished grabs.

AudioBookRequest hands a grab to Prowlarr and never hears back from the
download client, so a pending `LibraryImport` row is matched against the
directory names inside the completed downloads folder instead. Download clients
name that directory after the release, which is what was grabbed.
"""

import time
import uuid
from datetime import datetime, timedelta
from pathlib import Path

from rapidfuzz import fuzz, utils
from sqlmodel import Session, col, select

from app.internal.audiobookshelf.client import background_abs_trigger_scan
from app.internal.audiobookshelf.config import abs_config
from app.internal.library.config import DEFAULT_PENDING_TTL_DAYS, library_config
from app.internal.library.metadata import write_metadata
from app.internal.library.organizer import (
    OrganizeError,
    has_incomplete_files,
    organize,
    resolve_target_dir,
)
from app.internal.models import (
    Audiobook,
    LibraryImport,
    LibraryImportStatusEnum,
    ManualBookRequest,
)
from app.util.db import get_session
from app.util.log import logger

STABLE_AFTER_SECONDS = 60
"""How long a download has to stop changing before it is considered finished."""

_seen_sizes: dict[str, tuple[int, float]] = {}
"""Path to (total size, first time that size was observed)."""


def get_book(
    session: Session, asin_or_uuid: str
) -> Audiobook | ManualBookRequest | None:
    try:
        return session.get(ManualBookRequest, uuid.UUID(asin_or_uuid))
    except ValueError:
        return session.get(Audiobook, asin_or_uuid)


def _total_size(path: Path) -> int:
    if path.is_file():
        return path.stat().st_size
    return sum(f.stat().st_size for f in path.rglob("*") if f.is_file())


def is_stable(path: Path) -> bool:
    """True once a download stopped growing and has no partial files left."""
    if has_incomplete_files(path):
        _seen_sizes.pop(str(path), None)
        return False

    try:
        size = _total_size(path)
    except OSError:
        return False

    key = str(path)
    previous = _seen_sizes.get(key)
    if previous is None or previous[0] != size:
        _seen_sizes[key] = (size, time.time())
        return False
    return time.time() - previous[1] >= STABLE_AFTER_SECONDS


def find_match(
    release_title: str, candidates: list[Path], threshold: int
) -> Path | None:
    """Finds the download folder that belongs to a grabbed release."""
    wanted = utils.default_process(release_title)
    if not wanted:
        return None

    best: Path | None = None
    best_score = float(threshold)
    for candidate in candidates:
        # torrents keep their extension off the folder, usenet often does not
        name = utils.default_process(
            candidate.stem if candidate.is_file() else candidate.name
        )
        if name == wanted:
            return candidate
        score = fuzz.ratio(wanted, name)
        if score >= best_score:
            best = candidate
            best_score = score
    return best


def _forget_gone(candidates: list[Path]) -> None:
    """Drops remembered sizes of downloads that are no longer in the folder."""
    known = {str(path) for path in candidates}
    for key in list(_seen_sizes):
        if key not in known:
            del _seen_sizes[key]


def expire_stale_imports(session: Session) -> int:
    """Stops looking for downloads that never showed up."""
    cutoff = datetime.now() - timedelta(days=DEFAULT_PENDING_TTL_DAYS)
    stale = session.exec(
        select(LibraryImport).where(
            LibraryImport.status == LibraryImportStatusEnum.pending,
            col(LibraryImport.created_at) < cutoff,
        )
    ).all()
    for entry in stale:
        entry.status = LibraryImportStatusEnum.expired
        entry.error = (
            f"No matching download found within {DEFAULT_PENDING_TTL_DAYS} days"
        )
        session.add(entry)
    if stale:
        session.commit()
    return len(stale)


def import_single(session: Session, entry: LibraryImport, source: Path) -> None:
    """Organizes one download and records the outcome on the import row."""
    book = get_book(session, entry.asin_or_uuid)
    if book is None:
        entry.status = LibraryImportStatusEnum.failed
        entry.error = "The request this download belongs to no longer exists"
        session.add(entry)
        session.commit()
        return

    entry.source_path = str(source)
    try:
        target_dir = resolve_target_dir(session, book)
        target_dir = organize(
            source=source,
            target_dir=target_dir,
            mode=library_config.get_mode(session),
            overwrite=library_config.get_overwrite(session),
        )
    except (OrganizeError, OSError) as e:
        logger.error(
            "Library: failed to organize download",
            release_title=entry.release_title,
            source=str(source),
            error=str(e),
        )
        entry.status = LibraryImportStatusEnum.failed
        entry.error = str(e)
        session.add(entry)
        session.commit()
        return

    if library_config.get_write_metadata(session):
        _ = write_metadata(target_dir, book, library_config.get_overwrite(session))

    entry.status = LibraryImportStatusEnum.imported
    entry.target_path = str(target_dir)
    entry.error = None
    session.add(entry)
    session.commit()


def scan(session: Session) -> int:
    """Runs one pass over the completed downloads folder.

    Returns how many downloads were organized.
    """
    if not library_config.get_enabled(session):
        return 0

    _ = expire_stale_imports(session)

    pending = list(
        session.exec(
            select(LibraryImport).where(
                LibraryImport.status == LibraryImportStatusEnum.pending
            )
        ).all()
    )
    if not pending:
        return 0

    download_dir = library_config.get_download_dir(session)
    if download_dir is None:
        return 0
    if not download_dir.is_dir():
        logger.warning(
            "Library: completed downloads folder does not exist",
            path=str(download_dir),
        )
        return 0

    candidates = sorted(download_dir.iterdir())
    _forget_gone(candidates)
    if not candidates:
        return 0

    threshold = library_config.get_match_threshold(session)
    imported = 0
    for entry in pending:
        match = find_match(entry.release_title, candidates, threshold)
        if match is None:
            continue
        if not is_stable(match):
            logger.debug("Library: download still changing, waiting", source=str(match))
            continue
        import_single(session, entry, match)
        # the entry left "pending" either way, so its size no longer matters
        _ = _seen_sizes.pop(str(match), None)
        if entry.status == LibraryImportStatusEnum.imported:
            imported += 1
            candidates.remove(match)

    return imported


async def scan_downloads() -> None:
    """Scheduled entry point. Never raises so the scheduler keeps running."""
    try:
        with next(get_session()) as session:
            imported = scan(session)
            if not imported:
                return
            logger.info("Library: organized downloads", count=imported)
            trigger_abs = abs_config.is_valid(session)
    except Exception as e:
        logger.error("Library: scan failed", error=str(e))
        return

    if trigger_abs:
        # let Audiobookshelf pick up the new files right away
        await background_abs_trigger_scan()
