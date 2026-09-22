"""Watches the completed downloads folder and organizes finished grabs.

Earr hands a grab to Prowlarr and never hears back from the
download client, so a pending `LibraryImport` row is matched against the
directory names inside the completed downloads folder instead. Download clients
name that directory after the release, which is what was grabbed.
"""

import time
import uuid
from datetime import datetime, timedelta
from pathlib import Path

from rapidfuzz import fuzz, utils
from aiohttp import ClientSession
from sqlmodel import Session, col, desc, select

from app.internal.audiobookshelf.client import background_abs_trigger_scan
from app.internal.audiobookshelf.config import abs_config
from app.internal.download_clients.abstract import DownloadState
from app.internal.download_clients.config import download_client_config
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
    DownloadStatusEnum,
    EventEnum,
    LibraryImport,
    LibraryImportStatusEnum,
    ManualBookRequest,
)
from app.internal.notifications import (
    send_all_manual_notifications,
    send_all_notifications,
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
    release_title: str,
    candidates: list[Path],
    threshold: int,
    book_title: str | None = None,
) -> Path | None:
    """Finds the download folder that belongs to a grabbed release.

    Download clients do not always name the folder after the indexer's listing.
    A release listed as "Silverthorn by Raymond E Feist [ENG / M4B]" can arrive
    as "03 Silverthorn", which scores 40 against the release title but 88
    against the book title, so both are tried and the better score wins.

    Plain ratio is used rather than a partial or token set ratio on purpose:
    those score "Dune" against "Dune Messiah" as a perfect match, which would
    happily grab the wrong book in a series.
    """
    wanted = [utils.default_process(t) for t in (release_title, book_title) if t]
    wanted = [w for w in wanted if w]
    if not wanted:
        return None

    best: Path | None = None
    best_score = float(threshold)
    for candidate in candidates:
        # torrents keep their extension off the folder, usenet often does not
        name = utils.default_process(
            candidate.stem if candidate.is_file() else candidate.name
        )
        if name in wanted:
            return candidate
        score = max(fuzz.ratio(w, name) for w in wanted)
        if score >= best_score:
            best = candidate
            best_score = score
    return best


def set_download_status(
    session: Session,
    book: Audiobook | ManualBookRequest,
    status: DownloadStatusEnum,
) -> None:
    """Records what really happened to a grab, keeping `downloaded` in step.

    `downloaded` is the flag that stops a book being recommended or auto-grabbed
    again. A failure clears it, which puts the book back on the wishlist instead
    of leaving it there looking done. Confirmed files set it, so a book that was
    organized without going through a grab, such as via the import endpoint,
    does not sit in the library still marked as outstanding.
    """
    book.download_status = status
    if status == DownloadStatusEnum.failed:
        book.downloaded = False
    elif status == DownloadStatusEnum.downloaded:
        book.downloaded = True
    session.add(book)
    session.commit()


def _forget_gone(candidates: list[Path]) -> None:
    """Drops remembered sizes of downloads that are no longer in the folder."""
    known = {str(path) for path in candidates}
    for key in list(_seen_sizes):
        if key not in known:
            del _seen_sizes[key]


