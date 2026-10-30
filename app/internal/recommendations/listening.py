"""Recommendations from what people actually listen to in Audiobookshelf.

Requests say what someone wanted; listening says what they liked enough to
start, and how far they got. A finished book in a series is the strongest
signal there is: the next one.
"""

import asyncio

from aiohttp import ClientSession
from sqlmodel import Session, select

from app.internal.audible.catalog import (
    get_book_details,
    get_series_products,
    is_main_edition,
    sequence_key,
    series_sequence,
)
from app.internal.audible.matching import find_on_audible
from app.internal.audiobookshelf.catalog import (
    AbsListenedBook,
    library_index,
    listening_history,
)
from app.internal.book_page import with_requests
from app.internal.language import is_preferred
from app.internal.models import Audiobook, AudiobookRequest, User
from app.internal.recommendations.user_recommendations import (
    AudiobookRecommendation,
)

SEED_BOOKS = 10
STARTED_ENOUGH = 0.5
"""Half way through a book counts as wanting the next one."""


async def _asin(
    session: Session, client_session: ClientSession, listened: AbsListenedBook
) -> str | None:
    """The book's ASIN, found on Audible by title and author if the library
    has none."""
    if listened.asin:
        return listened.asin
    found = await find_on_audible(
        session,
        client_session,
        listened.title,
        listened.authors[0] if listened.authors else "",
    )
    return found.asin if found else None


async def listening_seeds(
    session: Session, client_session: ClientSession, user: User
) -> list[str]:
    """ASINs of the books this person listened to most recently."""
    history = (await listening_history(session, client_session, user))[:SEED_BOOKS]
    found = await asyncio.gather(*(_asin(session, client_session, b) for b in history))
    return [asin for asin in found if asin]


async def up_next(
    session: Session, client_session: ClientSession, user: User, limit: int = 12
) -> list[AudiobookRecommendation]:
    """The next book in each series this person is listening to, if it isn't
    on the shelf or asked for yet."""
    history = await listening_history(session, client_session, user)
    if not history:
        return []
    owned = await library_index(session, client_session)
    mine = set(
        session.exec(
            select(AudiobookRequest.asin).where(
                AudiobookRequest.user_username == user.username
            )
        ).all()
    )

    picks: list[tuple[str, Audiobook]] = []
    seen_series: set[str] = set()
    for listened in history:
        if len(picks) >= limit:
            break
        if not (listened.finished or listened.progress >= STARTED_ENOUGH):
            continue
        asin = await _asin(session, client_session, listened)
        if asin is None:
            continue
        details = await get_book_details(client_session, asin)
        series = details.primary_series if details else None
        if series is None or not series.sequence or series.asin in seen_series:
            continue
        seen_series.add(series.asin)

        editions = await get_series_products(
            client_session, series.asin, all_editions=True
        )
        # a number counts as had when any edition of it is owned or asked for
        had = {
            series_sequence(p, series.asin)
            for p in editions
            if p.asin in mine or owned.has(p.asin, p.title, [a.name for a in p.authors])
        }
        current = sequence_key(series.sequence)
        candidates = [
            p
            for p in await get_series_products(client_session, series.asin)
            if is_preferred(session, p.language)
            and is_main_edition(p)
            and sequence_key(series_sequence(p, series.asin)) > current
            and series_sequence(p, series.asin) not in had
        ]
        if not candidates:
            continue
        following = candidates[0]
        picks.append(
            (
                f"Next in {series.title}, after {listened.title}",
                following.to_audiobook(),
            )
        )

    books = with_requests(session, [book for _, book in picks], user.username)
    return [
        AudiobookRecommendation(book=book, reason=reason)
        for (reason, _), book in zip(picks, books, strict=True)
    ]
