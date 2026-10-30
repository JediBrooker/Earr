"""The System page's checks and the sidebar's health light."""

import asyncio
from datetime import datetime, timedelta
from pathlib import Path
from typing import Iterator
from unittest import mock

import pytest
from aiohttp import ClientSession
from sqlmodel import Session
from typing_extensions import override

from app.internal import health
from app.internal.download_clients.abstract import DownloadClient, DownloadInfo
from app.internal.health import (
    CheckLevel,
    HealthCheck,
    HealthReport,
    forget_report,
    get_report,
    run_checks,
)
from app.internal.library.config import library_config
from app.internal.models import (
    DownloadStatusEnum,
    LibraryImport,
    OrganizeModeEnum,
)
from tests.conftest import standalone_book


@pytest.fixture(autouse=True)
def _fresh_report() -> Iterator[None]:
    forget_report()
    yield
    forget_report()


class ReachableClient(DownloadClient):
    name: str = "qBittorrent"

    def __init__(self, ok: bool, message: str):
        self.result: tuple[bool, str] = (ok, message)

    @override
    async def test(self, client_session: ClientSession) -> tuple[bool, str]:
        return self.result

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
        return None


def organizing(session: Session, root: Path, downloads: Path, mode: OrganizeModeEnum):
    library_config.set_enabled(session, True)
    library_config.set_root_dir(session, str(root))
    library_config.set_download_dirs(session, str(downloads))
    library_config.set_mode(session, mode)


class TestLibraryFolders:
    def test_off_while_organizing_is_off(self, session: Session):
        check = health._library_folders(session)  # pyright: ignore[reportPrivateUsage]

        assert check.level == CheckLevel.off

    def test_a_working_setup_is_ok(self, session: Session, library: tuple[Path, Path]):
        downloads, root = library
        organizing(session, root, downloads, OrganizeModeEnum.hardlink)

        check = health._library_folders(session)  # pyright: ignore[reportPrivateUsage]

        assert check.level == CheckLevel.ok
        assert "hardlink" in check.message

    def test_a_missing_library_folder_is_an_error(
        self, session: Session, tmp_path: Path
    ):
        organizing(session, tmp_path / "gone", tmp_path, OrganizeModeEnum.copy)

        check = health._library_folders(session)  # pyright: ignore[reportPrivateUsage]

        assert check.level == CheckLevel.error
        assert "doesn't exist" in check.message
        assert check.fix_path == "/settings/library"

    def test_hardlinks_across_filesystems_are_caught_before_they_fail(
        self, session: Session, library: tuple[Path, Path]
    ):
        downloads, root = library
        organizing(session, root, downloads, OrganizeModeEnum.hardlink)

        with mock.patch.object(
            health, "_device", side_effect=lambda path: 1 if path == root else 2
        ):
            check = health._library_folders(session)  # pyright: ignore[reportPrivateUsage]

        assert check.level == CheckLevel.error
        assert "different filesystems" in check.message

    def test_copy_mode_does_not_care_about_filesystems(
        self, session: Session, library: tuple[Path, Path]
    ):
        downloads, root = library
        organizing(session, root, downloads, OrganizeModeEnum.copy)

        with mock.patch.object(
            health, "_device", side_effect=lambda path: 1 if path == root else 2
        ):
            check = health._library_folders(session)  # pyright: ignore[reportPrivateUsage]

        assert check.level == CheckLevel.ok


class TestDownloads:
    def test_nothing_stuck(self, session: Session):
        check = health._downloads(session)  # pyright: ignore[reportPrivateUsage]

        assert check.level == CheckLevel.ok

    def test_a_grab_waiting_for_days_is_flagged(self, session: Session):
        session.add(
            LibraryImport(
                asin_or_uuid="B1",
                book_title="The Way of Kings",
                release_title="x",
                created_at=datetime.now() - timedelta(days=3),
            )
        )
        session.commit()

        check = health._downloads(session)  # pyright: ignore[reportPrivateUsage]

        assert check.level == CheckLevel.warning
        assert check.fix_path == "/activity"

    def test_a_fresh_grab_is_not_stuck(self, session: Session):
        session.add(
            LibraryImport(asin_or_uuid="B1", book_title="t", release_title="x")
        )
        session.commit()

        check = health._downloads(session)  # pyright: ignore[reportPrivateUsage]

        assert check.level == CheckLevel.ok

    def test_failed_requests_are_flagged(self, session: Session):
        session.add(standalone_book(download_status=DownloadStatusEnum.failed))
        session.commit()

        check = health._downloads(session)  # pyright: ignore[reportPrivateUsage]

        assert check.level == CheckLevel.warning
        assert "1 request failed" in check.message


class TestReport:
    async def test_with_nothing_set_up(self, session: Session):
        async with ClientSession() as cs:
            report = await run_checks(session, cs)

        levels = {check.name: check.level for check in report.checks}
        assert levels["Prowlarr"] == CheckLevel.warning
        assert levels["qBittorrent"] == CheckLevel.off
        assert levels["Audiobookshelf"] == CheckLevel.off
        assert report.level == CheckLevel.warning

    async def test_an_unreachable_client_is_an_error_with_a_way_to_fix_it(self):
        async with ClientSession() as cs:
            check = await health._download_client(  # pyright: ignore[reportPrivateUsage]
                "qBittorrent", ReachableClient(False, "Login rejected."), cs
            )

        assert check.level == CheckLevel.error
        assert check.message == "Login rejected."
        assert check.fix_path == "/settings/library"

    async def test_a_hung_service_is_reported_instead_of_waited_for(self):
        async def never_answers() -> HealthCheck:
            await asyncio.sleep(10)
            raise AssertionError("should have timed out")

        with mock.patch.object(health, "CHECK_TIMEOUT", 0.01):
            check = await health._within_time(  # pyright: ignore[reportPrivateUsage]
                "Prowlarr", "/settings/prowlarr", never_answers()
            )

        assert check.level == CheckLevel.error
        assert "No answer" in check.message

    async def test_results_are_reused_for_a_minute(self, session: Session):
        report = HealthReport(checks=[], checked_at=datetime.now())
        checks = mock.AsyncMock(return_value=report)

        with mock.patch.object(health, "run_checks", checks):
            async with ClientSession() as cs:
                _ = await get_report(session, cs)
                _ = await get_report(session, cs)
                assert checks.await_count == 1
                _ = await get_report(session, cs, refresh=True)

        assert checks.await_count == 2

    def test_the_worst_check_decides_the_light(self):
        def check(level: CheckLevel) -> HealthCheck:
            return HealthCheck(name=level.value, level=level, message="")

        mixed = HealthReport(
            checks=[check(CheckLevel.ok), check(CheckLevel.off), check(CheckLevel.warning)],
            checked_at=datetime.now(),
        )
        unused = HealthReport(
            checks=[check(CheckLevel.ok), check(CheckLevel.off)], checked_at=datetime.now()
        )

        assert mixed.level == CheckLevel.warning
        assert [c.name for c in mixed.problems] == ["warning"]
        assert unused.level == CheckLevel.ok, "a service that isn't set up isn't a problem"
