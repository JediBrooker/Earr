"""The requests API: making a request, the permission model around automatic
downloading, and marking books as downloaded."""

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, select

from app.internal.models import (
    Audiobook,
    AudiobookRequest,
    GroupEnum,
    ManualBookRequest,
)
from tests.conftest import api_key_for, auth, series_book, standalone_book


@pytest.fixture
def admin(session: Session) -> dict[str, str]:
    return auth(api_key_for(session, GroupEnum.admin, "admin"))


@pytest.fixture
def trusted(session: Session) -> dict[str, str]:
    return auth(api_key_for(session, GroupEnum.trusted, "trusted"))


@pytest.fixture
def untrusted(session: Session) -> dict[str, str]:
    return auth(api_key_for(session, GroupEnum.untrusted, "untrusted"))


class TestListing:
    def test_requires_credentials(self, client: TestClient):
        assert client.get("/api/requests").status_code in (401, 403)

    def test_empty_to_begin_with(self, client: TestClient, untrusted: dict[str, str]):
        assert client.get("/api/requests", headers=untrusted).json() == []

    def test_an_admin_sees_everyone(
        self, client: TestClient, admin: dict[str, str], session: Session
    ):
        book = series_book()
        session.add(book)
        session.add(AudiobookRequest(asin=book.asin, user_username="untrusted-user"))
        session.add(
            __import__("app.internal.models", fromlist=["User"]).User(
                username="untrusted-user", password="x", group=GroupEnum.untrusted
            )
        )
        session.commit()

        body = client.get("/api/requests", headers=admin).json()

        assert len(body) == 1

    @pytest.mark.parametrize("filter_name", ["all", "downloaded", "not_downloaded"])
    def test_filters_are_accepted(
        self, client: TestClient, admin: dict[str, str], filter_name: str
    ):
        r = client.get(f"/api/requests?filter={filter_name}", headers=admin)
        assert r.status_code == 200

    def test_an_unknown_filter_is_refused(
        self, client: TestClient, admin: dict[str, str]
    ):
        assert client.get("/api/requests?filter=nonsense", headers=admin).status_code == 422


class TestMarkDownloaded:
    def test_admin_can_mark_a_book(
        self, client: TestClient, admin: dict[str, str], session: Session
    ):
        book = standalone_book()
        session.add(book)
        session.commit()

        r = client.patch(f"/api/requests/{book.asin}/downloaded", headers=admin)

        assert r.status_code == 204
        session.refresh(book)
        assert book.downloaded is True

    def test_a_trusted_user_cannot(
        self, client: TestClient, trusted: dict[str, str], session: Session
    ):
        book = standalone_book()
        session.add(book)
        session.commit()

        r = client.patch(f"/api/requests/{book.asin}/downloaded", headers=trusted)

        assert r.status_code in (401, 403)
        session.refresh(book)
        assert book.downloaded is False

    def test_unknown_book(self, client: TestClient, admin: dict[str, str]):
        assert client.patch(
            "/api/requests/NOSUCHASIN/downloaded", headers=admin
        ).status_code == 404


class TestManualRequests:
    def test_created_and_listed(
        self, client: TestClient, untrusted: dict[str, str], session: Session
    ):
        r = client.post(
            "/api/requests/manual",
            json={"title": "An Indie Book", "author": "Jane Doe"},
            headers=untrusted,
        )

        assert r.status_code == 201
        rows = session.exec(select(ManualBookRequest)).all()
        assert [row.title for row in rows] == ["An Indie Book"]

    def test_authors_are_split_on_commas(
        self, client: TestClient, untrusted: dict[str, str], session: Session
    ):
        _ = client.post(
            "/api/requests/manual",
            json={"title": "Co-authored", "author": "A,B", "narrator": "N1,N2"},
            headers=untrusted,
        )

        row = session.exec(select(ManualBookRequest)).one()
        assert row.authors == ["A", "B"]
        assert row.narrators == ["N1", "N2"]

    def test_title_is_required(self, client: TestClient, untrusted: dict[str, str]):
        assert client.post(
            "/api/requests/manual", json={"author": "Jane Doe"}, headers=untrusted
        ).status_code == 422

    def test_only_an_admin_can_mark_one_downloaded(
        self, client: TestClient, trusted: dict[str, str], session: Session
    ):
        row = ManualBookRequest(
            user_username="trusted", title="T", authors=["A"], narrators=[]
        )
        session.add(row)
        session.commit()

        r = client.patch(f"/api/requests/manual/{row.id}/downloaded", headers=trusted)

        assert r.status_code in (401, 403)


