"""Browsing what is already in the Audiobookshelf library, for the Library page.

The item list and the search endpoint return differently shaped items (the
list is always minified, search returns full items), so both are read leniently
and mapped to one small model.
"""

import posixpath
from math import ceil

from aiohttp import ClientSession
from pydantic import BaseModel
from sqlmodel import Session, col, select

from app.internal.audiobookshelf.config import abs_config
from app.internal.models import Audiobook, DownloadStatusEnum
from app.util.connection import USER_AGENT
from app.util.log import logger

PAGE_SIZE = 48


class LibraryBook(BaseModel):
    id: str
    title: str
    subtitle: str | None = None
    author: str = ""
    narrator: str = ""
    series: str = ""
    year: str | None = None
    hours: float | None = None
    cover_url: str | None = None
    link: str | None = None
    """Where to open the book, if anywhere."""


class LibraryPage(BaseModel):
    books: list[LibraryBook]
    total: int
    page: int
    source: str
    """"audiobookshelf", or "earr" when only Earr's own records are available."""
    error: str | None = None

    @property
    def pages(self) -> int:
        return max(1, ceil(self.total / PAGE_SIZE))


class _Named(BaseModel):
    name: str = ""


class _Series(BaseModel):
    name: str = ""
    sequence: str | None = None


class _Metadata(BaseModel):
    title: str | None = None
    subtitle: str | None = None
    # list items
    authorName: str | None = None
    narratorName: str | None = None
    seriesName: str | None = None
    # search results
    authors: list[_Named] = []
    narrators: list[str] = []
    series: list[_Series] | _Series | None = None
    publishedYear: str | None = None


class _Media(BaseModel):
    metadata: _Metadata = _Metadata()
    duration: float | None = None


class _Item(BaseModel):
    id: str
    mediaType: str = "book"
    media: _Media = _Media()


class _ItemPage(BaseModel):
    results: list[_Item] = []
    total: int = 0


class _SearchHit(BaseModel):
    libraryItem: _Item


class _SearchResults(BaseModel):
    book: list[_SearchHit] = []


def _series_label(metadata: _Metadata) -> str:
    if metadata.seriesName:
        return metadata.seriesName
    series = metadata.series
    if isinstance(series, _Series):
        series = [series]
    if not series:
        return ""
    first = series[0]
    return f"{first.name} #{first.sequence}" if first.sequence else first.name


def _to_book(item: _Item, base_url: str, cover_path: str) -> LibraryBook:
    metadata = item.media.metadata
    duration = item.media.duration
    return LibraryBook(
        id=item.id,
        title=metadata.title or "Untitled",
        subtitle=metadata.subtitle,
        author=metadata.authorName or ", ".join(a.name for a in metadata.authors),
        narrator=metadata.narratorName or ", ".join(metadata.narrators),
        series=_series_label(metadata),
        year=metadata.publishedYear,
        hours=round(duration / 3600, 1) if duration else None,
        cover_url=f"{cover_path}/{item.id}",
        link=posixpath.join(base_url, f"item/{item.id}"),
    )


def _headers(session: Session) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {abs_config.get_api_token(session) or ''}",
        "User-Agent": USER_AGENT,
    }


async def browse_audiobookshelf(
    session: Session,
    client_session: ClientSession,
    *,
    page: int,
    query: str,
    cover_path: str,
) -> LibraryPage:
    base_url = abs_config.get_base_url(session) or ""
    library_id = abs_config.get_library_id(session)
    try:
        if query:
            async with client_session.get(
                posixpath.join(base_url, f"api/libraries/{library_id}/search"),
                params={"q": query, "limit": str(PAGE_SIZE)},
                headers=_headers(session),
            ) as response:
                response.raise_for_status()
                hits = _SearchResults.model_validate(await response.json()).book
            items = [hit.libraryItem for hit in hits]
            total = len(items)
        else:
            async with client_session.get(
                posixpath.join(base_url, f"api/libraries/{library_id}/items"),
                params={
                    "limit": str(PAGE_SIZE),
                    "page": str(page),
                    # without a sort Audiobookshelf's paging isn't stable
                    "sort": "addedAt",
                    "desc": "1",
                },
                headers=_headers(session),
            ) as response:
                response.raise_for_status()
                listing = _ItemPage.model_validate(await response.json())
            items, total = listing.results, listing.total
    except Exception as e:
        logger.warning("ABS: could not browse the library", error=str(e))
        return LibraryPage(
            books=[],
            total=0,
            page=page,
            source="audiobookshelf",
            error="Couldn't reach Audiobookshelf. Check the URL and API token in Settings.",
        )

    books = [
        _to_book(item, base_url, cover_path)
        for item in items
        if item.mediaType == "book"
    ]
    return LibraryPage(books=books, total=total, page=page, source="audiobookshelf")


def browse_earr(session: Session, *, page: int, query: str) -> LibraryPage:
    """The books Earr itself confirmed in the library, when Audiobookshelf isn't connected."""
    books = session.exec(
        select(Audiobook)
        .where(Audiobook.download_status == DownloadStatusEnum.downloaded)
        .order_by(col(Audiobook.updated_at).desc())
    ).all()
    if query:
        wanted = query.lower()
        books = [
            b
            for b in books
            if wanted in b.title.lower() or any(wanted in a.lower() for a in b.authors)
        ]
    shown = books[page * PAGE_SIZE : (page + 1) * PAGE_SIZE]
    return LibraryPage(
        books=[
            LibraryBook(
                id=b.asin,
                title=b.title,
                subtitle=b.subtitle,
                author=", ".join(b.authors),
                narrator=", ".join(b.narrators),
                series=f"{b.series} #{b.series_position}"
                if b.series and b.series_position
                else b.series or "",
                year=str(b.release_date.year),
                hours=b.runtime_length_hrs or None,
                cover_url=b.cover_image,
            )
            for b in shown
        ],
        total=len(books),
        page=page,
        source="earr",
    )


async def fetch_cover(
    session: Session, client_session: ClientSession, item_id: str
) -> tuple[bytes, str] | None:
    """An item's cover, fetched by Earr so the browser never needs to reach Audiobookshelf."""
    base_url = abs_config.get_base_url(session)
    if not base_url:
        return None
    try:
        async with client_session.get(
            posixpath.join(base_url, f"api/items/{item_id}/cover"),
            params={"width": "400"},
            headers=_headers(session),
        ) as response:
            if not response.ok:
                return None
            return await response.read(), response.content_type or "image/jpeg"
    except Exception:
        return None
