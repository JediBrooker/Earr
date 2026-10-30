"""What is in the Audiobookshelf library, and who has listened to what.

Used to suggest series and authors to follow, to leave out books already on
the shelf, and to recommend from what people actually listen to. Every answer
is read defensively: the shapes differ a little between Audiobookshelf
versions, and a missing field should cost a suggestion, not the page.
"""

import time
from datetime import datetime
from typing import cast

from aiohttp import ClientSession
from pydantic import BaseModel
from sqlmodel import Session

from app.internal.audible.matching import book_key
from app.internal.audiobookshelf.config import abs_config
from app.internal.models import Audiobook, User
from app.util.connection import USER_AGENT
from app.util.log import logger

PAGE_SIZE = 500
MAX_PAGES = 40
"""20,000 items is more than any household library; stops a runaway loop."""
CACHE_SECONDS = 15 * 60

Json = dict[str, object]


class AbsSeriesBook(BaseModel):
    asin: str | None
    title: str
    sequence: str
    author: str = ""


class LibraryIndex(BaseModel):
    """What is on the shelf, by ASIN and by title and author."""

    asins: set[str] = set()
    keys: set[str] = set()
    """book_key() of every book, for those with no ASIN."""

    def has(self, asin: str | None, title: str, authors: list[str]) -> bool:
        if asin and asin in self.asins:
            return True
        return book_key(title, authors) in self.keys

    def has_book(self, book: Audiobook) -> bool:
        return self.has(book.asin, book.title, book.authors)


class AbsSeries(BaseModel):
    name: str
    books: list[AbsSeriesBook]


class AbsAuthor(BaseModel):
    name: str
    asin: str | None
    num_books: int


class AbsListenedBook(BaseModel):
    asin: str | None
    title: str
    authors: list[str]
    progress: float
    """0 to 1."""
    finished: bool
    last_update: datetime


def _dict(value: object) -> Json:
    return cast(Json, value) if isinstance(value, dict) else {}


def _list(value: object) -> list[object]:
    return cast(list[object], value) if isinstance(value, list) else []


def _str(value: object) -> str:
    return str(value) if isinstance(value, (str, int, float)) else ""


def _metadata(item: Json) -> Json:
    return _dict(_dict(item.get("media")).get("metadata"))


def _authors(metadata: Json) -> list[str]:
    names = [_str(_dict(a).get("name")) for a in _list(metadata.get("authors"))]
    names = [n for n in names if n]
    if not names and _str(metadata.get("authorName")):
        names = [n.strip() for n in _str(metadata.get("authorName")).split(",")]
    return names


_cache: dict[str, tuple[float, object]] = {}


def _cached(key: str) -> object | None:
    hit = _cache.get(key)
    if hit and time.time() - hit[0] < CACHE_SECONDS:
        return hit[1]
    return None


def forget_cache() -> None:
    _cache.clear()


async def _request(
    session: Session,
    client_session: ClientSession,
    method: str,
    path: str,
    *,
    params: dict[str, str] | None = None,
    body: Json | None = None,
) -> Json | None:
    base_url = abs_config.get_base_url(session)
    token = abs_config.get_api_token(session)
    if not base_url or not token:
        return None
    try:
        async with client_session.request(
            method,
            f"{base_url}/{path.lstrip('/')}",
            params=params,
            json=body,
            headers={"Authorization": f"Bearer {token}", "User-Agent": USER_AGENT},
        ) as response:
            if not response.ok:
                logger.debug("ABS: request failed", path=path, status=response.status)
                return None
            return _dict(cast(object, await response.json(content_type=None)))
    except Exception as e:
        logger.debug("ABS: request failed", path=path, error=str(e))
        return None


async def _library_pages(
    session: Session, client_session: ClientSession, path: str, extra: dict[str, str]
) -> list[Json]:
    results: list[Json] = []
    for page in range(MAX_PAGES):
        payload = await _request(
            session,
            client_session,
            "GET",
            path,
            params={"limit": str(PAGE_SIZE), "page": str(page)} | extra,
        )
        if payload is None:
            break
        batch = [_dict(r) for r in _list(payload.get("results"))]
        results.extend(batch)
        if len(batch) < PAGE_SIZE:
            break
    return results


async def library_index(
    session: Session, client_session: ClientSession
) -> LibraryIndex:
    """Every book in the library, to tell whether something is already there."""
    library_id = abs_config.get_library_id(session)
    if not library_id or not abs_config.is_connected(session):
        return LibraryIndex()
    key = f"index:{library_id}"
    if (hit := _cached(key)) is not None:
        return cast(LibraryIndex, hit)
    items = await _library_pages(
        session,
        client_session,
        f"api/libraries/{library_id}/items",
        {"minified": "1"},
    )
    index = LibraryIndex()
    for item in items:
        metadata = _metadata(item)
        if asin := _str(metadata.get("asin")):
            index.asins.add(asin)
        if title := _str(metadata.get("title")):
            index.keys.add(book_key(title, _authors(metadata)))
    _cache[key] = (time.time(), index)
    return index


