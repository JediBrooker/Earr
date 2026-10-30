"""Release dates of requested books: what is coming up, and what just came out."""

from datetime import datetime, timedelta

from pydantic import BaseModel
from sqlmodel import Session, col, select

from app.internal.models import Audiobook, AudiobookRequest, User

RECENT_DAYS = 30


class CalendarEntry(BaseModel):
    book: Audiobook
    requested_by: list[str]
    via_follow: list[str]
    """Followed authors or series that requested it."""

    @property
    def month_key(self) -> str:
        return self.book.release_date.strftime("%Y-%m")

    @property
    def month_label(self) -> str:
        return self.book.release_date.strftime("%B %Y")

    @property
    def day_label(self) -> str:
        return self.book.release_date.strftime("%a %-d")

    @property
    def status(self) -> str:
        if self.book.downloaded:
            return "In library"
        if self.book.download_status and self.book.download_status.value == "grabbed":
            return "Downloading"
        if self.book.is_released:
            return "Out now"
        return "Pre-ordered"


class Calendar(BaseModel):
    upcoming: list[CalendarEntry]
    recent: list[CalendarEntry]


def build(session: Session, user: User) -> Calendar:
    """Admins see every request; everyone else their own."""
    now = datetime.now()
    requested = select(AudiobookRequest.asin)
    if not user.is_admin():
        requested = requested.where(AudiobookRequest.user_username == user.username)
    query = (
        select(Audiobook)
        .where(
            col(Audiobook.release_date) >= now - timedelta(days=RECENT_DAYS),
            col(Audiobook.asin).in_(requested),
        )
        .order_by(col(Audiobook.release_date))
    )
    upcoming: list[CalendarEntry] = []
    recent: list[CalendarEntry] = []
    for book in session.exec(query).all():
        requests = [
            r
            for r in book.requests
            if user.is_admin() or r.user_username == user.username
        ]
        entry = CalendarEntry(
            book=book,
            requested_by=[r.user_username for r in requests],
            via_follow=sorted({r.via_follow for r in requests if r.via_follow}),
        )
        (recent if book.is_released else upcoming).append(entry)
    recent.reverse()
    return Calendar(upcoming=upcoming, recent=recent)
