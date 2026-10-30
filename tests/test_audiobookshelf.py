"""Handing new books to Audiobookshelf, and the Library page that browses it.

A small aiohttp app stands in for Audiobookshelf, with the routes and response
shapes of its current server code (2.37).
"""

import asyncio
from pathlib import Path
from typing import Any

from aiohttp import ClientSession, web
from fastapi.testclient import TestClient
from sqlmodel import Session

from app.internal.audiobookshelf.config import abs_config
from app.internal.audiobookshelf.handoff import (
    add_to_audiobookshelf,
    missing_metadata,
)
from app.internal.audiobookshelf.handoff import _Metadata as ItemMetadata  # pyright: ignore[reportPrivateUsage]
from app.internal.auth.authentication import create_user
from app.internal.models import DownloadStatusEnum, GroupEnum
from tests.conftest import make_download, manual_request, series_book, standalone_book

LIBRARY = "lib1"


async def no_wait(_: float) -> None:
    return None


class FakeABS:
    """Records what Earr asked for and answers like Audiobookshelf would."""

    def __init__(self, *, watcher_status: int = 200, appears: bool = True):
        self.watcher_status: int = watcher_status
        self.appears: bool = appears
        self.items: list[dict[str, Any]] = []
        self.watcher_calls: list[dict[str, Any]] = []
        self.patches: list[tuple[str, dict[str, Any]]] = []
        self.scans: int = 0

    def routes(self) -> list[Any]:
        return [
            web.post("/api/watcher/update", self.watcher),
            web.get("/api/libraries/{id}/items", self.list_items),
            web.get("/api/libraries/{id}/search", self.search),
            web.post("/api/libraries/{id}/scan", self.scan),
            web.patch("/api/items/{id}/media", self.patch),
            web.get("/api/items/{id}/cover", self.cover),
            web.get("/api/libraries", self.libraries),
        ]

    async def watcher(self, request: web.Request) -> web.Response:
        body: dict[str, Any] = await request.json()
        self.watcher_calls.append(body)
        if self.watcher_status == 200 and self.appears:
            folder = str(Path(body["path"]).parent)
            self.items.insert(0, item("li_new", "The Way of Kings", path=folder))
        return web.Response(status=self.watcher_status)

    async def list_items(self, request: web.Request) -> web.Response:
        return web.json_response({"results": self.items, "total": len(self.items)})

    async def search(self, request: web.Request) -> web.Response:
        wanted = request.query["q"].lower()
        hits = [
            {"libraryItem": full_item(i)}
            for i in self.items
            if wanted in i["media"]["metadata"]["title"].lower()
        ]
        return web.json_response({"book": hits})

    async def scan(self, request: web.Request) -> web.Response:
        self.scans += 1
        return web.Response(status=200)

    async def patch(self, request: web.Request) -> web.Response:
        self.patches.append((request.match_info["id"], await request.json()))
        return web.json_response({"updated": True, "libraryItem": {}})

    async def cover(self, request: web.Request) -> web.Response:
        return web.Response(body=b"\x89PNG fake", content_type="image/png")

    async def libraries(self, request: web.Request) -> web.Response:
        return web.json_response(
            {"libraries": [{"id": LIBRARY, "name": "Audiobooks", "mediaType": "book"}]}
        )


def item(item_id: str, title: str, path: str = "/x", **metadata: Any) -> dict[str, Any]:
    """A minified library item, as GET /api/libraries/:id/items returns them."""
    return {
        "id": item_id,
        "path": path,
        "mediaType": "book",
        "media": {
            "duration": 98_000.0,
            "metadata": {"title": title, "authorName": "Brandon Sanderson"} | metadata,
        },
    }


def full_item(minified: dict[str, Any]) -> dict[str, Any]:
    """The expanded shape search returns."""
    meta = minified["media"]["metadata"]
    return minified | {
        "media": {
            "duration": minified["media"]["duration"],
            "metadata": {
                "title": meta["title"],
                "authors": [{"id": "a1", "name": meta["authorName"]}],
                "narrators": ["Michael Kramer"],
                "series": [{"id": "s1", "name": "The Stormlight Archive", "sequence": "1"}],
            },
        }
    }


async def connect(session: Session, serve: Any, fake: FakeABS) -> str:
    url: str = await serve(fake.routes())
    abs_config.set_base_url(session, url)
    abs_config.set_api_token(session, "token")
    abs_config.set_library_id(session, LIBRARY)
    return url


