"""Suggesting what to follow from what is already in the Audiobookshelf library.

Someone with forty series on the shelf shouldn't have to find and follow each
one. Audiobookshelf knows the series and authors; Audible knows their ids,
which following needs, so each series is looked up through one of its books.
"""

import asyncio

import aiohttp
from aiohttp import ClientSession
from pydantic import BaseModel
from rapidfuzz import utils
from sqlmodel import Session

from app.internal.audible.catalog import get_book_details
from app.internal.audible.matching import find_on_audible
from app.internal.audiobookshelf.catalog import (
    AbsSeries,
    library_authors,
    library_series,
)
from app.internal.follows import find_follow, start_following
from app.internal.models import FollowKindEnum, User
from app.util.db import get_session
from app.util.log import logger

MAX_SERIES = 80
"""Looked up on Audible one by one, so the list is capped."""
CONCURRENCY = 6


class FollowSuggestion(BaseModel):
    kind: FollowKindEnum
    audible_id: str
    name: str
    owned: int
    following: bool


def _key(name: str) -> str:
    return utils.default_process(name)


async def _book_asins(
    session: Session, client_session: ClientSession, series: AbsSeries
) -> list[str]:
    """ASINs of up to two of the series' books, looking them up by title and
    author on Audible when the library doesn't have them."""
    asins = [b.asin for b in series.books if b.asin][:2]
    if asins:
        return asins
    for book in series.books[:2]:
        found = await find_on_audible(session, client_session, book.title, book.author)
        if found is not None:
            asins.append(found.asin)
    return asins


async def _resolve_series(
    session: Session,
    client_session: ClientSession,
    series: AbsSeries,
    limit: asyncio.Semaphore,
) -> tuple[str, str] | None:
    """Audible's id and name for a series, from one of its books."""
    async with limit:
        asins = await _book_asins(session, client_session, series)
    for asin in asins:
        async with limit:
            details = await get_book_details(client_session, asin)
        if details is None:
            continue
        match = next(
            (s for s in details.series if _key(s.title) == _key(series.name)),
            details.primary_series,
        )
        if match is not None:
            return match.asin, match.title
    return None


async def suggest_from_library(
    session: Session, client_session: ClientSession, user: User
) -> list[FollowSuggestion]:
    """Series first, then authors, each with the most books owned first."""
    series = sorted(
        await library_series(session, client_session),
        key=lambda s: len(s.books),
        reverse=True,
    )[:MAX_SERIES]
    limit = asyncio.Semaphore(CONCURRENCY)
    resolved = await asyncio.gather(
        *(_resolve_series(session, client_session, s, limit) for s in series)
    )

    suggestions: list[FollowSuggestion] = []
    seen: set[tuple[FollowKindEnum, str]] = set()
    for found, owned in zip(resolved, series, strict=True):
        if found is None or (FollowKindEnum.series, found[0]) in seen:
            continue
        seen.add((FollowKindEnum.series, found[0]))
        suggestions.append(
            FollowSuggestion(
                kind=FollowKindEnum.series,
                audible_id=found[0],
                name=found[1],
                owned=len(owned.books),
                following=find_follow(
                    session, user.username, FollowKindEnum.series, found[0]
                )
                is not None,
            )
        )

    authors = sorted(
        await library_authors(session, client_session),
        key=lambda a: a.num_books,
        reverse=True,
    )
    for author in authors:
        audible_id = author.asin or author.name
        if (FollowKindEnum.author, audible_id) in seen:
            continue
        seen.add((FollowKindEnum.author, audible_id))
        suggestions.append(
            FollowSuggestion(
                kind=FollowKindEnum.author,
                audible_id=audible_id,
                name=author.name,
                owned=author.num_books,
                following=find_follow(
                    session, user.username, FollowKindEnum.author, audible_id
                )
                is not None,
            )
        )
    return suggestions


async def follow_many(
    username: str, choices: list[tuple[FollowKindEnum, str, str]]
) -> None:
    """Follows each choice in turn. Run in the background: each one asks Audible."""
    with next(get_session()) as session:
        user = session.get(User, username)
        if user is None:
            return
        async with ClientSession(
            timeout=aiohttp.ClientTimeout(total=60)
        ) as client_session:
            for kind, audible_id, name in choices:
                try:
                    _ = await start_following(
                        session, client_session, user, kind, audible_id, name
                    )
                except Exception as e:
                    logger.warning("Following failed", follow=name, error=str(e))
    logger.info("Followed from the library", count=len(choices))
