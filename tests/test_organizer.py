"""File operations against a real library. These are the ones that can lose or
duplicate somebody's audiobooks, so they are exercised on real files."""

import errno
import os
from pathlib import Path
from unittest import mock

import pytest
from sqlmodel import Session

from app.internal.library.config import library_config
from app.internal.library.organizer import (
    OrganizeError,
    has_incomplete_files,
    iter_source_files,
    organize,
    resolve_target_dir,
)
from app.internal.models import OrganizeModeEnum
from tests.conftest import make_download, series_book, standalone_book


class TestFileDiscovery:
    def test_walks_nested_folders(self, library: tuple[Path, Path]):
        downloads, _ = library
        src = make_download(
            downloads,
            "book",
            {"Disc 1/01.m4b": "a", "Disc 1/02.m4b": "b", "Disc 2/03.m4b": "c"},
        )
        assert sorted(str(p) for p in iter_source_files(src)) == [
            "Disc 1/01.m4b",
            "Disc 1/02.m4b",
            "Disc 2/03.m4b",
        ]

    def test_a_single_file_download_yields_its_own_name(self, library: tuple[Path, Path]):
        downloads, _ = library
        src = downloads / "The Martian.m4b"
        _ = src.write_text("x")
        assert [str(p) for p in iter_source_files(src)] == ["The Martian.m4b"]

    @pytest.mark.parametrize("marker", ["a.m4b.part", "a.m4b.!qB", "a.m4b.crdownload"])
    def test_partial_downloads_are_detected(self, library: tuple[Path, Path], marker: str):
        downloads, _ = library
        src = make_download(downloads, "book", {"a.m4b": "x", marker: "y"})
        assert has_incomplete_files(src) is True

    def test_a_finished_download_is_not_flagged(self, library: tuple[Path, Path]):
        downloads, _ = library
        src = make_download(downloads, "book", {"a.m4b": "x", "cover.jpg": "y"})
        assert has_incomplete_files(src) is False


class TestModes:
    def test_hardlink_shares_storage_and_leaves_the_source(self, library: tuple[Path, Path]):
        downloads, root = library
        src = make_download(downloads, "book", {"01.m4b": "a", "cover.jpg": "b"})

        out = organize(
            source=src,
            target_dir=root / "Author" / "Title",
            mode=OrganizeModeEnum.hardlink,
            overwrite=False,
        )

        assert sorted(p.name for p in out.iterdir()) == ["01.m4b", "cover.jpg"]
        assert os.stat(out / "01.m4b").st_ino == os.stat(src / "01.m4b").st_ino
        assert src.exists(), "hardlinking must not disturb the torrent being seeded"

    def test_copy_makes_an_independent_file(self, library: tuple[Path, Path]):
        downloads, root = library
        src = make_download(downloads, "book", {"01.m4b": "a"})

        out = organize(
            source=src,
            target_dir=root / "Author" / "Title",
            mode=OrganizeModeEnum.copy,
            overwrite=False,
        )

        assert os.stat(out / "01.m4b").st_ino != os.stat(src / "01.m4b").st_ino
        assert src.exists()

    def test_move_takes_the_files_and_prunes_the_leftovers(self, library: tuple[Path, Path]):
        downloads, root = library
        src = make_download(downloads, "book", {"sub/01.m4b": "a", "02.m4b": "b"})

        out = organize(
            source=src,
            target_dir=root / "Author" / "Title",
            mode=OrganizeModeEnum.move,
            overwrite=False,
        )

        assert sorted(p.name for p in out.rglob("*") if p.is_file()) == ["01.m4b", "02.m4b"]
        assert not src.exists()

    def test_nested_layout_is_preserved(self, library: tuple[Path, Path]):
        downloads, root = library
        src = make_download(downloads, "book", {"Disc 1/01.m4b": "a", "Disc 2/02.m4b": "b"})

        out = organize(
            source=src,
            target_dir=root / "T",
            mode=OrganizeModeEnum.copy,
            overwrite=False,
        )

        assert (out / "Disc 1" / "01.m4b").exists()
        assert (out / "Disc 2" / "02.m4b").exists()


