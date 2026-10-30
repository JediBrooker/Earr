"""Recommendations from what people actually listen to in Audiobookshelf.

Requests say what someone wanted; listening says what they liked enough to
start, and how far they got. A finished book in a series is the strongest
signal there is: the next one.
"""

from aiohttp import ClientSession
from sqlmodel import Session, select

from app.internal.audible.catalog import (
    get_book_details,
    get_series_products,
    is_main_edition,
    sequence_key,
    series_sequence,
)
from app.internal.audiobookshelf.catalog import library_asins, listening_history
from app.internal.book_page import with_requests
from app.internal.language import is_preferred
from app.internal.models import Audiobook, AudiobookRequest, User
from app.internal.recommendations.user_recommendations import (
    AudiobookRecommendation,
)

SEED_BOOKS = 10
STARTED_ENOUGH = 0.5
"""Half way through a book counts as wanting the next one."""


async def listening_seeds(
    session: Session, client_session: ClientSession, user: User
) -> list[str]:
    """ASINs of the books this person listened to most recently."""
    history = await listening_history(session, client_session, user)
    return [b.asin for b in history if b.asin][:SEED_BOOKS]


async def up_next(
    session: Session, client_session: ClientSession, user: User, limit: int = 12
) -> list[AudiobookRecommendation]:
    """The next book in each series this person is listening to, if it isn't
    on the shelf or asked for yet."""
    history = await listening_history(session, client_session, user)
    if not history:
        return []
    owned = await library_asins(session, client_session)
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
        if not listened.asin or not (
            listened.finished or listened.progress >= STARTED_ENOUGH
        ):
            continue
        details = await get_book_details(client_session, listened.asin)
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
            if p.asin in owned or p.asin in mine
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