class TestHandOff:
    async def test_a_new_folder_is_added_without_scanning_the_library(
        self, session: Session, serve: Any, library: tuple[Path, Path]
    ):
        _, root = library
        fake = FakeABS()
        await connect(session, serve, fake)
        folder = make_download(root, "Brandon Sanderson/The Way of Kings", {"01.m4b": "a"})

        async with ClientSession() as cs:
            await add_to_audiobookshelf(
                session, cs, [(folder, series_book())], sleep=no_wait
            )

        [call] = fake.watcher_calls
        assert call["libraryId"] == LIBRARY
        assert call["type"] == "add"
        assert call["path"].endswith("01.m4b"), "the watcher ignores bare folders"
        assert fake.scans == 0

        [(item_id, body)] = fake.patches
        assert item_id == "li_new"
        metadata = body["metadata"]
        assert metadata["asin"] == "B1"
        assert metadata["narrators"] == ["Michael Kramer", "Kate Reading"]
        assert metadata["series"] == [{"name": "The Stormlight Archive", "sequence": "1"}]

    async def test_falls_back_to_a_full_scan_when_the_item_never_appears(
        self, session: Session, serve: Any, library: tuple[Path, Path]
    ):
        _, root = library
        # 200 but nothing happens: what Audiobookshelf does with its watcher off
        fake = FakeABS(appears=False)
        await connect(session, serve, fake)
        folder = make_download(root, "Book", {"01.mp3": "a"})

        async with ClientSession() as cs:
            await add_to_audiobookshelf(session, cs, [(folder, standalone_book())], sleep=no_wait)

        assert fake.scans == 1
        assert fake.patches == []

    async def test_older_audiobookshelf_gets_a_full_scan(
        self, session: Session, serve: Any, library: tuple[Path, Path]
    ):
        _, root = library
        fake = FakeABS(watcher_status=404)
        await connect(session, serve, fake)
        folder = make_download(root, "Book", {"01.mp3": "a"})

        async with ClientSession() as cs:
            await add_to_audiobookshelf(session, cs, [(folder, standalone_book())], sleep=no_wait)

        assert fake.scans == 1

    async def test_nothing_is_sent_without_audiobookshelf(
        self, session: Session, library: tuple[Path, Path]
    ):
        _, root = library
        folder = make_download(root, "Book", {"01.mp3": "a"})

        async with ClientSession() as cs:
            # would fail loudly if it tried to reach a server
            await add_to_audiobookshelf(session, cs, [(folder, standalone_book())], sleep=no_wait)


class TestMissingMetadata:
    def test_what_audiobookshelf_already_has_is_left_alone(self):
        filled = ItemMetadata(
            subtitle="Book One",
            narratorName="Someone Else",
            seriesName="Stormlight #1",
            publishedYear="2010",
            asin="B0OTHER",
        )

        assert missing_metadata(filled, series_book()) == {}

    def test_a_manual_request_contributes_its_year_and_narrators(self):
        patch = missing_metadata(
            ItemMetadata(), manual_request(narrators=["Jane Reader"])
        )

        assert patch == {"narrators": ["Jane Reader"], "publishedYear": "2021"}


class TestLibraryPage:
    async def test_lists_the_audiobookshelf_library(
        self, client: TestClient, session: Session, serve: Any
    ):
        fake = FakeABS()
        fake.items = [
            item("li_1", "The Way of Kings", narratorName="Michael Kramer", seriesName="Stormlight #1"),
            item("li_2", "Words of Radiance"),
        ]
        await connect(session, serve, fake)
        session.add(create_user("reader", "reader-password", GroupEnum.untrusted))
        session.commit()

        # off the event loop, so the fake Audiobookshelf on it can answer
        response = await asyncio.to_thread(
            lambda: client.get("/library", auth=("reader", "reader-password"))
        )

        assert response.status_code == 200, response.text
        page = response.text
        assert "2 books in Audiobookshelf" in page
        assert "Words of Radiance" in page
        assert "Stormlight #1" in page
        assert "/library/cover/li_1" in page, "covers come through Earr"

    async def test_search_uses_audiobookshelf_search(
        self, client: TestClient, session: Session, serve: Any
    ):
        fake = FakeABS()
        fake.items = [item("li_1", "The Way of Kings"), item("li_2", "Words of Radiance")]
        await connect(session, serve, fake)
        session.add(create_user("reader", "reader-password", GroupEnum.untrusted))
        session.commit()

        response = await asyncio.to_thread(
            lambda: client.get(
                "/library", params={"q": "radiance"}, auth=("reader", "reader-password")
            )
        )

        assert "Words of Radiance" in response.text
        assert "The Way of Kings" not in response.text
        assert "Michael Kramer" in response.text, "search results' narrators are read too"

    async def test_covers_are_fetched_through_earr(
        self, client: TestClient, session: Session, serve: Any
    ):
        await connect(session, serve, FakeABS())
        session.add(create_user("reader", "reader-password", GroupEnum.untrusted))
        session.commit()

        cover = await asyncio.to_thread(
            lambda: client.get("/library/cover/li_1", auth=("reader", "reader-password"))
        )
        bad = await asyncio.to_thread(
            lambda: client.get(
                "/library/cover/..%2F..%2Fetc", auth=("reader", "reader-password")
            )
        )

        assert cover.status_code == 200
        assert cover.headers["content-type"] == "image/png"
        assert bad.status_code == 404

    def test_without_audiobookshelf_it_shows_what_earr_placed(
        self, client: TestClient, session: Session
    ):
        session.add(create_user("reader", "reader-password", GroupEnum.untrusted))
        session.add(standalone_book("B1", download_status=DownloadStatusEnum.downloaded))
        session.add(
            series_book("B2", downloaded=True, download_status=DownloadStatusEnum.grabbed)
        )
        session.commit()

        response = client.get("/library", auth=("reader", "reader-password"))

        assert "The Martian" in response.text
        assert "The Way of Kings" not in response.text, "still downloading"
        assert "Connect Audiobookshelf" in response.text

