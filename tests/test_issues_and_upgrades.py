"""Reported problems, quality upgrades, and the stats built from both."""

from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

import pytest
from aiohttp import ClientSession
from sqlmodel import Session

from app.internal import blocklist, issues, upgrades
from app.internal.library.config import library_config
from app.internal.library.organizer import remove_replaced
from app.internal.library.tracking import record_grab
from app.internal.library.watcher import import_single
from app.internal.models import (
    AudiobookRequest,
    DownloadStatusEnum,
    GroupEnum,
    IssueKindEnum,
    IssueStatusEnum,
    LibraryImport,
    LibraryImportStatusEnum,
    OrganizeModeEnum,
    TorrentSource,
    User,
)
from app.internal.ranking.quality import quality_config
from app.internal.stats import build as build_stats
from tests.conftest import make_download, series_book


def people(session: Session) -> tuple[User, User]:
    admin = User(username="boss", password="x", group=GroupEnum.admin)
    reader = User(username="reader", password="x", group=GroupEnum.trusted)
    session.add_all([admin, reader])
    session.commit()
    return admin, reader


def in_library(
    session: Session, release: str = "Sanderson - Way of Kings [MP3]", days_ago: int = 2
):
    book = series_book()
    book.downloaded = True
    book.download_status = DownloadStatusEnum.downloaded
    when = datetime.now() - timedelta(days=days_ago)
    entry = LibraryImport(
        asin_or_uuid=book.asin,
        book_title=book.title,
        release_title=release,
        status=LibraryImportStatusEnum.imported,
        guid="g-old",
        indexer="Books",
        created_at=when - timedelta(hours=3),
        updated_at=when,
    )
    session.add_all(
        [book, entry, AudiobookRequest(asin=book.asin, user_username="reader")]
    )
    session.commit()
    return book, entry


class TestIssues:
    def test_one_open_report_per_person_and_book(self, session: Session):
        _, reader = people(session)
        book, _ = in_library(session)
        _ = issues.report(
            session, reader, book.asin, IssueKindEnum.bad_audio, "  hiss  "
        )
        with pytest.raises(issues.IssueError):
            _ = issues.report(session, reader, book.asin, IssueKindEnum.other, None)
        [issue] = issues.list_issues(session, reader, IssueStatusEnum.open)
        assert issue.message == "hiss"
        assert issues.open_count(session) == 1

    def test_people_only_see_their_own(self, session: Session):
        admin, reader = people(session)
        book, _ = in_library(session)
        _ = issues.report(session, admin, book.asin, IssueKindEnum.other, None)
        assert issues.list_issues(session, reader, IssueStatusEnum.open) == []
        assert len(issues.list_issues(session, admin, IssueStatusEnum.open)) == 1

    async def test_resolving_notifies(self, session: Session):
        admin, reader = people(session)
        book, _ = in_library(session)
        issue = issues.report(
            session, reader, book.asin, IssueKindEnum.incomplete, None
        )
        with mock.patch.object(
            issues, "send_all_notifications", new=mock.AsyncMock()
        ) as sent:
            resolved = await issues.resolve(session, issue.id, admin, "Re-downloaded")
        assert resolved.status == IssueStatusEnum.resolved
        assert resolved.resolved_by == "boss"
        replacements = sent.await_args.args[2]
        assert replacements["issueResolution"] == "Re-downloaded"
        assert replacements["issueKind"] == "Missing parts"

    async def test_replacing_blocklists_the_release_and_searches_again(
        self, session: Session
    ):
        admin, reader = people(session)
        book, _ = in_library(session)
        issue = issues.report(
            session, reader, book.asin, IssueKindEnum.wrong_book, None
        )

        async def grabs_another(**kwargs: object) -> None:
            # an admin's explicit replace searches regardless of who requested it
            assert kwargs["start_auto_download"] is True
            book.downloaded = True
            session.add(book)
            session.commit()

        with (
            mock.patch("app.internal.query.query_sources", new=grabs_another),
            mock.patch.object(issues, "send_all_notifications", new=mock.AsyncMock()),
        ):
            async with ClientSession() as cs:
                message = await issues.replace_download(session, cs, issue.id, admin)

        assert message == "Blocklisted the release and grabbed another one."
        [blocked] = blocklist.entries(session)
        assert blocked.guid == "g-old"
        assert blocked.reason == "Reported: wrong book"
        session.refresh(issue)
        assert issue.status == IssueStatusEnum.resolved


