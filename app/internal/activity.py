"""What is downloading right now, and what happened to recent downloads.

Earr hears about a download when it hands a release to Prowlarr. With library
organizing on, it keeps a row per grab (LibraryImport) holding the torrent's
info hash or the release name, which is enough to ask the download client how
far along it is. Without organizing no such row exists, so there is nothing to
follow.
"""

import asyncio
import uuid
from datetime import datetime
from typing import Literal

from aiohttp import ClientSession
from pydantic import BaseModel
from sqlmodel import Session, col, desc, select

from app.internal.download_clients.abstract import (
    DownloadClient,
    DownloadInfo,
    DownloadState,
)
from app.internal.download_clients.config import download_client_config
from app.internal.library.watcher import get_book
from app.internal.models import (
    Audiobook,
    AudiobookRequest,
    LibraryImport,
    LibraryImportStatusEnum,
    ManualBookRequest,
    User,
)

LOOKUP_TIMEOUT = 8
"""Seconds to wait for a download client before showing the row without progress."""


def _size(value: int | None) -> str:
    if not value:
        return ""
    size = float(value)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024:
            return f"{size:.0f} {unit}" if unit in ("B", "KB") else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} TB"


def _duration(seconds: int | None) -> str:
    if not seconds:
        return ""
    hours, rest = divmod(seconds, 3600)
    minutes = rest // 60
    if hours >= 24:
        return f"{hours // 24}d {hours % 24}h"
    if hours:
        return f"{hours}h {minutes}m"
    return f"{minutes}m" if minutes else "under a minute"


class QueueItem(BaseModel):
    id: uuid.UUID
    title: str
    authors: list[str]
    cover_image: str | None
    release_title: str
    client: str | None
    """Name of the download client that reported on it, if any did."""
    state: Literal["downloading", "importing", "waiting", "failed"]
    message: str
    info: DownloadInfo | None
    requested_by: list[str]
    added_at: datetime
    replaces: bool = False
    """An upgrade or replacement of a book already in the library."""

    @property
    def percent(self) -> int:
        return round((self.info.progress if self.info else 0) * 100)

    @property
    def size_label(self) -> str:
        return _size(self.info.size if self.info else None)

    @property
    def speed_label(self) -> str:
        speed = self.info.speed if self.info else None
        return f"{_size(speed)}/s" if speed else ""

    @property
    def eta_label(self) -> str:
        return _duration(self.info.eta if self.info else None)


class HistoryItem(BaseModel):
    title: str
    release_title: str
    status: LibraryImportStatusEnum
    target_path: str | None
    error: str | None
    when: datetime


def _book_details(
    book: Audiobook | ManualBookRequest | None, fallback_title: str
) -> tuple[str, list[str], str | None, list[str]]:
    if isinstance(book, Audiobook):
        return (
            book.title,
            book.authors,
            book.cover_image,
            [r.user_username for r in book.requests],
        )
    if isinstance(book, ManualBookRequest):
        return book.title, book.authors, None, [book.user_username]
    return fallback_title, [], None, []


def _visible(
    session: Session, user: User, rows: list[LibraryImport]
) -> list[LibraryImport]:
    """Admins see every download; everyone else sees the books they asked for."""
    if user.is_admin():
        return rows
    mine = set(
        session.exec(
            select(AudiobookRequest.asin).where(
                AudiobookRequest.user_username == user.username
            )
        ).all()
    )
    mine.update(
        str(request_id)
        for request_id in session.exec(
            select(ManualBookRequest.id).where(
                ManualBookRequest.user_username == user.username
            )
        ).all()
    )
    return [row for row in rows if row.asin_or_uuid in mine]


def _imports(
    session: Session, user: User, *, pending: bool, limit: int | None = None
) -> list[LibraryImport]:
    status = col(LibraryImport.status)
    query = (
        select(LibraryImport)
        .where(
            status == LibraryImportStatusEnum.pending
            if pending
            else status != LibraryImportStatusEnum.pending
        )
        .order_by(
            desc(col(LibraryImport.created_at if pending else LibraryImport.updated_at))
        )
    )
    rows = _visible(session, user, list(session.exec(query).all()))
    return rows[:limit] if limit else rows


def pending_count(session: Session, user: User) -> int:
    return len(_imports(session, user, pending=True))