class TestOverwrite:
    def test_existing_files_are_kept_by_default(self, library: tuple[Path, Path]):
        downloads, root = library
        src = make_download(downloads, "book", {"01.m4b": "NEW"})
        target = root / "Author" / "Title"
        target.mkdir(parents=True)
        _ = (target / "01.m4b").write_text("OLD")

        _ = organize(source=src, target_dir=target, mode=OrganizeModeEnum.copy, overwrite=False)

        assert (target / "01.m4b").read_text() == "OLD"

    def test_overwrite_replaces_them(self, library: tuple[Path, Path]):
        downloads, root = library
        src = make_download(downloads, "book", {"01.m4b": "NEW"})
        target = root / "Author" / "Title"
        target.mkdir(parents=True)
        _ = (target / "01.m4b").write_text("OLD")

        _ = organize(source=src, target_dir=target, mode=OrganizeModeEnum.copy, overwrite=True)

        assert (target / "01.m4b").read_text() == "NEW"


class TestFailures:
    def test_missing_source(self, library: tuple[Path, Path]):
        downloads, root = library
        with pytest.raises(OrganizeError, match="not found"):
            organize(
                source=downloads / "nope",
                target_dir=root / "T",
                mode=OrganizeModeEnum.copy,
                overwrite=False,
            )

    def test_empty_download(self, library: tuple[Path, Path]):
        downloads, root = library
        src = downloads / "empty"
        src.mkdir()
        with pytest.raises(OrganizeError, match="no files"):
            organize(
                source=src,
                target_dir=root / "T",
                mode=OrganizeModeEnum.copy,
                overwrite=False,
            )

    def test_a_partial_copy_is_rolled_back(self, library: tuple[Path, Path]):
        """A half written book is worse than none."""
        downloads, root = library
        src = make_download(downloads, "book", {"a.m4b": "1", "b.m4b": "2", "c.m4b": "3"})
        target = root / "Author" / "Title"

        real_copy = __import__("shutil").copy2
        calls = {"n": 0}

        def flaky(s: object, d: object, **kw: object):
            calls["n"] += 1
            if calls["n"] == 2:
                raise OSError(errno.ENOSPC, "No space left on device")
            return real_copy(s, d, **kw)  # pyright: ignore[reportArgumentType]

        with mock.patch("app.internal.library.organizer.shutil.copy2", flaky):
            with pytest.raises(OSError):
                organize(
                    source=src,
                    target_dir=target,
                    mode=OrganizeModeEnum.copy,
                    overwrite=False,
                )

        assert [p for p in target.rglob("*") if p.is_file()] == []

    def test_cross_device_hardlink_explains_itself(self, library: tuple[Path, Path]):
        downloads, root = library
        src = make_download(downloads, "book", {"a.m4b": "x"})

        def cross_device(*_: object):
            raise OSError(errno.EXDEV, "Invalid cross-device link")

        with mock.patch("app.internal.library.organizer.os.link", cross_device):
            with pytest.raises(OrganizeError, match="same filesystem"):
                organize(
                    source=src,
                    target_dir=root / "T",
                    mode=OrganizeModeEnum.hardlink,
                    overwrite=False,
                )


class TestTargetResolution:
    def test_uses_the_configured_template(self, session: Session, library: tuple[Path, Path]):
        _, root = library
        library_config.set_root_dir(session, str(root))
        library_config.set_folder_template(session, "{author}/{series}/{title}")

        target = resolve_target_dir(session, series_book())

        assert target == root.resolve() / "Brandon Sanderson" / "The Stormlight Archive" / "The Way of Kings"

    def test_standalone_books_collapse_the_empty_segment(
        self, session: Session, library: tuple[Path, Path]
    ):
        _, root = library
        library_config.set_root_dir(session, str(root))
        library_config.set_folder_template(session, "{author}/{series}/{title}")

        assert resolve_target_dir(session, standalone_book()) == (
            root.resolve() / "Andy Weir" / "The Martian"
        )

    def test_refuses_to_write_outside_the_library(
        self, session: Session, library: tuple[Path, Path]
    ):
        _, root = library
        library_config.set_root_dir(session, str(root))
        library_config.set_folder_template(session, "{author}/{title}")

        with pytest.raises(OrganizeError):
            _ = resolve_target_dir(session, standalone_book(title="..", authors=[".."]))
