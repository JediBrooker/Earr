"""Matching finished downloads to grabs, and the status transitions that follow."""

from datetime import datetime, timedelta
from pathlib import Path

import pytest
from sqlmodel import Session, select

from app.internal.library import watcher
from app.internal.library.watcher import find_match as find_match_with_title
from app.internal.library.config import library_config
from app.internal.library.tracking import record_grab
from app.internal.models import (
    Audiobook,
    DownloadStatusEnum,
    LibraryImport,
    LibraryImportStatusEnum,
    OrganizeModeEnum,
)
from tests.conftest import make_download, series_book, standalone_book


@pytest.fixture(autouse=True)
def _instant_stability(monkeypatch: pytest.MonkeyPatch):
    """Downloads normally have to sit still for a minute before being touched."""
    monkeypatch.setattr(watcher, "STABLE_AFTER_SECONDS", 0)
    watcher._seen_sizes.clear()  # pyright: ignore[reportPrivateUsage]


@pytest.fixture
def configured(session: Session, library: tuple[Path, Path]) -> tuple[Path, Path]:
    downloads, root = library
    library_config.set_enabled(session, True)
    library_config.set_mode(session, OrganizeModeEnum.copy)
    library_config.set_download_dir(session, str(downloads))
    library_config.set_root_dir(session, str(root))
    library_config.set_folder_template(session, "{author}/{title}")
    return downloads, root


class TestMatching:
    def setup_candidates(self, downloads: Path) -> list[Path]:
        for name in [
            "Brandon Sanderson - The Way of Kings [M4B 64kbps]",
            "Andy Weir - The Martian (2013) [MP3]",
            "completely.unrelated.release",
        ]:
            (downloads / name).mkdir()
        return sorted(downloads.iterdir())

    def test_exact_name_wins(self, library: tuple[Path, Path]):
        downloads, _ = library
        candidates = self.setup_candidates(downloads)
        match = watcher.find_match(
            "Brandon Sanderson - The Way of Kings [M4B 64kbps]", candidates, 85
        )
        assert match is not None
        assert match.name == "Brandon Sanderson - The Way of Kings [M4B 64kbps]"

    def test_a_torrent_extension_on_the_release_still_matches(self, library: tuple[Path, Path]):
        downloads, _ = library
        candidates = self.setup_candidates(downloads)
        match = watcher.find_match(
            "Brandon Sanderson - The Way of Kings [M4B 64kbps].torrent", candidates, 85
        )
        assert match is not None
        assert match.name.startswith("Brandon Sanderson")

    def test_nothing_similar_enough_is_no_match(self, library: tuple[Path, Path]):
        downloads, _ = library
        candidates = self.setup_candidates(downloads)
        assert watcher.find_match("Some Totally Other Book", candidates, 85) is None

    def test_the_book_title_is_tried_when_the_client_renamed_the_folder(
        self, library: tuple[Path, Path]
    ):
        """Seen live: MyAnonamouse listed a release as
        "Silverthorn by Raymond E Feist [ENG / M4B]" and qBittorrent created
        "03 Silverthorn", which scores 40 against the release title."""
        downloads, _ = library
        (downloads / "03 Silverthorn").mkdir()
        candidates = sorted(downloads.iterdir())

        match = find_match_with_title(
            "Silverthorn by Raymond E Feist [ENG / M4B]",
            candidates,
            85,
            "Silverthorn",
        )

        assert match is not None
        assert match.name == "03 Silverthorn"

    def test_another_book_in_the_same_series_is_not_matched(
        self, library: tuple[Path, Path]
    ):
        """The reason plain ratio is used rather than a partial or token set
        ratio: those score Dune against Dune Messiah as a perfect match."""
        downloads, _ = library
        (downloads / "Dune").mkdir()
        candidates = sorted(downloads.iterdir())

        assert (
            find_match_with_title("Dune Messiah release", candidates, 85, "Dune Messiah")
            is None
        )

    def test_a_threshold_of_zero_matches_anything(self, library: tuple[Path, Path]):
        downloads, _ = library
        candidates = self.setup_candidates(downloads)
        assert watcher.find_match("Some Totally Other Book", candidates, 0) is not None


