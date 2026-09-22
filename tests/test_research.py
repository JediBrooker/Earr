"""Re-searching outstanding requests.

This grabs without anyone watching, so the tests are mostly about what it
refuses to do: requests from untrusted users, books already downloaded, and
books that have used up their attempts.
"""

from datetime import datetime, timedelta
from unittest import mock

import pytest
from sqlmodel import Session

from app.internal import research
from app.internal.models import (
    Audiobook,
    AudiobookRequest,
    GroupEnum,
    ManualBookRequest,
    User,
)
from app.internal.ranking.quality import quality_config
from tests.conftest import series_book, standalone_book

INTERVAL = 3600
MAX_ATTEMPTS = 5


def add_user(session: Session, username: str, group: GroupEnum) -> User:
    user = User(username=username, password="x", group=group)
    session.add(user)
    session.commit()
    return user


def requested_book(
    session: Session, username: str, group: GroupEnum, asin: str = "R1"
) -> Audiobook:
    add_user(session, username, group)
    book = standalone_book(asin=asin)
    session.add(book)
    session.add(AudiobookRequest(asin=asin, user_username=username))
    session.commit()
    return book


def eligible(session: Session) -> list[Audiobook]:
    return research.find_audiobooks_to_retry(session, INTERVAL, MAX_ATTEMPTS)


class TestEligibility:
    def test_a_trusted_users_outstanding_request_is_retried(self, session: Session):
        book = requested_book(session, "trusty", GroupEnum.trusted)
        assert [b.asin for b in eligible(session)] == [book.asin]

    def test_an_admins_request_is_retried(self, session: Session):
        book = requested_book(session, "boss", GroupEnum.admin)
        assert [b.asin for b in eligible(session)] == [book.asin]

    def test_an_untrusted_users_request_is_not(self, session: Session):
        """Untrusted requests need approval, so they must not grab by themselves."""
        _ = requested_book(session, "newbie", GroupEnum.untrusted)
        assert eligible(session) == []

    def test_a_book_nobody_requested_is_not(self, session: Session):
        session.add(series_book())
        session.commit()
        assert eligible(session) == []

    def test_an_already_downloaded_book_is_not(self, session: Session):
        book = requested_book(session, "trusty", GroupEnum.trusted)
        book.downloaded = True
        session.add(book)
        session.commit()
        assert eligible(session) == []

    def test_a_book_that_used_up_its_attempts_is_not(self, session: Session):
        book = requested_book(session, "trusty", GroupEnum.trusted)
        book.search_attempts = MAX_ATTEMPTS
        session.add(book)
        session.commit()
        assert eligible(session) == []

    def test_a_recently_searched_book_waits(self, session: Session):
        book = requested_book(session, "trusty", GroupEnum.trusted)
        book.last_searched_at = datetime.now()
        session.add(book)
        session.commit()
        assert eligible(session) == []

    def test_it_becomes_due_once_the_interval_has_passed(self, session: Session):
        book = requested_book(session, "trusty", GroupEnum.trusted)
        book.last_searched_at = datetime.now() - timedelta(seconds=INTERVAL + 60)
        session.add(book)
        session.commit()
        assert [b.asin for b in eligible(session)] == [book.asin]

    def test_least_tried_books_go_first(self, session: Session):
        add_user(session, "trusty", GroupEnum.trusted)
        for asin, attempts in [("A", 3), ("B", 0), ("C", 1)]:
            session.add(standalone_book(asin=asin, search_attempts=attempts))
            session.add(AudiobookRequest(asin=asin, user_username="trusty"))
        session.commit()

        assert [b.asin for b in eligible(session)] == ["B", "C", "A"]

    def test_a_batch_is_capped(self, session: Session, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setattr(research, "BATCH_SIZE", 2)
        add_user(session, "trusty", GroupEnum.trusted)
        for i in range(5):
            session.add(standalone_book(asin=f"X{i}"))
            session.add(AudiobookRequest(asin=f"X{i}", user_username="trusty"))
        session.commit()

        assert len(eligible(session)) == 2


class TestManualRequests:
    def test_a_trusted_users_manual_request_is_retried(self, session: Session):
        add_user(session, "trusty", GroupEnum.trusted)
        session.add(
            ManualBookRequest(
                user_username="trusty", title="Indie", authors=["A"], narrators=[]
            )
        )
        session.commit()

        assert len(research.find_manual_requests_to_retry(session, INTERVAL, MAX_ATTEMPTS)) == 1

    def test_an_untrusted_users_manual_request_is_not(self, session: Session):
        add_user(session, "newbie", GroupEnum.untrusted)
        session.add(
            ManualBookRequest(
                user_username="newbie", title="Indie", authors=["A"], narrators=[]
            )
        )
        session.commit()

        assert research.find_manual_requests_to_retry(session, INTERVAL, MAX_ATTEMPTS) == []


class TestAttemptCounting:
    def test_an_attempt_is_recorded(self, session: Session):
        book = requested_book(session, "trusty", GroupEnum.trusted)

        research.record_attempt(session, book)

        assert book.search_attempts == 1
        assert book.last_searched_at is not None

    async def test_a_search_that_throws_still_burns_an_attempt(self, session: Session):
        """Otherwise a book that always errors would be retried forever."""
        book = requested_book(session, "trusty", GroupEnum.trusted)

        with mock.patch.object(
            research, "query_sources", side_effect=RuntimeError("prowlarr is down")
        ):
            started = await research.retry_one(session, mock.MagicMock(), book, book.asin)

        assert started is False
        assert book.search_attempts == 1

    async def test_attempts_accumulate_until_the_book_is_left_alone(
        self, session: Session
    ):
        book = requested_book(session, "trusty", GroupEnum.trusted)

        for _ in range(MAX_ATTEMPTS):
            with mock.patch.object(
                research, "query_sources", side_effect=RuntimeError("nope")
            ):
                _ = await research.retry_one(session, mock.MagicMock(), book, book.asin)
            book.last_searched_at = None
            session.add(book)
            session.commit()

        assert book.search_attempts == MAX_ATTEMPTS
        assert eligible(session) == []


class TestRunGating:
    async def test_nothing_runs_while_auto_download_is_off(self, session: Session):
        quality_config.set_auto_download(session, False)
        quality_config.set_research_enabled(session, True)
        _ = requested_book(session, "trusty", GroupEnum.trusted)

        assert await research.run_research(session, mock.MagicMock()) == 0

    async def test_nothing_runs_while_research_is_off(self, session: Session):
        quality_config.set_auto_download(session, True)
        quality_config.set_research_enabled(session, False)
        _ = requested_book(session, "trusty", GroupEnum.trusted)

        assert await research.run_research(session, mock.MagicMock()) == 0

    async def test_both_switches_on_searches_the_book(self, session: Session):
        quality_config.set_auto_download(session, True)
        quality_config.set_research_enabled(session, True)
        book = requested_book(session, "trusty", GroupEnum.trusted)

        searched: list[str] = []

        async def fake_query(**kw: object):
            searched.append(str(kw["asin_or_uuid"]))
            assert kw["force_refresh"] is True, "a cached result is what failed before"
            assert kw["start_auto_download"] is True
            return mock.MagicMock(error_message=None)

        with mock.patch.object(research, "query_sources", fake_query):
            _ = await research.run_research(session, mock.MagicMock())

        assert searched == [book.asin]
        session.refresh(book)
        assert book.search_attempts == 1
