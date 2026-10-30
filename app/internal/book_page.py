"""Everything the book page shows: details, the series in order, more by the author."""

from aiohttp import ClientSession
from pydantic import BaseModel
from sqlmodel import Session, col, select

from app.internal.audible.catalog import (
    BookDetails,
    get_author_products,
    get_book_details,
    get_series_products,
    is_main_edition,
    series_sequence,
)
from app.internal.audible.types import (
    AudibleContributor,
    AudibleProduct,
    AudibleSeries,
)
from app.internal.follows import find_follow
from app.internal.language import is_preferred, language_label
from app.internal.models import (
    Audiobook,
    AudiobookWithRequests,
    Follow,
    FollowKindEnum,
    User,
)

MORE_BY_AUTHOR = 12


class SeriesEntry(BaseModel):
    sequence: str
    book: AudiobookWithRequests


class BookPage(BaseModel):
    book: AudiobookWithRequests
    description: str | None
    format_type: str | None
    series: AudibleSeries | None
    series_books: list[SeriesEntry]
    author: AudibleContributor | None
    author_books: list[AudiobookWithRequests]
    following_author: Follow | None
    following_series: Follow | None

    @property
    def language(self) -> str:
        return language_label(self.book.book.language)

    @property
    def missing_in_series(self) -> list[Audiobook]:
        """Books in the series nobody has asked for and that aren't in the library."""
        return [
            entry.book.book
            for entry in self.series_books
            if not entry.book.book.downloaded and not entry.book.requests
        ]


def with_requests(
    session: Session, books: list[Audiobook], username: str
) -> list[AudiobookWithRequests]:
    """Swaps in the stored copy of each book, which knows who requested it."""
    stored = {
        b.asin: b
        for b in session.exec(
            select(Audiobook).where(col(Audiobook.asin).in_([b.asin for b in books]))
        ).all()
    }
    result: list[AudiobookWithRequests] = []
    for book in books:
        known = stored.get(book.asin)
        if known is not None and not known.language:
            known.language = book.language
        result.append(
            AudiobookWithRequests(
                book=known or book,
                requests=list(known.requests) if known else [],
                username=username,
            )
        )
    return result


def _pick_editions(
    session: Session,
    editions: list[AudibleProduct],
    series_asin: str,
    current_asin: str,
) -> list[AudibleProduct]:
    """One edition per number in the series, preferring the one being viewed,
    then one that is requested or in the library, then the earliest."""
    stored = {
        b.asin: b
        for b in session.exec(
            select(Audiobook).where(col(Audiobook.asin).in_([p.asin for p in editions]))
        ).all()
    }

    def rank(product: AudibleProduct) -> int:
        if product.asin == current_asin:
            return 0
        known = stored.get(product.asin)
        if known is not None and (known.downloaded or known.requests):
            return 1
        return 2

    best: dict[str, AudibleProduct] = {}
    order: list[str] = []
    for product in editions:
        sequence = series_sequence(product, series_asin)
        key = f"{product.language}#{sequence}" if sequence else product.asin
        if key not in best:
            order.append(key)
            best[key] = product
        elif rank(product) < rank(best[key]):
            best[key] = product
    return [best[key] for key in order]


async def build(
    session: Session,
    client_session: ClientSession,
    user: User,
    asin: str,
) -> BookPage | None:
    details: BookDetails | None = await get_book_details(client_session, asin)
    stored = session.get(Audiobook, asin)
    if details is None and stored is None:
        return None
    book = details.book if details else stored
    assert book is not None
    [current] = with_requests(session, [book], user.username)

    series = details.primary_series if details else None
    series_books: list[SeriesEntry] = []
    if series:
        editions = [
            p
            for p in await get_series_products(
                client_session, series.asin, all_editions=True
            )
            # other languages' editions are separate books in Audible's list
            if p.asin == asin
            or (is_preferred(session, p.language) and is_main_edition(p))
        ]
        products = _pick_editions(session, editions, series.asin, asin)
        entries = with_requests(
            session, [p.to_audiobook() for p in products], user.username
        )
        series_books = [
            SeriesEntry(sequence=series_sequence(p, series.asin), book=entry)
            for p, entry in zip(products, entries, strict=True)
        ]

    authors = details.authors if details else []
    author = next((a for a in authors if a.asin), authors[0] if authors else None)
    author_books: list[AudiobookWithRequests] = []
    if author:
        in_series = {entry.book.book.asin for entry in series_books}
        products = [
            p
            for p in await get_author_products(client_session, author)
            if p.asin != asin
            and p.asin not in in_series
            and is_preferred(session, p.language)
            and is_main_edition(p)
        ][:MORE_BY_AUTHOR]
        author_books = with_requests(
            session, [p.to_audiobook() for p in products], user.username
        )

    return BookPage(
        book=current,
        description=details.description if details else None,
        format_type=details.format_type if details else None,
        series=series,
        series_books=series_books,
        author=author,
        author_books=author_books,
        following_author=find_follow(
            session,
            user.username,
            FollowKindEnum.author,
            author.asin or author.name,
        )
        if author
        else None,
        following_series=find_follow(
            session, user.username, FollowKindEnum.series, series.asin
        )
        if series
        else None,
    )
