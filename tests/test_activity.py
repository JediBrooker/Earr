"""The Activity page: downloads on their way to the library, and recent history."""

from typing import Iterator
from unittest import mock

import pytest
from aiohttp import ClientSession
from fastapi.testclient import TestClient
from sqlmodel import Session
from typing_extensions import override

from app.internal.activity import history, pending_count, queue
from app.internal.auth.authentication import create_user
from app.internal.download_clients.abstract import (
    DownloadClient,
    DownloadInfo,
    DownloadState,
)
from app.internal.download_clients.config import download_client_config
from app.internal.health import forget_report
from app.internal.library.config import library_config
from app.internal.models import (
    Audiobook,
    AudiobookRequest,
    GroupEnum,
    LibraryImport,
    LibraryImportStatusEnum,
    User,
)
from tests.conftest import series_book, standalone_book

RELEASE = "Brandon Sanderson - The Way of Kings [M4B]"


class FakeClient(DownloadClient):
    name: str = "qBittorrent"

    def __init__(self, info: DownloadInfo | None):
        self.info: DownloadInfo | None = info
        self.lookups: list[tuple[str | None, str | None]] = []

    @override
    async def test(self, client_session: ClientSession) -> tuple[bool, str]:
        return True, "ok"

    @override
    async def list_categories(self, client_session: ClientSession) -> list[str]:
        return []

    @override
    async def set_category(
        self,
        client_session: ClientSession,
        category: str,
        *,
        client_id: str | None = None,
        name: str | None = None,
    ) -> bool:
        return True

    @override
    async def find(
        self,
        client_session: ClientSession,
        *,
        client_id: str | None = None,
        name: str | None = None,
    ) -> DownloadInfo | None:
        self.lookups.append((client_id, name))
        return self.info


@pytest.fixture(autouse=True)
def _fresh_report() -> Iterator[None]:
    forget_report()
    yield
    forget_report()


def with_client(info: DownloadInfo | None) -> tuple[FakeClient, object]:
    fake = FakeClient(info)
    patches = mock.patch.multiple(
        download_client_config,
        build_qbit=mock.Mock(return_value=fake),
        build_sab=mock.Mock(return_value=None),
    )
    return fake, patches


def users(session: Session) -> tuple[User, User]:
    admin = create_user("boss", "boss-password", GroupEnum.admin)
    reader = create_user("reader", "reader-password", GroupEnum.untrusted)
    session.add_all([admin, reader])
    session.commit()
    return admin, reader


def grab(session: Session, book: Audiobook, requester: str, **row: object) -> None:
    session.add(book)
    session.add(AudiobookRequest(asin=book.asin, user_username=requester))
    session.add(
        LibraryImport(
            asin_or_uuid=book.asin,
            book_title=book.title,
            release_title=RELEASE,
            client_id="abc123",
            protocol="torrent",
            **row,  # pyright: ignore[reportArgumentType]
        )
    )
    session.commit()


