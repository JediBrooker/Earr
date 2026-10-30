"""Every new page renders, for the people allowed to see it."""

from datetime import datetime, timedelta
from unittest import mock

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session

from app.internal import blocklist
from app.internal.audible.catalog import BookDetails
from app.internal.audible.types import AudibleContributor, AudibleSeries
from app.internal.auth.authentication import create_user
from app.internal.models import (
    AudiobookRequest,
    Follow,
    FollowKindEnum,
    GroupEnum,
)
from app.util.log import logger, recent
from tests.conftest import series_book

ADMIN = ("boss", "boss-password")
READER = ("reader", "reader-password")


@pytest.fixture
def people(session: Session) -> None:
    session.add_all(
        [
            create_user(*ADMIN, group=GroupEnum.admin),
            create_user(*READER, group=GroupEnum.untrusted),
        ]
    )
    session.commit()


@pytest.mark.usefixtures("people")
class TestPages:
    @pytest.mark.parametrize(
        "path",
        [
            "/following",
            "/calendar",
            "/system/backups",
            "/system/logs",
            "/activity/blocklist",
            "/settings/download",
            "/settings/users",
            "/settings/security",
            "/settings/library",
        ],
    )
    def test_renders_for_an_admin(self, client: TestClient, path: str):
        response = client.get(path, auth=ADMIN)
        assert response.status_code == 200, response.text[:500]

    @pytest.mark.parametrize(
        "path", ["/system/backups", "/system/logs", "/activity/blocklist"]
    )
    def test_admin_pages_are_refused_to_others(self, client: TestClient, path: str):
        assert client.get(path, auth=READER).status_code == 403

    def test_following_lists_the_follows(self, client: TestClient, session: Session):
        session.add(
            Follow(
                user_username="reader",
                kind=FollowKindEnum.series,
                audible_id="S1",
                name="The <Stormlight> Archive",
            )
        )
        session.commit()

        page = client.get("/following", auth=READER).text

        # names from outside are escaped, never markup
        assert "The &lt;Stormlight&gt; Archive" in page
        assert "<Stormlight>" not in page

    def test_the_calendar_shows_a_preorder(self, client: TestClient, session: Session):
        book = series_book("SOON", release_date=datetime.now() + timedelta(days=9))
        session.add(book)
        session.add(AudiobookRequest(asin="SOON", user_username="reader"))
        session.commit()

        page = client.get("/calendar", auth=READER).text

        assert "The Way of Kings" in page
        assert "Pre-ordered" in page

    def test_the_blocklist_lists_entries(self, client: TestClient, session: Session):
        _ = blocklist.add(session, title="Bad Release [M4B]", reason="files missing")
        page = client.get("/activity/blocklist", auth=ADMIN).text
        assert "Bad Release [M4B]" in page
        assert "Allow again" in page

    def test_logs_show_recent_lines(self, client: TestClient):
        logger.warning("Something worth seeing", detail="<b>here</b>")
        page = client.get("/system/logs?level=warning", auth=ADMIN).text
        assert "Something worth seeing" in page
        assert "&lt;b&gt;here&lt;/b&gt;" in page
        [line] = [r for r in recent if r.event == "Something worth seeing"][-1:]
        assert line.level == "warning"
        assert line.fields["detail"] == "<b>here</b>"

    def test_the_book_page(self, client: TestClient, session: Session):
        book = series_book("B1", language="english")
        details = BookDetails(
            book=book,
            description="First paragraph.\n\nSecond <b>one</b>.",
            authors=[AudibleContributor(name="Brandon Sanderson", asin="A1")],
            series=[
                AudibleSeries(asin="S1", title="The Stormlight Archive", sequence="1")
            ],
            format_type="unabridged",
        )
        with (
            mock.patch(
                "app.internal.book_page.get_book_details",
                new=mock.AsyncMock(return_value=details),
            ),
            mock.patch(
                "app.internal.book_page.get_series_products",
                new=mock.AsyncMock(return_value=[]),
            ),
            mock.patch(
                "app.internal.book_page.get_author_products",
                new=mock.AsyncMock(return_value=[]),
            ),
        ):
            response = client.get("/book/B1", auth=READER)

        assert response.status_code == 200, response.text[:500]
        page = response.text
        assert "Book 1 of The Stormlight Archive" in page
        assert "Follow author" in page and "Follow series" in page
        assert "Second &lt;b&gt;one&lt;/b&gt;." in page
        assert "English" in page

    def test_an_unknown_book_is_not_found(self, client: TestClient):
        with mock.patch(
            "app.internal.book_page.get_book_details",
            new=mock.AsyncMock(return_value=None),
        ):
            assert client.get("/book/NOPE", auth=READER).status_code == 404