class TestStability:
    def test_a_download_must_be_seen_twice_at_the_same_size(self, library: tuple[Path, Path]):
        downloads, _ = library
        src = make_download(downloads, "book", {"a.m4b": "x"})

        assert watcher.is_stable(src) is False, "first sighting is never trusted"
        assert watcher.is_stable(src) is True

    def test_growing_downloads_are_not_stable(self, library: tuple[Path, Path]):
        downloads, _ = library
        src = make_download(downloads, "book", {"a.m4b": "x"})
        _ = watcher.is_stable(src)
        _ = (src / "b.m4b").write_text("more data")

        assert watcher.is_stable(src) is False

    def test_partial_files_block_it(self, library: tuple[Path, Path]):
        downloads, _ = library
        src = make_download(downloads, "book", {"a.m4b": "x", "b.m4b.part": "y"})

        assert watcher.is_stable(src) is False
        assert watcher.is_stable(src) is False


class TestScan:
    def test_a_finished_download_is_organized_and_marked(
        self, session: Session, configured: tuple[Path, Path]
    ):
        downloads, root = configured
        book = standalone_book()
        session.add(book)
        session.commit()
        _ = record_grab(session, book, book.asin, "andy weir - the martian")
        _ = make_download(downloads, "andy weir - the martian", {"01.m4b": "x"})

        assert watcher.scan(session) == 0, "not stable on the first pass"
        assert watcher.scan(session) == 1

        session.refresh(book)
        assert book.download_status == DownloadStatusEnum.downloaded
        assert book.downloaded is True
        assert (root / "Andy Weir" / "The Martian" / "01.m4b").exists()

    def test_partial_downloads_are_left_alone(
        self, session: Session, configured: tuple[Path, Path]
    ):
        downloads, _ = configured
        book = standalone_book()
        session.add(book)
        session.commit()
        _ = record_grab(session, book, book.asin, "andy weir - the martian")
        _ = make_download(downloads, "andy weir - the martian", {"01.m4b.part": "x"})

        assert watcher.scan(session) == 0
        assert watcher.scan(session) == 0

    def test_nothing_happens_while_disabled(
        self, session: Session, configured: tuple[Path, Path]
    ):
        downloads, _ = configured
        library_config.set_enabled(session, False)
        book = standalone_book()
        session.add(book)
        session.commit()
        session.add(
            LibraryImport(asin_or_uuid=book.asin, book_title=book.title, release_title="rel")
        )
        session.commit()
        _ = make_download(downloads, "rel", {"01.m4b": "x"})

        assert watcher.scan(session) == 0

    def test_a_regrab_does_not_queue_a_second_row(self, session: Session, configured):
        book = series_book()
        session.add(book)
        session.commit()
        _ = record_grab(session, book, book.asin, "release one")
        _ = record_grab(session, book, book.asin, "release two")

        rows = session.exec(select(LibraryImport)).all()
        assert len(rows) == 1
        assert rows[0].release_title == "release two"

    def test_nothing_is_queued_while_the_feature_is_off(self, session: Session):
        library_config.set_enabled(session, False)
        book = series_book()
        session.add(book)
        session.commit()

        assert record_grab(session, book, book.asin, "rel") is None
        assert session.exec(select(LibraryImport)).all() == []


class TestFailureHandling:
    def test_a_grab_that_never_arrives_returns_to_the_wishlist(
        self, session: Session, configured: tuple[Path, Path]
    ):
        book = standalone_book()
        book.downloaded = True
        book.download_status = DownloadStatusEnum.grabbed
        session.add(book)
        session.add(
            LibraryImport(
                asin_or_uuid=book.asin,
                book_title=book.title,
                release_title="never shows up",
                created_at=datetime.now() - timedelta(days=60),
            )
        )
        session.commit()

        _ = watcher.scan(session)

        session.refresh(book)
        assert book.download_status == DownloadStatusEnum.failed
        assert book.downloaded is False, "must be eligible to be grabbed again"

        entry = session.exec(select(LibraryImport)).one()
        assert entry.status == LibraryImportStatusEnum.expired

    def test_a_recent_grab_is_not_expired(self, session: Session, configured: tuple[Path, Path]):
        book = standalone_book()
        book.downloaded = True
        session.add(book)
        session.add(
            LibraryImport(
                asin_or_uuid=book.asin, book_title=book.title, release_title="still waiting"
            )
        )
        session.commit()

        _ = watcher.scan(session)

        entry = session.exec(select(LibraryImport)).one()
        assert entry.status == LibraryImportStatusEnum.pending
        session.refresh(book)
        assert book.downloaded is True


class TestBookLookup:
    def test_finds_an_audiobook_by_asin(self, session: Session):
        book = series_book()
        session.add(book)
        session.commit()
        found = watcher.get_book(session, book.asin)
        assert isinstance(found, Audiobook)

    def test_missing_identifier_returns_none(self, session: Session):
        assert watcher.get_book(session, "NOPE") is None
