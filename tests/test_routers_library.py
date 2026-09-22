"""The library API, including the authorisation boundary and the path
confinement on the manual import endpoint."""

from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session

from app.internal.library.config import library_config
from app.internal.models import (
    GroupEnum,
    LibraryImport,
    LibraryImportStatusEnum,
    OrganizeModeEnum,
)
from tests.conftest import api_key_for, auth, make_download, standalone_book


@pytest.fixture
def admin(session: Session) -> dict[str, str]:
    return auth(api_key_for(session, GroupEnum.admin, "admin"))


@pytest.fixture
def trusted(session: Session) -> dict[str, str]:
    return auth(api_key_for(session, GroupEnum.trusted, "trusted"))


@pytest.fixture
def configured(session: Session, library: tuple[Path, Path]) -> tuple[Path, Path]:
    downloads, root = library
    library_config.set_enabled(session, True)
    library_config.set_mode(session, OrganizeModeEnum.copy)
    library_config.set_download_dir(session, str(downloads))
    library_config.set_root_dir(session, str(root))
    library_config.set_folder_template(session, "{author}/{title}")
    return downloads, root


class TestAuthorisation:
    ENDPOINTS = [
        ("GET", "/api/settings/library"),
        ("GET", "/api/settings/library/placeholders"),
        ("GET", "/api/library/imports"),
        ("POST", "/api/library/scan"),
    ]

    @pytest.mark.parametrize("method,path", ENDPOINTS)
    def test_rejected_without_credentials(
        self, client: TestClient, method: str, path: str
    ):
        assert client.request(method, path).status_code in (401, 403)

    @pytest.mark.parametrize("method,path", ENDPOINTS)
    def test_rejected_for_non_admins(
        self, client: TestClient, trusted: dict[str, str], method: str, path: str
    ):
        # a valid key for an insufficient group is answered with 401 rather than
        # 403, so this asserts that access is denied rather than the exact code
        assert client.request(method, path, headers=trusted).status_code in (401, 403)

    def test_a_bad_key_is_rejected(self, client: TestClient):
        assert client.get(
            "/api/settings/library", headers=auth("not-a-real-key")
        ).status_code in (401, 403)


class TestSettings:
    def test_defaults_are_returned(self, client: TestClient, admin: dict[str, str]):
        body = client.get("/api/settings/library", headers=admin).json()

        assert body["enabled"] is False
        assert body["mode"] == "hardlink"
        assert body["folder_template"]

    def test_round_trip(self, client: TestClient, admin: dict[str, str], tmp_path: Path):
        payload = {
            "enabled": True,
            "mode": "copy",
            "download_dir": str(tmp_path / "dl"),
            "root_dir": str(tmp_path / "lib"),
            "folder_template": "{author}/{series}/{title}",
            "scan_interval": 600,
            "match_threshold": 70,
            "overwrite": True,
            "write_metadata": False,
        }

        assert client.patch(
            "/api/settings/library", json=payload, headers=admin
        ).status_code == 204

        body = client.get("/api/settings/library", headers=admin).json()
        assert body["folder_template"] == "{author}/{series}/{title}"
        assert body["mode"] == "copy"
        assert body["match_threshold"] == 70
        assert body["write_metadata"] is False

    @pytest.mark.parametrize(
        "field,value",
        [
            ("folder_template", "{author}/{nope}"),
            ("folder_template", ""),
            ("scan_interval", 5),
            ("match_threshold", 500),
        ],
    )
    def test_invalid_settings_are_refused(
        self, client: TestClient, admin: dict[str, str], tmp_path: Path, field: str, value: object
    ):
        payload = {
            "enabled": False,
            "mode": "copy",
            "download_dir": str(tmp_path),
            "root_dir": str(tmp_path),
            "folder_template": "{author}/{title}",
            "scan_interval": 300,
            "match_threshold": 85,
            "overwrite": False,
            "write_metadata": True,
            field: value,
        }

        assert client.patch(
            "/api/settings/library", json=payload, headers=admin
        ).status_code == 422

    def test_enabling_without_folders_is_refused(
        self, client: TestClient, admin: dict[str, str]
    ):
        payload = {
            "enabled": True,
            "mode": "copy",
            "download_dir": "",
            "root_dir": "",
            "folder_template": "{author}/{title}",
            "scan_interval": 300,
            "match_threshold": 85,
            "overwrite": False,
            "write_metadata": True,
        }

        assert client.patch(
            "/api/settings/library", json=payload, headers=admin
        ).status_code == 422

    def test_placeholders_are_documented(self, client: TestClient, admin: dict[str, str]):
        body = client.get("/api/settings/library/placeholders", headers=admin).json()

        assert "series_position" in body["tokens"]
        assert body["examples"]


class TestImports:
    def test_listing_is_empty_to_begin_with(
        self, client: TestClient, admin: dict[str, str]
    ):
        assert client.get("/api/library/imports", headers=admin).json() == []

    def test_listing_can_be_filtered_by_status(
        self, client: TestClient, admin: dict[str, str], session: Session
    ):
        session.add(
            LibraryImport(asin_or_uuid="A", book_title="Pending", release_title="p")
        )
        session.add(
            LibraryImport(
                asin_or_uuid="B",
                book_title="Done",
                release_title="d",
                status=LibraryImportStatusEnum.imported,
            )
        )
        session.commit()

        done = client.get(
            "/api/library/imports?status=imported", headers=admin
        ).json()

        assert [e["book_title"] for e in done] == ["Done"]

    def test_scan_needs_the_folders_configured(
        self, client: TestClient, admin: dict[str, str]
    ):
        assert client.post("/api/library/scan", headers=admin).status_code == 400


class TestManualImport:
    def endpoint(self, asin: str, path: str) -> dict[str, str]:
        return {"asin_or_uuid": asin, "source_path": path}

    def test_organizes_a_download(
        self,
        client: TestClient,
        admin: dict[str, str],
        session: Session,
        configured: tuple[Path, Path],
    ):
        downloads, root = configured
        book = standalone_book()
        session.add(book)
        session.commit()
        _ = make_download(downloads, "some release", {"01.m4b": "x"})

        r = client.post(
            "/api/library/import",
            json=self.endpoint(book.asin, "some release"),
            headers=admin,
        )

        assert r.status_code == 200, r.text
        assert (root / "Andy Weir" / "The Martian" / "01.m4b").exists()

    @pytest.mark.parametrize("escape", ["/etc", "../../../etc", "/"])
    def test_refuses_paths_outside_the_downloads_folder(
        self,
        client: TestClient,
        admin: dict[str, str],
        session: Session,
        configured: tuple[Path, Path],
        escape: str,
    ):
        book = standalone_book()
        session.add(book)
        session.commit()

        r = client.post(
            "/api/library/import",
            json=self.endpoint(book.asin, escape),
            headers=admin,
        )

        assert r.status_code == 400
        assert "inside the completed downloads folder" in r.json()["detail"]

    def test_unknown_book(
        self, client: TestClient, admin: dict[str, str], configured: tuple[Path, Path]
    ):
        r = client.post(
            "/api/library/import",
            json=self.endpoint("NOSUCHASIN", "anything"),
            headers=admin,
        )
        assert r.status_code == 404

    def test_missing_path(
        self,
        client: TestClient,
        admin: dict[str, str],
        session: Session,
        configured: tuple[Path, Path],
    ):
        book = standalone_book()
        session.add(book)
        session.commit()

        r = client.post(
            "/api/library/import",
            json=self.endpoint(book.asin, "not-there"),
            headers=admin,
        )
        assert r.status_code == 404