class TestQueue:
    async def test_progress_comes_from_the_download_client(self, session: Session):
        admin, reader = users(session)
        grab(session, series_book(), reader.username)
        fake, patches = with_client(
            DownloadInfo(
                state=DownloadState.downloading,
                name=RELEASE,
                progress=0.42,
                size=900 * 1024 * 1024,
                speed=3 * 1024 * 1024,
                eta=600,
                status="downloading",
            )
        )

        with patches:  # pyright: ignore[reportGeneralTypeIssues]
            async with ClientSession() as cs:
                items = await queue(session, cs, admin)

        assert fake.lookups == [("abc123", RELEASE)]
        [item] = items
        assert item.state == "downloading"
        assert item.client == "qBittorrent"
        assert item.percent == 42
        assert item.size_label == "900.0 MB"
        assert item.speed_label == "3.0 MB/s"
        assert item.eta_label == "10m"
        assert item.requested_by == ["reader"]

    @pytest.mark.parametrize(
        "info, state, message",
        [
            (
                DownloadInfo(state=DownloadState.completed, name=RELEASE, progress=1),
                "importing",
                "next scan",
            ),
            (
                DownloadInfo(state=DownloadState.failed, name=RELEASE, error="missingFiles"),
                "failed",
                "missingFiles",
            ),
            (None, "waiting", "Not in qBittorrent yet"),
        ],
    )
    async def test_states(
        self, session: Session, info: DownloadInfo | None, state: str, message: str
    ):
        admin, reader = users(session)
        grab(session, series_book(), reader.username)
        _, patches = with_client(info)

        with patches:  # pyright: ignore[reportGeneralTypeIssues]
            async with ClientSession() as cs:
                [item] = await queue(session, cs, admin)

        assert item.state == state
        assert message in item.message

    async def test_without_a_download_client_it_waits_for_the_files(
        self, session: Session
    ):
        admin, reader = users(session)
        grab(session, series_book(), reader.username)

        async with ClientSession() as cs:
            [item] = await queue(session, cs, admin)

        assert item.state == "waiting"
        assert "completed downloads folder" in item.message

    async def test_people_only_see_the_books_they_asked_for(self, session: Session):
        admin, reader = users(session)
        grab(session, series_book("B1"), reader.username)
        grab(session, standalone_book("B2"), admin.username)

        async with ClientSession() as cs:
            theirs = await queue(session, cs, reader)
            everything = await queue(session, cs, admin)

        assert [i.title for i in theirs] == ["The Way of Kings"]
        assert len(everything) == 2
        assert pending_count(session, reader) == 1
        assert pending_count(session, admin) == 2


class TestHistory:
    def test_finished_downloads_newest_first(self, session: Session):
        admin, reader = users(session)
        grab(
            session,
            series_book("B1"),
            reader.username,
            status=LibraryImportStatusEnum.imported,
            target_path="/library/Brandon Sanderson/The Way of Kings",
        )
        grab(
            session,
            standalone_book("B2"),
            reader.username,
            status=LibraryImportStatusEnum.failed,
            error="Download contains no files",
        )
        grab(session, standalone_book("B3", title="Still going"), reader.username)

        items = history(session, admin)

        assert {i.title for i in items} == {"The Way of Kings", "The Martian"}
        assert all(i.status != LibraryImportStatusEnum.pending for i in items)


class TestPages:
    def test_the_queue_renders_live_rows(self, client: TestClient, session: Session):
        _, reader = users(session)
        library_config.set_enabled(session, True)
        grab(session, series_book(), reader.username)
        _, patches = with_client(
            DownloadInfo(state=DownloadState.downloading, name=RELEASE, progress=0.5)
        )

        with patches:  # pyright: ignore[reportGeneralTypeIssues]
            response = client.get("/activity/hx-queue", auth=("boss", "boss-password"))

        assert response.status_code == 200, response.text
        assert "The Way of Kings" in response.text
        assert "50%" in response.text
        assert 'hx-trigger="every 5s"' in response.text

    def test_with_organizing_off_it_says_why_nothing_is_tracked(
        self, client: TestClient, session: Session
    ):
        users(session)

        response = client.get("/activity/hx-queue", auth=("boss", "boss-password"))

        assert "library organizing is on" in response.text
        assert "Nothing downloading" in response.text

    def test_the_sidebar_counts_downloads_and_shows_the_health_light(
        self, client: TestClient, session: Session
    ):
        _, reader = users(session)
        grab(session, series_book(), reader.username)

        as_admin = client.get("/system/hx-sidebar", auth=("boss", "boss-password"))
        as_reader = client.get("/system/hx-sidebar", auth=("reader", "reader-password"))

        assert as_admin.status_code == 200, as_admin.text
        assert 'id="nav-activity-count"' in as_admin.text
        assert ">1</span>" in as_admin.text
        assert 'id="nav-system-dot"' in as_admin.text
        assert 'id="nav-system-dot"' not in as_reader.text, "only admins can fix it"

    def test_the_system_page_lists_every_check(
        self, client: TestClient, session: Session
    ):
        users(session)

        response = client.get("/system/status", auth=("boss", "boss-password"))

        assert response.status_code == 200, response.text
        for name in ("Prowlarr", "qBittorrent", "SABnzbd", "Audiobookshelf", "Library folders"):
            assert name in response.text

    def test_only_admins_see_the_system_page(self, client: TestClient, session: Session):
        users(session)

        response = client.get("/system/status", auth=("reader", "reader-password"))

        assert response.status_code == 403
