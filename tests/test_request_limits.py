"""How many books each person may request."""

from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session

from app.internal.models import (
    Audiobook,
    AudiobookRequest,
    GroupEnum,
    ManualBookRequest,
    User,
)
from app.internal.request_limits import (
    LimitPeriod,
    allowance,
    request_limit_config,
)
from tests.conftest import api_key_for, auth, standalone_book


@pytest.fixture(autouse=True)
def _fresh():
    request_limit_config._cache.clear()  # pyright: ignore[reportPrivateUsage]
    yield
    request_limit_config._cache.clear()  # pyright: ignore[reportPrivateUsage]


def user(session: Session, group: GroupEnum, **extra: object) -> User:
    u = User(username=f"{group.value}-u", password="x", group=group, **extra)  # pyright: ignore[reportArgumentType]
    session.add(u)
    session.commit()
    return u


def request(session: Session, u: User, asin: str, days_ago: float = 0, **extra: object):
    if session.get(Audiobook, asin) is None:
        session.add(standalone_book(asin))
    session.add(
        AudiobookRequest(
            asin=asin,
            user_username=u.username,
            created_at=datetime.now() - timedelta(days=days_ago),
            **extra,  # pyright: ignore[reportArgumentType]
        )
    )
    session.commit()


class TestAllowance:
    def test_unlimited_by_default(self, session: Session):
        assert allowance(session, user(session, GroupEnum.untrusted)) is None

    def test_counts_requests_in_the_period(self, session: Session):
        request_limit_config.set_group_limit(session, GroupEnum.untrusted, 3)
        u = user(session, GroupEnum.untrusted)
        request(session, u, "A", days_ago=1)
        request(session, u, "B", days_ago=8)  # last week, doesn't count
        session.add(ManualBookRequest(user_username=u.username, title="Indie"))
        session.commit()

        found = allowance(session, u)

        assert found is not None
        assert (found.used, found.remaining, found.reached) == (2, 1, False)

    def test_says_when_the_next_request_fits(self, session: Session):
        request_limit_config.set_group_limit(session, GroupEnum.untrusted, 1)
        request_limit_config.set_period(session, LimitPeriod.day)
        u = user(session, GroupEnum.untrusted)
        request(session, u, "A", days_ago=0.5)

        found = allowance(session, u)

        assert found is not None and found.reached
        assert found.next_free_at is not None
        assert (
            timedelta(hours=11)
            < found.next_free_at - datetime.now()
            < timedelta(hours=13)
        )
        assert "today" in found.message()

    def test_followed_requests_dont_count(self, session: Session):
        request_limit_config.set_group_limit(session, GroupEnum.untrusted, 1)
        u = user(session, GroupEnum.untrusted)
        request(session, u, "A", via_follow="Brandon Sanderson")

        found = allowance(session, u)
        assert found is not None and found.used == 0

    def test_a_personal_limit_beats_the_group(self, session: Session):
        request_limit_config.set_group_limit(session, GroupEnum.trusted, 1)
        assert (
            allowance(session, user(session, GroupEnum.trusted, request_limit=0))
            is None
        )

    def test_admins_are_never_limited(self, session: Session):
        request_limit_config.set_group_limit(session, GroupEnum.untrusted, 1)
        assert (
            allowance(session, user(session, GroupEnum.admin, request_limit=1)) is None
        )


class TestApi:
    def test_a_request_over_the_limit_is_refused(
        self, client: TestClient, session: Session
    ):
        token = api_key_for(session, GroupEnum.untrusted)
        request_limit_config.set_group_limit(session, GroupEnum.untrusted, 1)
        session.add_all([standalone_book("A"), standalone_book("B")])
        session.commit()

        first = client.post("/api/requests/A", headers=auth(token))
        second = client.post("/api/requests/B", headers=auth(token))

        assert first.status_code == 200, first.text
        assert second.status_code == 429
        assert (
            "You have used all 1 of your requests this week" in second.json()["detail"]
        )

    def test_manual_requests_are_limited_too(
        self, client: TestClient, session: Session
    ):
        token = api_key_for(session, GroupEnum.untrusted)
        request_limit_config.set_group_limit(session, GroupEnum.untrusted, 1)
        body = {"title": "Indie", "author": "Jane Doe"}

        assert (
            client.post(
                "/api/requests/manual", json=body, headers=auth(token)
            ).status_code
            == 201
        )
        assert (
            client.post(
                "/api/requests/manual", json=body, headers=auth(token)
            ).status_code
            == 429
        )