async def _lookup(
    clients: list[DownloadClient], client_session: ClientSession, row: LibraryImport
) -> tuple[DownloadClient | None, DownloadInfo | None]:
    for client in clients:
        try:
            info = await asyncio.wait_for(
                client.find(
                    client_session, client_id=row.client_id, name=row.release_title
                ),
                LOOKUP_TIMEOUT,
            )
        except TimeoutError:
            continue
        if info is not None:
            return client, info
    return None, None


async def queue(
    session: Session, client_session: ClientSession, user: User
) -> list[QueueItem]:
    """Grabs that haven't reached the library yet, with live progress where possible."""
    torrent = download_client_config.clients_for(session, "torrent")
    usenet = download_client_config.clients_for(session, "usenet")

    def clients_for(protocol: str | None) -> list[DownloadClient]:
        if protocol == "torrent":
            return torrent
        if protocol == "usenet":
            return usenet
        return [*torrent, *usenet]

    async def build(row: LibraryImport) -> QueueItem:
        clients = clients_for(row.protocol)
        client, info = await _lookup(clients, client_session, row)
        title, authors, cover, requested_by = _book_details(
            get_book(session, row.asin_or_uuid), row.book_title
        )

        state: Literal["downloading", "importing", "waiting", "failed"]
        if info is None:
            state = "waiting"
            message = (
                f"Not in {' or '.join(c.name for c in clients)} yet."
                if clients
                else "Waiting for the files to appear in the completed downloads folder."
            )
        elif info.state == DownloadState.failed:
            state = "failed"
            message = info.error or "The download client reports an error."
        elif info.state == DownloadState.completed:
            state = "importing"
            message = "Downloaded. It goes into the library on the next scan."
        else:
            state = "downloading"
            message = info.status or "downloading"

        return QueueItem(
            id=row.id,
            title=title,
            authors=authors,
            cover_image=cover,
            release_title=row.release_title,
            client=client.name if client else None,
            state=state,
            message=message,
            info=info,
            requested_by=requested_by,
            added_at=row.created_at,
            replaces=row.replaces,
        )

    rows = _imports(session, user, pending=True)
    return list(await asyncio.gather(*(build(row) for row in rows)))


async def remove_download(
    session: Session,
    client_session: ClientSession,
    import_id: uuid.UUID,
    *,
    blocklist: bool,
) -> str:
    """Takes a grab out of the download client and stops waiting for it.

    With `blocklist`, the release is never grabbed again and the next best one
    is searched for straight away. Returns a message saying what happened.
    """
    from app.internal.library.watcher import fail_download
    from app.internal.research import search_again

    entry = session.get(LibraryImport, import_id)
    if entry is None or entry.status != LibraryImportStatusEnum.pending:
        return "That download is no longer in the queue"

    removed_from = None
    for client in download_client_config.clients_for(session, entry.protocol):
        if await client.remove(
            client_session, client_id=entry.client_id, name=entry.release_title
        ):
            removed_from = client.name
            break

    if blocklist:
        fail_download(session, entry, "Removed from the queue and blocklisted")
        started = await search_again(session, client_session, [entry.asin_or_uuid])
        followup = " Grabbed another release." if started else ""
    else:
        entry.status = LibraryImportStatusEnum.failed
        entry.error = "Removed from the queue"
        session.add(entry)
        book = get_book(session, entry.asin_or_uuid)
        if book is not None:
            book.downloaded = False
            book.download_status = None
            session.add(book)
        session.commit()
        followup = ""

    where = (
        f"from {removed_from}"
        if removed_from
        else "from the queue (no download client had it)"
    )
    verb = "Blocklisted and removed" if blocklist else "Removed"
    return f"{verb} {entry.release_title} {where}.{followup}"


def history(session: Session, user: User, limit: int = 50) -> list[HistoryItem]:
    items: list[HistoryItem] = []
    for row in _imports(session, user, pending=False, limit=limit):
        title, _, _, _ = _book_details(
            get_book(session, row.asin_or_uuid), row.book_title
        )
        items.append(
            HistoryItem(
                title=title,
                release_title=row.release_title,
                status=row.status,
                target_path=row.target_path,
                error=row.error,
                when=row.updated_at,
            )
        )
    return items
