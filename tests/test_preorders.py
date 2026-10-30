"""Books requested before they are out."""

from datetime import datetime, timedelta
from unittest import mock

from aiohttp import ClientSession
from sqlmodel import Session

from app.internal import research
from app.internal.models import AudiobookRequest, DownloadStatusEnum, GroupEnum, User
from app.internal.ranking.quality import quality_config
from app.internal.release_calendar import build as build_calendar
from app.internal.requesting import starts_download
from tests.conftest import standalone_book


def trusted(session: Session) -> User:
    u = User(username="trusty", password="x", group=GroupEnum.trusted)
    session.add(u)
    session.commit()
    return u


def requested(session: Session, u: User, asin: str, released: datetime, **book: object):
    b = standalone_book(asin, release_date=released, **book)
    session.add(b)
    session.add(AudiobookRequest(asin=asin, user_username=u.username))
    session.commit()
    return b


class TestPreorders:
    def test_a_preorder_does_not_start_a_download(self, session: Session):
        quality_config.set_auto_download(session, True)
        u = trusted(session)
        soon = standalone_book("S", release_date=datetime.now() + timedelta(days=5))
        out = standalone_book("O", release_date=datetime.now() - timedelta(days=5))

        assert not starts_download(session, u, soon)
        assert starts_download(session, u, out)

    def test_research_leaves_unreleased_books_alone(self, session: Session):
        u = trusted(session)
        _ = requested(session, u, "S", datetime.now() + timedelta(days=5))
        _ = requested(session, u, "O", datetime.now() - timedelta(days=5))

        found = research.find_audiobooks_to_retry(session, 3600, 5)
        assert [b.asin for b in found] == ["O"]

    def test_release_day_picks_up_what_just_came_out(self, session: Session):
        u = trusted(session)
        now = datetime.now()
        _ = requested(session, u, "TODAY", now - timedelta(hours=2))
        _ = requested(
            session,
            u,
            "SEARCHED",
            now - timedelta(days=1),
            last_searched_at=now - timedelta(hours=1),
        )
        _ = requested(session, u, "LONG_AGO", now - timedelta(days=60))
        _ = requested(session, u, "FUTURE", now + timedelta(days=1))
        _ = requested(
            session,
            u,
            "GRABBED",
            now - timedelta(hours=3),
            download_status=DownloadStatusEnum.grabbed,
        )

        assert [b.asin for b in research.find_newly_released(session)] == ["TODAY"]

    async def test_release_day_searches_even_with_research_off(self, session: Session):
        quality_config.set_auto_download(session, True)
        u = trusted(session)
        _ = requested(session, u, "TODAY", datetime.now() - timedelta(hours=2))

        with mock.patch.object(
            research, "retry_one", new=mock.AsyncMock(return_value=True)
        ) as retry:
            async with ClientSession() as cs:
                started = await research.run_release_day(session, cs)

        assert started == 1
        retry.assert_awaited_once()


class TestCalendar:
    def test_splits_upcoming_from_recent(self, session: Session):
        u = trusted(session)
        now = datetime.now()
        _ = requested(session, u, "NEXT", now + timedelta(days=3))
        _ = requested(session, u, "LATER", now + timedelta(days=40))
        _ = requested(session, u, "JUST", now - timedelta(days=2))
        _ = requested(session, u, "OLD", now - timedelta(days=200))

        calendar = build_calendar(session, u)

        assert [e.book.asin for e in calendar.upcoming] == ["NEXT", "LATER"]
        assert [e.book.asin for e in calendar.recent] == ["JUST"]
        assert calendar.upcoming[0].status == "Pre-ordered"
        assert calendar.recent[0].status == "Out now"

    def test_people_only_see_their_own(self, session: Session):
        u = trusted(session)
        other = User(username="other", password="x", group=GroupEnum.untrusted)
        session.add(other)
        session.commit()
        _ = requested(session, u, "MINE", datetime.now() + timedelta(days=3))

        assert build_calendar(session, other).upcoming == []
