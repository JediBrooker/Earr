"""Importing downloads by hand when the library watcher couldn't match them."""

import json
from pathlib import Path

from fastapi.testclient import TestClient
from sqlmodel import Session, select

from app.internal.auth.authentication import create_user
from app.internal.library.config import library_config
from app.internal.library.manual_import import import_targets, list_candidates
from app.internal.models import (
    Audiobook,
    AudiobookRequest,
    DownloadStatusEnum,
    GroupEnum,
    LibraryImport,
    LibraryImportStatusEnum,
    OrganizeModeEnum,
)
from tests.conftest import make_download, manual_request, standalone_book


def silverthorn():
    return standalone_book(
        "B9", title="Silverthorn", authors=["Raymond E. Feist"], narrators=["Peter Joyce"]
    )


def requested(session: Session, *books: object) -> None:
    session.add(create_user("boss", "boss-password", GroupEnum.admin))
    for book in books:
        session.add(book)
        session.add(AudiobookRequest(asin=book.asin, user_username="boss"))  # pyright: ignore[reportAttributeAccessIssue]
    session.commit()


def organizing(session: Session, library: tuple[Path, Path]) -> None:
    downloads, root = library
    library_config.set_enabled(session, True)
    library_config.set_download_dirs(session, str(downloads))
    library_config.set_root_dir(session, str(root))
    library_config.set_mode(session, OrganizeModeEnum.copy)


class TestCandidates:
    def test_unimported_downloads_are_listed(
        self, session: Session, library: tuple[Path, Path]
    ):
        downloads, _ = library
        organizing(session, library)
        make_download(downloads, "03 Silverthorn", {"01.mp3": "aaaa", "02.mp3": "bb"})
        done = make_download(downloads, "Already done", {"a.mp3": "x"})
        session.add(
            LibraryImport(
                asin_or_uuid="B1",
                book_title="t",
                release_title="Already done",
                status=LibraryImportStatusEnum.imported,
                source_path=str(done.resolve()),
            )
        )
        session.commit()

        [candidate] = list_candidates(session, import_targets(session))

        assert candidate.name == "03 Silverthorn"
        assert (candidate.files, candidate.size) == (2, 6)
        assert not candidate.incomplete

    def test_a_download_with_partial_files_is_not_ready(
        self, session: Session, library: tuple[Path, Path]
    ):
        downloads, _ = library
        organizing(session, library)
        make_download(downloads, "Half there", {"01.mp3.part": "x"})

        [candidate] = list_candidates(session, import_targets(session))

        assert candidate.incomplete

    def test_the_matching_request_is_suggested_despite_the_renamed_folder(
        self, session: Session, library: tuple[Path, Path]
    ):
        downloads, _ = library
        organizing(session, library)
        requested(session, silverthorn(), standalone_book("B2"))
        make_download(downloads, "03 Silverthorn", {"01.mp3": "x"})

        [candidate] = list_candidates(session, import_targets(session))

        assert candidate.suggestion == "B9"

    def test_books_already_in_the_library_are_not_offered(self, session: Session):
        in_library = standalone_book("B1", download_status=DownloadStatusEnum.downloaded)
        on_its_way = silverthorn()
        requested(session, in_library, on_its_way)
        session.add(manual_request(title="An Indie Book"))
        session.commit()

        labels = [t.label for t in import_targets(session)]

        assert "Silverthorn by Raymond E. Feist" in labels
        assert "An Indie Book by Jane Doe (manual)" in labels
        assert not any(label.startswith("The Martian") for label in labels)


class TestImportPage:
    def test_importing_places_the_files_in_the_library(
        self, client: TestClient, session: Session, library: tuple[Path, Path]
    ):
        downloads, root = library
        organizing(session, library)
        requested(session, silverthorn())
        source = make_download(downloads, "03 Silverthorn", {"01.mp3": "audio"})

        response = client.post(
            "/activity/import",
            data={"source_path": str(source), "asin_or_uuid": "B9"},
            auth=("boss", "boss-password"),
        )

        assert response.status_code == 200, response.text
        toast = json.loads(response.headers["HX-Trigger"])["toast"]
        assert toast["type"] == "success"
        assert list(root.rglob("01.mp3")), "the file was copied into the library"
        row = session.exec(select(LibraryImport)).one()
        assert row.status == LibraryImportStatusEnum.imported
        assert "03 Silverthorn" not in response.text, "it drops off the list"
        book = session.get(Audiobook, "B9")
        assert book is not None
        session.refresh(book)
        assert book.download_status == DownloadStatusEnum.downloaded
        assert book.downloaded, "it counts as in the library, like a watcher import"

    def test_a_path_outside_the_downloads_folder_is_refused(
        self, client: TestClient, session: Session, library: tuple[Path, Path]
    ):
        organizing(session, library)
        requested(session, silverthorn())

        response = client.post(
            "/activity/import",
            data={"source_path": "/etc", "asin_or_uuid": "B9"},
            auth=("boss", "boss-password"),
        )

        toast = json.loads(response.headers["HX-Trigger"])["toast"]
        assert toast["type"] == "error"
        assert "completed downloads folder" in toast["message"]

    def test_only_admins_can_import(
        self, client: TestClient, session: Session, library: tuple[Path, Path]
    ):
        organizing(session, library)
        session.add(create_user("reader", "reader-password", GroupEnum.untrusted))
        session.commit()

        response = client.get("/activity/import", auth=("reader", "reader-password"))

        assert response.status_code == 403
