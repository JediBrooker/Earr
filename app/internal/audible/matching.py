"""Telling whether two descriptions are the same book, and finding a book on
Audible from just its title and author.

An ASIN settles it when both sides have one, but many libraries don't: books
added from files often carry only a title and an author.
"""

import time

from aiohttp import ClientSession
from rapidfuzz import fuzz, utils
from sqlmodel import Session

from app.internal.audible.search import search_audible_books
from app.internal.language import is_preferred
from app.internal.models import Audiobook

TITLE_MATCH = 85
AUTHOR_MATCH = 80
CACHE_SECONDS = 6 * 60 * 60


def same_author(a: str, b: str) -> bool:
    """Close spellings, or the same surname and first initial ("J.R.R. Tolkien"
    and "J. R. R. Tolkien"), but not Frank and Brian Herbert."""
    a_parts = utils.default_process(a).split()
    b_parts = utils.default_process(b).split()
    if not a_parts or not b_parts:
        return False
    if fuzz.token_sort_ratio(" ".join(a_parts), " ".join(b_parts)) >= AUTHOR_MATCH + 5:
        return True
    return a_parts[-1] == b_parts[-1] and a_parts[0][0] == b_parts[0][0]


def book_key(title: str, authors: list[str]) -> str:
    """The title before any subtitle, plus the first author's surname.

    Good enough to recognise the same book in two places without an ASIN,
    and strict enough that two books sharing a title rarely collide.
    """
    main = utils.default_process(title.split(":")[0])
    surname = ""
    if authors:
        parts = utils.default_process(authors[0]).split()
        surname = parts[-1] if parts else ""
    return f"{main}|{surname}"


def match_book(
    session: Session, title: str, author: str, results: list[Audiobook]
) -> Audiobook | None:
    """The result that really is this book: the title and an author have to match."""

    def score(book: Audiobook) -> tuple[bool, bool, float]:
        return (
            is_preferred(session, book.language),
            "dramatized" not in book.title.lower(),
            # the closest title, so "Dune" beats "Dune Messiah"
            fuzz.ratio(title, book.title, processor=utils.default_process),
        )

    candidates: list[Audiobook] = []
    for book in results:
        if (
            fuzz.token_set_ratio(title, book.title, processor=utils.default_process)
            < TITLE_MATCH
        ):
            continue
        if author and not any(same_author(author, a) for a in book.authors):
            continue
        candidates.append(book)
    if not candidates:
        return None
    return max(candidates, key=score)


_found: dict[str, tuple[float, Audiobook | None]] = {}


async def find_on_audible(
    session: Session, client_session: ClientSession, title: str, author: str
) -> Audiobook | None:
    """Looks a book up on Audible by title and author. None when nothing matches."""
    if not title:
        return None
    key = f"{title}|{author}".casefold()
    hit = _found.get(key)
    if hit and time.time() - hit[0] < CACHE_SECONDS:
        return hit[1]
    results = await search_audible_books(
        client_session, f"{title} {author}".strip(), num_results=10
    )
    book = match_book(session, title, author, results)
    _found[key] = (time.time(), book)
    return book