async def library_asins(session: Session, client_session: ClientSession) -> set[str]:
    """The ASIN of every book in the library that has one."""
    return (await library_index(session, client_session)).asins


def _sequence(item: Json, series_name: str) -> str:
    series = _metadata(item).get("series")
    if isinstance(series, dict):
        return _str(cast(Json, series).get("sequence"))
    for entry in _list(series):
        entry = _dict(entry)
        if _str(entry.get("name")).casefold() == series_name.casefold():
            return _str(entry.get("sequence"))
    return _str(item.get("sequence"))


async def library_series(
    session: Session, client_session: ClientSession
) -> list[AbsSeries]:
    library_id = abs_config.get_library_id(session)
    if not library_id or not abs_config.is_connected(session):
        return []
    results = await _library_pages(
        session, client_session, f"api/libraries/{library_id}/series", {}
    )
    found: list[AbsSeries] = []
    for result in results:
        name = _str(result.get("name"))
        if not name:
            continue
        books = [
            AbsSeriesBook(
                asin=_str(_metadata(item).get("asin")) or None,
                title=_str(_metadata(item).get("title")),
                sequence=_sequence(item, name),
                author=next(iter(_authors(_metadata(item))), ""),
            )
            for item in (_dict(b) for b in _list(result.get("books")))
        ]
        found.append(AbsSeries(name=name, books=books))
    return found


async def library_authors(
    session: Session, client_session: ClientSession
) -> list[AbsAuthor]:
    library_id = abs_config.get_library_id(session)
    if not library_id or not abs_config.is_connected(session):
        return []
    payload = await _request(
        session, client_session, "GET", f"api/libraries/{library_id}/authors"
    )
    if payload is None:
        return []
    authors: list[AbsAuthor] = []
    for raw in _list(payload.get("authors")):
        author = _dict(raw)
        name = _str(author.get("name"))
        if not name:
            continue
        num_books = author.get("numBooks")
        authors.append(
            AbsAuthor(
                name=name,
                asin=_str(author.get("asin")) or None,
                num_books=num_books if isinstance(num_books, int) else 0,
            )
        )
    return authors


async def find_abs_user_id(
    session: Session, client_session: ClientSession, user: User
) -> str | None:
    """The Audiobookshelf account that belongs to an Earr user.

    Known for accounts that sign in with Audiobookshelf; otherwise matched by
    username, which is how most households name their accounts anyway.
    """
    if user.abs_user_id:
        return user.abs_user_id
    payload = await _request(session, client_session, "GET", "api/users")
    if payload is None:
        return None
    for raw in _list(payload.get("users")):
        account = _dict(raw)
        if _str(account.get("username")).casefold() == user.username.casefold():
            return _str(account.get("id")) or None
    return None


def _when(value: object) -> datetime:
    if isinstance(value, (int, float)) and value > 0:
        return datetime.fromtimestamp(value / 1000)
    return datetime.fromtimestamp(0)


async def listening_history(
    session: Session, client_session: ClientSession, user: User, limit: int = 20
) -> list[AbsListenedBook]:
    """What this person has been listening to, most recent first."""
    if not abs_config.is_connected(session):
        return []
    key = f"listening:{user.username}"
    if (hit := _cached(key)) is not None:
        return cast(list[AbsListenedBook], hit)

    abs_id = await find_abs_user_id(session, client_session, user)
    if not abs_id:
        _cache[key] = (time.time(), [])
        return []
    account = await _request(session, client_session, "GET", f"api/users/{abs_id}")
    progress = [
        _dict(p)
        for p in _list((account or {}).get("mediaProgress"))
        # podcasts have an episode id; started books only
        if not _dict(p).get("episodeId")
        and isinstance(_dict(p).get("progress"), (int, float))
        and cast(float, _dict(p).get("progress")) > 0.02
    ]
    progress.sort(key=lambda p: _when(p.get("lastUpdate")), reverse=True)
    progress = progress[:limit]
    item_ids = [_str(p.get("libraryItemId")) for p in progress]
    item_ids = [i for i in item_ids if i]

    items: dict[str, Json] = {}
    if item_ids:
        payload = await _request(
            session,
            client_session,
            "POST",
            "api/items/batch/get",
            body={"libraryItemIds": item_ids},
        )
        for raw in _list((payload or {}).get("libraryItems")):
            item = _dict(raw)
            items[_str(item.get("id"))] = item

    history: list[AbsListenedBook] = []
    for entry in progress:
        item = items.get(_str(entry.get("libraryItemId")))
        if item is None:
            continue
        metadata = _metadata(item)
        history.append(
            AbsListenedBook(
                asin=_str(metadata.get("asin")) or None,
                title=_str(metadata.get("title")),
                authors=_authors(metadata),
                progress=float(cast(float, entry.get("progress"))),
                finished=entry.get("isFinished") is True,
                last_update=_when(entry.get("lastUpdate")),
            )
        )
    _cache[key] = (time.time(), history)
    return history
