"""Approving and declining requests that wait for an admin.

Requests from untrusted users sit on the wishlist until someone looks at them.
Approving marks the request, so the job that keeps searching for outstanding
requests treats it like a trusted one, and sends onRequestApproved. Declining
sends onRequestDeclined with the reason and then removes the request; the
notification goes out first so it can still name whoever asked.
"""

from sqlmodel import Session, col, delete, select

from app.internal.db_queries import get_book
from app.internal.models import (
    Audiobook,
    AudiobookRequest,
    EventEnum,
    ManualBookRequest,
)
from app.internal.notifications import (
    send_all_manual_notifications,
    send_all_notifications,
)
from app.util.log import logger

NO_REASON = "No reason given"


class RequestNotFound(LookupError):
    pass


def _audiobook_requests(session: Session, asin: str) -> list[AudiobookRequest]:
    return list(
        session.exec(
            select(AudiobookRequest).where(AudiobookRequest.asin == asin)
        ).all()
    )


def approve(session: Session, asin_or_uuid: str) -> Audiobook | ManualBookRequest:
    """Marks every request for the book approved. Notifying is up to the caller."""
    book = get_book(session, asin_or_uuid)
    if isinstance(book, Audiobook):
        requests = _audiobook_requests(session, book.asin)
        if not requests:
            raise RequestNotFound(asin_or_uuid)
        for request in requests:
            request.approved = True
            session.add(request)
    elif isinstance(book, ManualBookRequest):
        book.approved = True
        session.add(book)
    else:
        raise RequestNotFound(asin_or_uuid)
    session.commit()
    return book


async def notify_approved(book: str | ManualBookRequest) -> None:
    """Takes an ASIN or a detached copy, since it runs after the request's session closed."""
    if isinstance(book, ManualBookRequest):
        await send_all_manual_notifications(EventEnum.on_request_approved, book)
    else:
        await send_all_notifications(EventEnum.on_request_approved, book)


async def decline(session: Session, asin_or_uuid: str, reason: str) -> str:
    """Notifies, then removes the request. Returns the book's title."""
    replacements = {"declineReason": reason.strip() or NO_REASON}
    book = get_book(session, asin_or_uuid)

    if isinstance(book, Audiobook):
        if not _audiobook_requests(session, book.asin):
            raise RequestNotFound(asin_or_uuid)
        title = book.title
        try:
            await send_all_notifications(
                EventEnum.on_request_declined, book.asin, replacements
            )
        except Exception as e:
            # a broken webhook must not stop the request being declined
            logger.error("Failed to send the declined notification", error=str(e))
        _ = session.exec(
            delete(AudiobookRequest).where(col(AudiobookRequest.asin) == book.asin)
        )
    elif isinstance(book, ManualBookRequest):
        title = book.title
        await send_all_manual_notifications(
            EventEnum.on_request_declined,
            ManualBookRequest.model_validate(book),
            replacements,
        )
        session.delete(book)
    else:
        raise RequestNotFound(asin_or_uuid)

    session.commit()
    return title