async def locate_via_client(
    session: Session,
    client_session: ClientSession,
    entry: LibraryImport,
) -> tuple[Path | None, bool]:
    """Asks the download client where a grab ended up.

    Returns the path and whether the client had an opinion at all. A client
    that has never heard of the job leaves the caller free to fall back to
    matching folder names, but one that reports it as still downloading or
    failed is authoritative and stops the fallback guessing wrong.
    """
    clients = download_client_config.clients_for(session, entry.protocol)
    for client in clients:
        info = await client.find(
            client_session, client_id=entry.client_id, name=entry.release_title
        )
        if info is None:
            continue

        if info.state == DownloadState.failed:
            logger.info(
                "Library: download client reports a failed download",
                client=client.name,
                release_title=entry.release_title,
                error=info.error,
            )
            return None, True

        if info.state == DownloadState.downloading:
            logger.debug(
                "Library: still downloading",
                client=client.name,
                release_title=entry.release_title,
                progress=round(info.progress, 3),
            )
            return None, True

        if not info.path:
            logger.warning(
                "Library: client reported a finished download with no path",
                client=client.name,
                release_title=entry.release_title,
            )
            return None, True

        path = Path(info.path)
        if not path.exists():
            # the client's view of the filesystem can differ from ours when
            # the paths are mounted differently in each container
            logger.warning(
                "Library: the path the client reported does not exist here",
                client=client.name,
                path=info.path,
            )
            return None, False

        logger.debug(
            "Library: located download via client",
            client=client.name,
            path=info.path,
        )
        return path, True

    return None, False


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
        book = get_book(session, entry.asin_or_uuid)
        if book is not None:
            set_download_status(session, book, DownloadStatusEnum.failed)
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
        set_download_status(session, book, DownloadStatusEnum.failed)
        return

    if library_config.get_write_metadata(session):
        _ = write_metadata(target_dir, book, library_config.get_overwrite(session))

    entry.status = LibraryImportStatusEnum.imported
    entry.target_path = str(target_dir)
    entry.error = None
    session.add(entry)
    session.commit()
    set_download_status(session, book, DownloadStatusEnum.downloaded)


async def scan(session: Session, client_session: ClientSession | None = None) -> int:
    """Runs one pass, resolving each pending grab to a folder and organizing it.

    A configured download client is asked first, since it knows exactly where it
    put a job. Matching folder names is the fallback for when no client is
    configured, or when it has no record of the grab.

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

    candidates: list[Path] = []
    for download_dir in library_config.get_download_dirs(session):
        if download_dir.is_dir():
            candidates.extend(sorted(download_dir.iterdir()))
        else:
            logger.warning(
                "Library: completed downloads folder does not exist",
                path=str(download_dir),
            )
    _forget_gone(candidates)

    use_clients = client_session is not None and download_client_config.any_enabled(
        session
    )
    threshold = library_config.get_match_threshold(session)
    imported = 0

    for entry in pending:
        match: Path | None = None
        if use_clients and client_session is not None:
            match, decided = await locate_via_client(session, client_session, entry)
            if match is None and decided:
                # the client knows about this job and it is not ready, so
                # guessing at folder names would only find the wrong thing
                continue
        if match is None:
            match = find_match(
                entry.release_title, candidates, threshold, entry.book_title
            )
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
            if match in candidates:
                candidates.remove(match)

    return imported


async def notify_imported(session: Session, entries: list[LibraryImport]) -> None:
    """Fires onSuccessfulDownload now that the files are actually on disk."""
    for entry in entries:
        book = get_book(session, entry.asin_or_uuid)
        replacements = {"targetPath": entry.target_path or ""}
        try:
            if isinstance(book, ManualBookRequest):
                await send_all_manual_notifications(
                    EventEnum.on_successful_download, book, replacements
                )
            else:
                await send_all_notifications(
                    EventEnum.on_successful_download,
                    entry.asin_or_uuid,
                    replacements,
                )
        except Exception as e:
            logger.error(
                "Library: failed to send download notification",
                asin_or_uuid=entry.asin_or_uuid,
                error=str(e),
            )


async def scan_downloads() -> None:
    """Scheduled entry point. Never raises so the scheduler keeps running."""
    try:
        with next(get_session()) as session:
            async with ClientSession() as client_session:
                imported = await scan(session, client_session)
            if not imported:
                return
            logger.info("Library: organized downloads", count=imported)
            trigger_abs = abs_config.is_valid(session)
            just_imported = list(
                session.exec(
                    select(LibraryImport)
                    .where(LibraryImport.status == LibraryImportStatusEnum.imported)
                    .order_by(desc(col(LibraryImport.updated_at)))
                    .limit(imported)
                ).all()
            )
            await notify_imported(session, just_imported)
    except Exception as e:
        logger.error("Library: scan failed", error=str(e))
        return

    if trigger_abs:
        # let Audiobookshelf pick up the new files right away
        await background_abs_trigger_scan()
