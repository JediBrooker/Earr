"""Approving and declining requests that wait for an admin."""

from unittest import mock

from fastapi.testclient import TestClient
from sqlmodel import Session, select

from app.internal.approvals import approve, decline
from app.internal.auth.authentication import create_user
from app.internal.db_queries import get_wishlist_results
from app.internal.models import (
    Audiobook,
    AudiobookRequest,
    DownloadStatusEnum,
    EventEnum,
    GroupEnum,
    ManualBookRequest,
)
from app.internal.ranking.quality import quality_config
from app.internal.research import find_audiobooks_to_retry, find_manual_requests_to_retry
from tests.conftest import manual_request, series_book


def people(session: Session) -> None:
    session.add_all(
        [
            create_user("boss", "boss-password", GroupEnum.admin),
            create_user("pal", "pal-password", GroupEnum.trusted),
            create_user("guest", "guest-password", GroupEnum.untrusted),
            create_user("guest2", "guest2-password", GroupEnum.untrusted),
        ]
    )
    session.commit()


def requested_by(session: Session, *usernames: str, approved: bool = False) -> str:
    book = series_book()
    session.add(book)
    for username in usernames:
        session.add(
            AudiobookRequest(asin=book.asin, user_username=username, approved=approved)
        )
    session.commit()
    return book.asin


def awaiting(session: Session) -> bool:
    [result] = get_wishlist_results(session)
    return result.awaiting_approval


class TestAwaitingApproval:
    def test_only_untrusted_requesters(self, session: Session):
        people(session)
        requested_by(session, "guest", "guest2")

        assert awaiting(session)

    def test_a_trusted_requester_is_enough(self, session: Session):
        people(session)
        requested_by(session, "guest", "pal")

        assert not awaiting(session)

    def test_an_approved_request_is_no_longer_waiting(self, session: Session):
        people(session)
        requested_by(session, "guest", approved=True)

        assert not awaiting(session)

    def test_a_download_someone_already_tried_is_not_waiting(self, session: Session):
        people(session)
        asin = requested_by(session, "guest")
        book = session.get(Audiobook, asin)
        assert book is not None
        book.download_status = DownloadStatusEnum.failed
        session.add(book)
        session.commit()

        assert not awaiting(session)

    def test_manual_requests(self, session: Session):
        people(session)
        untrusted = manual_request(user_username="guest")
        trusted = manual_request(user_username="pal")
        session.add_all([untrusted, trusted])
        session.commit()

        assert untrusted.awaiting_approval
        assert not trusted.awaiting_approval


class TestApprove:
    def test_marks_every_request_for_the_book(self, session: Session):
        people(session)
        asin = requested_by(session, "guest", "guest2")

        _ = approve(session, asin)

        assert all(r.approved for r in session.exec(select(AudiobookRequest)).all())
        assert not awaiting(session)

    def test_approved_requests_are_searched_for_again_later(self, session: Session):
        people(session)
        asin = requested_by(session, "guest")
        assert find_audiobooks_to_retry(session, 0, 5) == []

        _ = approve(session, asin)

        assert [b.asin for b in find_audiobooks_to_retry(session, 0, 5)] == [asin]

    def test_approved_manual_requests_are_searched_for_again_later(
        self, session: Session
    ):
        people(session)
        request = manual_request(user_username="guest")
        session.add(request)
        session.commit()
        assert find_manual_requests_to_retry(session, 0, 5) == []

        _ = approve(session, str(request.id))

        assert len(find_manual_requests_to_retry(session, 0, 5)) == 1