class TestReplacingFiles:
    def test_a_regrab_of_a_book_in_the_library_replaces_it(self, session: Session):
        library_config.set_enabled(session, True)
        people(session)
        book, _ = in_library(session)
        entry = record_grab(session, book, book.asin, "Sanderson - Way of Kings [M4B]")
        assert entry is not None and entry.replaces

    def test_only_audio_the_new_download_didnt_bring_goes(self, tmp_path: Path):
        folder = tmp_path / "book"
        (folder / "CD1").mkdir(parents=True)
        for name in (
            "01.mp3",
            "02.mp3",
            "CD1/03.mp3",
            "cover.jpg",
            "metadata.json",
            "book.m4b",
        ):
            _ = (folder / name).write_text("x")

        removed = remove_replaced(folder, keep={Path("book.m4b")})

        assert removed == 3
        assert sorted(p.name for p in folder.rglob("*") if p.is_file()) == [
            "book.m4b",
            "cover.jpg",
            "metadata.json",
        ]

    def test_importing_an_upgrade_swaps_the_files(
        self, session: Session, library: tuple[Path, Path]
    ):
        downloads, root = library
        library_config.set_enabled(session, True)
        library_config.set_root_dir(session, str(root))
        library_config.set_download_dirs(session, str(downloads))
        library_config.set_mode(session, OrganizeModeEnum.copy)
        library_config.set_folder_template(session, "{author}/{title}")
        people(session)
        book, _ = in_library(session)
        target = root / "Brandon Sanderson" / "The Way of Kings"
        target.mkdir(parents=True)
        _ = (target / "01.mp3").write_text("old")
        _ = (target / "cover.jpg").write_text("art")
        source = make_download(
            downloads, "Way of Kings M4B", {"The Way of Kings.m4b": "new"}
        )
        entry = record_grab(session, book, book.asin, "Way of Kings M4B")
        assert entry is not None

        import_single(session, entry, source)

        assert entry.status == LibraryImportStatusEnum.imported
        names = sorted(p.name for p in target.iterdir())
        assert "01.mp3" not in names
        assert "The Way of Kings.m4b" in names and "cover.jpg" in names


def source(title: str, guid: str) -> TorrentSource:
    return TorrentSource(
        guid=guid,
        indexer_id=1,
        indexer="Books",
        title=title,
        size=1,
        publish_date=datetime(2024, 1, 1),
        info_url=None,
        indexer_flags=[],
        seeders=9,
        leechers=0,
    )


class TestUpgrades:
    def test_books_below_the_cutoff_are_upgradable(self, session: Session):
        people(session)
        quality_config.set_upgrade_cutoff(session, "m4b")
        book, _ = in_library(session)

        [found] = upgrades.find_upgradable(session)

        assert found.book.asin == book.asin
        assert found.current == "mp3"

    def test_good_enough_old_or_recently_checked_books_are_left_alone(
        self, session: Session
    ):
        people(session)
        quality_config.set_upgrade_days(session, 30)
        book, _ = in_library(session)

        book.quality_format = "m4b"
        session.add(book)
        session.commit()
        assert upgrades.find_upgradable(session) == []

        book.quality_format = "mp3"
        book.last_searched_at = datetime.now() - timedelta(hours=2)
        session.add(book)
        session.commit()
        assert upgrades.find_upgradable(session) == []

    def test_too_long_ago_is_left_alone(self, session: Session):
        people(session)
        quality_config.set_upgrade_days(session, 7)
        _ = in_library(session, days_ago=20)
        assert upgrades.find_upgradable(session) == []

    def test_only_a_better_valid_release_is_taken(self, session: Session):
        sources = [
            source("Way of Kings [FLAC]", "flac"),
            source("Way of Kings [M4B]", "m4b"),
            source("Way of Kings [MP3]", "mp3"),
        ]
        # the FLAC fails the quality bar, so the M4B is the upgrade
        best = upgrades.better_source(session, sources, [False, True, True], "mp3")
        assert best is not None and best.guid == "m4b"
        assert upgrades.better_source(session, sources[2:], [True], "mp3") is None


def test_stats(session: Session):
    _, reader = people(session)
    _ = in_library(session)
    failed = LibraryImport(
        asin_or_uuid="other",
        book_title="Other",
        release_title="Other [MP3]",
        status=LibraryImportStatusEnum.failed,
        indexer="Books",
    )
    session.add(failed)
    _ = issues.report(session, reader, "B1", IssueKindEnum.other, None)

    stats = build_stats(session)

    assert stats.total_requests == 1
    assert stats.books_in_library == 1
    assert stats.success_rate == 50
    assert stats.median_hours_to_library == 3.0
    assert stats.open_issues == 1
    [books] = stats.indexers
    assert (books.indexer, books.grabs, books.success_rate) == ("Books", 2, 50)
    by_name = {p.username: p for p in stats.people}
    assert by_name["reader"].requests == 1 and by_name["reader"].issues == 1
    assert stats.authors[0].label == "Brandon Sanderson"
    assert stats.months[-1].count == 1
