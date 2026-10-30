"""Requesting a book from a background job, such as a followed author's new release.

The API route does the same for a person, using FastAPI's background tasks;
this one does everything inline because there is no request to attach to.
"""

from aiohttp import ClientSession
from sqlmodel import Session, select

from app.internal.models import (
    Audiobook,
    AudiobookRequest,
    EventEnum,
    GroupEnum,
    User,
)
from app.internal.notifications import send_all_notifications
from app.internal.query import query_sources
from app.internal.ranking.quality import quality_config
from app.util.log import logger


def starts_download(session: Session, user: User, book: Audiobook) -> bool:
    """Whether requesting this should start a search and download right away.

    A pre-order waits for its release day; the release-day search picks it up.
    """
    return (
        quality_config.get_auto_download(session)
        and user.is_above(GroupEnum.trusted)
        and book.is_released
    )


def store_book(session: Session, book: Audiobook) -> Audiobook:
    """Caches a book from Audible, filling in what an older copy lacks."""
    existing = session.get(Audiobook, book.asin)
    if existing is None:
        session.add(book)
        session.commit()
        return book
    if not existing.language and book.language:
        existing.language = book.language
    if book.series and book.series_position and not existing.series_position:
        existing.series = book.series
        existing.series_position = book.series_position
    # pre-order dates move
    existing.release_date = book.release_date
    session.add(existing)
    session.commit()
    return existing


def create_request(
    session: Session,
    user: User,
    book: Audiobook,
    *,
    via_follow: str | None = None,
) -> AudiobookRequest | None:
    """Stores the request only. Returns None if they had already asked for it.

    Notifying and starting the download is up to the caller, which knows
    whether it can hand those to a background task.
    """
    stored = store_book(session, book)
    if session.exec(
        select(AudiobookRequest).where(
            AudiobookRequest.asin == stored.asin,
            AudiobookRequest.user_username == user.username,
        )
    ).first():
        return None

    request = AudiobookRequest(
        asin=stored.asin, user_username=user.username, via_follow=via_follow
    )
    session.add(request)
    session.commit()
    logger.info(
        "Requested book", asin=stored.asin, title=stored.title, via_follow=via_follow
    )
    return request


async def request_book(
    session: Session,
    client_session: ClientSession,
    user: User,
    book: Audiobook,
    *,
    via_follow: str | None = None,
) -> AudiobookRequest | None:
    """Requests a book, notifies, and searches if it may download right away."""
    request = create_request(session, user, book, via_follow=via_follow)
    if request is None:
        return None
    asin = request.asin
    stored = session.get(Audiobook, asin)
    assert stored is not None

    try:
        await send_all_notifications(EventEnum.on_new_request, asin)
    except Exception as e:
        logger.error("Could not send request notification", asin=asin, error=str(e))

    if starts_download(session, user, stored):
        try:
            _ = await query_sources(
                asin_or_uuid=asin,
                session=session,
                client_session=client_session,
                start_auto_download=True,
            )
        except Exception as e:
            logger.warning("Search after request failed", asin=asin, error=str(e))
    return request