class TestDeletion:
    def test_a_user_can_withdraw_their_own_request(
        self, client: TestClient, session: Session
    ):
        key = api_key_for(session, GroupEnum.untrusted, "owner")
        book = series_book()
        session.add(book)
        session.add(AudiobookRequest(asin=book.asin, user_username="owner"))
        session.commit()

        r = client.delete(f"/api/requests/{book.asin}", headers=auth(key))

        assert r.status_code == 204
        assert session.exec(select(AudiobookRequest)).all() == []

    def test_a_user_cannot_withdraw_somebody_elses(
        self, client: TestClient, session: Session
    ):
        from app.internal.models import User

        other_key = api_key_for(session, GroupEnum.untrusted, "other")
        session.add(
            User(username="owner", password="x", group=GroupEnum.untrusted)
        )
        book = series_book()
        session.add(book)
        session.add(AudiobookRequest(asin=book.asin, user_username="owner"))
        session.commit()

        _ = client.delete(f"/api/requests/{book.asin}", headers=auth(other_key))

        assert len(session.exec(select(AudiobookRequest)).all()) == 1

    def test_an_admin_can_withdraw_anyones(self, client: TestClient, session: Session):
        from app.internal.models import User

        admin_key = api_key_for(session, GroupEnum.admin, "the-admin")
        session.add(User(username="owner", password="x", group=GroupEnum.untrusted))
        book = series_book()
        session.add(book)
        session.add(AudiobookRequest(asin=book.asin, user_username="owner"))
        session.commit()

        r = client.delete(f"/api/requests/{book.asin}", headers=auth(admin_key))

        assert r.status_code == 204
        assert session.exec(select(AudiobookRequest)).all() == []


class TestHealth:
    def test_health_needs_no_credentials(self, client: TestClient):
        assert client.get("/api/health").status_code == 200


class TestCreateRequest:
    """POST /api/requests/{asin} used to answer 500 for every call.

    The handler returns AudiobookWithRequests but the route declared
    response_model=Audiobook, so FastAPI validated the wrapper against the
    inner model and found six required fields missing. Because automatic
    downloading is scheduled as a background task, and background tasks only
    run after a successful response, a request made over the API never
    triggered a grab either.
    """

    def test_requesting_a_cached_book_succeeds(
        self, client: TestClient, session: Session
    ):
        key = api_key_for(session, GroupEnum.untrusted, "requester")
        book = series_book()
        session.add(book)
        session.commit()

        r = client.post(f"/api/requests/{book.asin}", headers=auth(key))

        assert r.status_code == 200, r.text[:200]

    def test_the_response_carries_the_book_and_its_requests(
        self, client: TestClient, session: Session
    ):
        key = api_key_for(session, GroupEnum.untrusted, "requester")
        book = series_book()
        session.add(book)
        session.commit()

        body = client.post(f"/api/requests/{book.asin}", headers=auth(key)).json()

        assert body["book"]["asin"] == book.asin
        assert body["book"]["title"] == book.title
        assert [r["user_username"] for r in body["requests"]] == ["requester"]

    def test_the_request_is_persisted(self, client: TestClient, session: Session):
        key = api_key_for(session, GroupEnum.untrusted, "requester")
        book = series_book()
        session.add(book)
        session.commit()

        _ = client.post(f"/api/requests/{book.asin}", headers=auth(key))

        assert session.exec(select(AudiobookRequest)).one().asin == book.asin

    def test_requesting_the_same_book_twice_conflicts(
        self, client: TestClient, session: Session
    ):
        key = api_key_for(session, GroupEnum.untrusted, "requester")
        book = series_book()
        session.add(book)
        session.commit()

        _ = client.post(f"/api/requests/{book.asin}", headers=auth(key))
        second = client.post(f"/api/requests/{book.asin}", headers=auth(key))

        assert second.status_code == 409