class TestDecline:
    async def test_tells_the_requesters_why_then_removes_the_request(
        self, session: Session
    ):
        people(session)
        asin = requested_by(session, "guest", "guest2")
        still_there: list[int] = []

        async def sent(event: EventEnum, book_asin: str, replacements: dict[str, str]):
            # the notification has to go out while the requesters can still be found
            still_there.append(len(session.exec(select(AudiobookRequest)).all()))
            assert event == EventEnum.on_request_declined
            assert book_asin == asin
            assert replacements == {"declineReason": "Not on any indexer"}

        with mock.patch("app.internal.approvals.send_all_notifications", new=sent):
            title = await decline(session, asin, "  Not on any indexer ")

        assert title == "The Way of Kings"
        assert still_there == [2]
        assert session.exec(select(AudiobookRequest)).all() == []

    async def test_a_broken_webhook_does_not_stop_it(self, session: Session):
        people(session)
        asin = requested_by(session, "guest")

        with mock.patch(
            "app.internal.approvals.send_all_notifications",
            new=mock.AsyncMock(side_effect=RuntimeError("webhook down")),
        ):
            _ = await decline(session, asin, "")

        assert session.exec(select(AudiobookRequest)).all() == []

    async def test_a_manual_request_without_a_reason(self, session: Session):
        people(session)
        request = manual_request(user_username="guest")
        session.add(request)
        session.commit()
        calls = mock.AsyncMock()

        with mock.patch("app.internal.approvals.send_all_manual_notifications", new=calls):
            _ = await decline(session, str(request.id), "")

        assert calls.await_args is not None
        assert calls.await_args.args[2] == {"declineReason": "No reason given"}
        assert session.exec(select(ManualBookRequest)).all() == []


class TestWishlistPage:
    def test_admins_see_the_badge_and_the_approve_button(
        self, client: TestClient, session: Session
    ):
        people(session)
        requested_by(session, "guest")

        page = client.get("/wishlist", auth=("boss", "boss-password")).text

        assert "awaiting approval" in page
        assert 'title="Approve"' in page
        assert 'title="Decline"' in page

    def test_approving_starts_the_download_when_that_is_automatic(
        self, client: TestClient, session: Session
    ):
        people(session)
        asin = requested_by(session, "guest")
        quality_config.set_auto_download(session, True)
        start = mock.AsyncMock()

        with (
            mock.patch("app.routers.api.requests.background_start_query", new=start),
            mock.patch("app.internal.approvals.send_all_notifications", new=mock.AsyncMock()),
        ):
            response = client.post(
                f"/wishlist/hx-approve/{asin}", auth=("boss", "boss-password")
            )

        assert response.status_code == 200, response.text
        assert "awaiting approval" not in response.text
        start.assert_awaited_once_with(asin_or_uuid=asin, auto_download=True)

    def test_declining_with_a_reason(self, client: TestClient, session: Session):
        people(session)
        asin = requested_by(session, "guest")

        with mock.patch("app.internal.approvals.send_all_notifications", new=mock.AsyncMock()):
            response = client.post(
                f"/wishlist/hx-decline/{asin}",
                data={"reason": "Already in the library"},
                auth=("boss", "boss-password"),
            )

        assert response.status_code == 200, response.text
        assert session.exec(select(AudiobookRequest)).all() == []

    def test_only_admins_decide(self, client: TestClient, session: Session):
        people(session)
        asin = requested_by(session, "guest")

        approve_attempt = client.post(
            f"/wishlist/hx-approve/{asin}", auth=("pal", "pal-password")
        )
        decline_attempt = client.post(
            f"/api/requests/{asin}/decline", json={}, auth=("guest", "guest-password")
        )

        assert approve_attempt.status_code == 403
        assert decline_attempt.status_code in (401, 403)
        assert len(session.exec(select(AudiobookRequest)).all()) == 1

    def test_the_api_can_approve(self, client: TestClient, session: Session):
        from tests.conftest import api_key_for, auth

        people(session)
        asin = requested_by(session, "guest")
        token = api_key_for(session, GroupEnum.admin, "robot")

        with mock.patch("app.internal.approvals.send_all_notifications", new=mock.AsyncMock()):
            response = client.post(f"/api/requests/{asin}/approve", headers=auth(token))
        missing = client.post("/api/requests/B0MISSING/approve", headers=auth(token))

        assert response.status_code == 204
        assert missing.status_code == 404
