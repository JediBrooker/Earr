"""A stand-in for Prowlarr, so the Sources page can be captured without real indexers.

Every search answers with the same spread of made-up releases for the book whose
title matches the query: a few that clear the default download settings, and
one each that fails on bitrate, seeders and title match. Sizes are worked out
from the bitrate and the book's runtime, which is how Earr estimates bitrate.
"""

import asyncio
import re
import threading
from datetime import datetime, timedelta
from typing import TypedDict

from aiohttp import web


class StubBook(TypedDict):
    title: str
    authors: list[str]
    narrators: list[str]
    runtime_length_min: int


INDEXERS = [
    {"id": 1, "name": "MyAnonamouse", "enable": True, "privacy": "private"},
    {"id": 2, "name": "AudioBookBay", "enable": True, "privacy": "public"},
    {"id": 3, "name": "NZBgeek", "enable": True, "privacy": "private"},
]
_INDEXER_NAMES = {i["id"]: i["name"] for i in INDEXERS}


def _dotted(text: str) -> str:
    return ".".join(re.findall(r"[A-Za-z0-9]+", text))


def releases(book: StubBook) -> list[dict[str, object]]:
    author = book["authors"][0] if book["authors"] else "Unknown"
    narrator = book["narrators"][0] if book["narrators"] else "Unknown"
    title = book["title"]
    seconds = max(book["runtime_length_min"], 1) * 60
    now = datetime.now().replace(microsecond=0)

    def release(
        n: int,
        name: str,
        kbps: int,
        indexer: int,
        days_ago: int,
        seeders: int | None = None,
        grabs: int | None = None,
        flags: list[str] | None = None,
    ) -> dict[str, object]:
        result: dict[str, object] = {
            "guid": f"earr-readme-{n}-{_dotted(title)}",
            "indexerId": indexer,
            "indexer": _INDEXER_NAMES[indexer],
            "title": name,
            "size": kbps * 1000 // 8 * seconds,
            "indexerFlags": flags or [],
            "publishDate": (now - timedelta(days=days_ago)).isoformat(),
        }
        if grabs is not None:
            result |= {"protocol": "usenet", "grabs": grabs}
        else:
            result |= {"protocol": "torrent", "seeders": seeders, "leechers": 2}
        return result

    return [
        release(
            1,
            f"{author} - {title} - Read by {narrator} [M4B 64kbps]",
            64,
            indexer=1,
            days_ago=410,
            seeders=48,
            flags=["Freeleech"],
        ),
        release(
            2,
            f"{title} - {author} (read by {narrator}) [MP3 128kbps]",
            128,
            indexer=2,
            days_ago=220,
            seeders=19,
        ),
        release(
            3,
            f"{_dotted(author)}.{_dotted(title)}.Unabridged.M4B-AUDIOBOOKS",
            80,
            indexer=3,
            days_ago=35,
            grabs=37,
        ),
        release(
            4,
            f"{author} - {title} [FLAC]",
            1100,
            indexer=2,
            days_ago=90,
            seeders=6,
        ),
        release(5, f"{title} [M4B]", 64, indexer=1, days_ago=12, seeders=1),
        release(
            6,
            "Kings of the Wyld - Nicholas Eames [M4B]",
            64,
            indexer=2,
            days_ago=600,
            seeders=25,
        ),
    ]


class ProwlarrStub:
    """Serves the two Prowlarr endpoints Earr uses, from a background thread."""

    def __init__(self, port: int, books: list[StubBook]):
        self.port: int = port
        self.books: dict[str, StubBook] = {b["title"].lower(): b for b in books}
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._ready: threading.Event = threading.Event()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    async def _search(self, request: web.Request) -> web.Response:
        if request.method == "POST":
            # a grab: Earr only checks that it succeeded
            return web.json_response({})
        book = self.books.get(request.query.get("query", "").strip().lower())
        return web.json_response(releases(book) if book else [])

    async def _indexers(self, _: web.Request) -> web.Response:
        return web.json_response(INDEXERS)

    async def _serve(self) -> web.AppRunner:
        app = web.Application()
        _ = app.router.add_route("*", "/api/v1/search", self._search)
        _ = app.router.add_get("/api/v1/indexer", self._indexers)
        runner = web.AppRunner(app)
        await runner.setup()
        await web.TCPSite(runner, "127.0.0.1", self.port).start()
        return runner

    def _run(self) -> None:
        loop = asyncio.new_event_loop()
        self._loop = loop
        runner = loop.run_until_complete(self._serve())
        self._ready.set()
        try:
            loop.run_forever()
        finally:
            loop.run_until_complete(runner.cleanup())
            loop.close()

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        if not self._ready.wait(10):
            raise RuntimeError("Prowlarr stub did not start")

    def stop(self) -> None:
        if self._loop and self._thread:
            _ = self._loop.call_soon_threadsafe(self._loop.stop)
            self._thread.join(10)
