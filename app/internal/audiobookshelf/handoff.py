"""Getting a newly organized book into Audiobookshelf quickly.

A full library scan reads every folder, and Audiobookshelf's own folder watcher
only notices new files when it is enabled. Instead Earr tells the watcher about
the one new folder (POST /api/watcher/update, Audiobookshelf 2.5 or later),
waits for the item to appear, and fills in whatever metadata Audiobookshelf
left empty. When the item doesn't show up, because the watcher is disabled or
the folder has a different path from Audiobookshelf's side, it falls back to a
full scan.

The watcher endpoint needs a media file rather than a folder: Audiobookshelf
groups changed files into items by their extension, and a bare directory is
dropped. Any one file is enough, the whole folder is scanned as a new item.
"""

import asyncio
import posixpath
from collections.abc import Awaitable, Callable
from pathlib import Path

from aiohttp import ClientSession
from pydantic import BaseModel
from sqlmodel import Session

from app.internal.audiobookshelf.client import abs_trigger_scan
from app.internal.audiobookshelf.config import abs_config
from app.internal.db_queries import get_book
from app.internal.models import Audiobook, ManualBookRequest
from app.internal.ranking.quality_extract import audio_file_formats
from app.util.connection import USER_AGENT
from app.util.db import get_session
from app.util.log import logger

APPEAR_TIMEOUT = 60
"""Seconds to wait for the new item. Audiobookshelf waits for a file to stop
changing and then another ten seconds before it scans."""
POLL_INTERVAL = 5

Sleep = Callable[[float], Awaitable[None]]


class _Metadata(BaseModel):
    title: str | None = None
    subtitle: str | None = None
    authorName: str | None = None
    narratorName: str | None = None
    seriesName: str | None = None
    publishedYear: str | None = None
    asin: str | None = None


class _Media(BaseModel):
    metadata: _Metadata = _Metadata()


class _Item(BaseModel):
    id: str
    path: str | None = None
    media: _Media = _Media()


class _Page(BaseModel):
    results: list[_Item] = []


def _headers(session: Session) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {abs_config.get_api_token(session) or ''}",
        "User-Agent": USER_AGENT,
    }


def _url(session: Session, path: str) -> str:
    return posixpath.join(abs_config.get_base_url(session) or "", path)


def first_audio_file(folder: Path) -> Path | None:
    suffixes = set(audio_file_formats)
    if folder.is_file():
        return folder if folder.suffix.lower() in suffixes else None
    for path in sorted(folder.rglob("*")):
        if path.is_file() and path.suffix.lower() in suffixes:
            return path
    return None


async def _notify_watcher(
    session: Session, client_session: ClientSession, file: Path
) -> bool:
    payload = {
        "libraryId": abs_config.get_library_id(session),
        "path": str(file),
        "type": "add",
    }
    try:
        async with client_session.post(
            _url(session, "api/watcher/update"),
            json=payload,
            headers=_headers(session),
        ) as response:
            if not response.ok:
                # 404 on Audiobookshelf before 2.5
                logger.info(
                    "ABS: watcher update not accepted",
                    status=response.status,
                    path=str(file),
                )
            return response.ok
    except Exception as e:
        logger.warning("ABS: watcher update failed", error=str(e))
        return False


async def _recent_items(
    session: Session, client_session: ClientSession, limit: int = 50
) -> list[_Item]:
    library_id = abs_config.get_library_id(session)
    params = {"limit": str(limit), "page": "0", "sort": "addedAt", "desc": "1"}
    try:
        async with client_session.get(
            _url(session, f"api/libraries/{library_id}/items"),
            params=params,
            headers=_headers(session),
        ) as response:
            if not response.ok:
                return []
            return _Page.model_validate(await response.json()).results
    except Exception as e:
        logger.warning("ABS: could not list recent items", error=str(e))
        return []


def missing_metadata(
    item_metadata: _Metadata, book: Audiobook | ManualBookRequest
) -> dict[str, object]:
    """Only the fields Audiobookshelf left empty, so nothing set by hand is replaced."""
    patch: dict[str, object] = {}
    if not item_metadata.subtitle and book.subtitle:
        patch["subtitle"] = book.subtitle
    if not item_metadata.narratorName and book.narrators:
        patch["narrators"] = list(book.narrators)
    if isinstance(book, Audiobook):
        if not item_metadata.asin:
            patch["asin"] = book.asin
        if not item_metadata.publishedYear:
            patch["publishedYear"] = str(book.release_date.year)
        if not item_metadata.seriesName and book.series:
            series: dict[str, str] = {"name": book.series}
            if book.series_position:
                # Audiobookshelf drops a sequence that isn't a string
                series["sequence"] = str(book.series_position)
            patch["series"] = [series]
    elif not item_metadata.publishedYear and book.publish_date:
        patch["publishedYear"] = book.publish_date[:4]
    return patch


async def _fill_metadata(
    session: Session,
    client_session: ClientSession,
    item: _Item,
    book: Audiobook | ManualBookRequest,
) -> None:
    patch = missing_metadata(item.media.metadata, book)
    if not patch:
        return
    try:
        async with client_session.patch(
            _url(session, f"api/items/{item.id}/media"),
            json={"metadata": patch},
            headers=_headers(session),
        ) as response:
            logger.info(
                "ABS: filled in missing metadata"
                if response.ok
                else "ABS: could not fill in metadata",
                item_id=item.id,
                fields=sorted(patch),
                status=response.status,
            )
    except Exception as e:
        logger.warning("ABS: metadata update failed", error=str(e))


def _same_path(a: str, b: str) -> bool:
    return Path(a) == Path(b)


async def add_to_audiobookshelf(
    session: Session,
    client_session: ClientSession,
    imports: list[tuple[Path, Audiobook | ManualBookRequest | None]],
    sleep: Sleep = asyncio.sleep,
) -> None:
    """Hands freshly organized folders to Audiobookshelf. Never raises."""
    if not imports or not abs_config.is_connected(session):
        return

    waiting: list[tuple[Path, Audiobook | ManualBookRequest | None]] = []
    fall_back = False
    for folder, book in imports:
        audio = first_audio_file(folder)
        if audio is None or not await _notify_watcher(session, client_session, audio):
            fall_back = True
            continue
        waiting.append((folder, book))

    waited = 0
    while waiting and waited < APPEAR_TIMEOUT:
        await sleep(POLL_INTERVAL)
        waited += POLL_INTERVAL
        items = await _recent_items(session, client_session)
        for folder, book in list(waiting):
            item = next(
                (i for i in items if i.path and _same_path(i.path, str(folder))), None
            )
            if item is None:
                continue
            waiting.remove((folder, book))
            logger.info("ABS: new item is in the library", path=str(folder))
            if book is not None:
                await _fill_metadata(session, client_session, item, book)

    if waiting or fall_back:
        logger.info(
            "ABS: falling back to a full library scan",
            not_found=[str(folder) for folder, _ in waiting],
        )
        try:
            _ = await abs_trigger_scan(session, client_session)
        except Exception as e:
            logger.warning("ABS: library scan failed", error=str(e))


async def background_add_to_audiobookshelf(imports: list[tuple[str, str]]) -> None:
    """For BackgroundTasks: (target folder, asin_or_uuid) pairs."""
    with next(get_session()) as session:
        async with ClientSession() as client_session:
            await add_to_audiobookshelf(
                session,
                client_session,
                [(Path(path), get_book(session, key)) for path, key in imports],
            )
