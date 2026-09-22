"""Talking to qBittorrent and SABnzbd.

Both are exercised against a fake HTTP server serving recorded response shapes,
so the parsing and the state mapping are tested for real rather than mocked at
the client boundary.
"""

import json
from typing import Any, Callable

import pytest
from aiohttp import ClientSession, web

from app.internal.download_clients.abstract import DownloadState
from app.internal.download_clients.qbittorrent import QBittorrentClient
from app.internal.download_clients.sabnzbd import SabnzbdClient

HASH = "abc123def456abc123def456abc123def456abcd"


@pytest.fixture
async def serve() -> Any:
    """Starts a throwaway aiohttp server and returns its base url."""
    servers: list[Any] = []

    async def _start(routes: list[Any]) -> str:
        app = web.Application()
        app.add_routes(routes)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        servers.append(runner)
        port = runner.addresses[0][1]
        return f"http://127.0.0.1:{port}"

    yield _start
    for runner in servers:
        await runner.cleanup()


def qbit_routes(torrents: list[dict[str, object]], version: str = "v4.6.0") -> list[Any]:
    async def login(request: web.Request) -> web.Response:
        return web.Response(text="Ok.", headers={"Set-Cookie": "SID=test; path=/"})

    async def info(request: web.Request) -> web.Response:
        hashes = request.query.get("hashes")
        data = torrents
        if hashes:
            data = [t for t in torrents if str(t.get("hash", "")).lower() == hashes]
        return web.json_response(data)

    async def ver(request: web.Request) -> web.Response:
        return web.Response(text=version)

    return [
        web.post("/api/v2/auth/login", login),
        web.get("/api/v2/torrents/info", info),
        web.get("/api/v2/app/version", ver),
    ]


class TestQBittorrent:
    async def test_a_finished_torrent_reports_its_content_path(self, serve: Any):
        url = await serve(
            qbit_routes([{
                "hash": HASH, "name": "03 Silverthorn", "state": "stalledUP",
                "progress": 1.0, "content_path": "/data/torrents/prowlarr/03 Silverthorn",
                "save_path": "/data/torrents/prowlarr/",
            }])
        )
        async with ClientSession() as cs:
            info = await QBittorrentClient(url).find(cs, client_id=HASH)

        assert info is not None
        assert info.state == DownloadState.completed
        assert info.path == "/data/torrents/prowlarr/03 Silverthorn"

    async def test_the_hash_lookup_is_case_insensitive(self, serve: Any):
        url = await serve(
            qbit_routes([{
                "hash": HASH, "name": "x", "state": "uploading", "progress": 1.0,
                "content_path": "/data/x",
            }])
        )
        async with ClientSession() as cs:
            info = await QBittorrentClient(url).find(cs, client_id=HASH.upper())

        assert info is not None
        assert info.state == DownloadState.completed

    @pytest.mark.parametrize(
        "state,progress,expected",
        [
            ("downloading", 0.4, DownloadState.downloading),
            ("stalledDL", 0.1, DownloadState.downloading),
            ("metaDL", 0.0, DownloadState.downloading),
            ("uploading", 1.0, DownloadState.completed),
            ("pausedUP", 1.0, DownloadState.completed),
            ("queuedUP", 1.0, DownloadState.completed),
            ("error", 0.5, DownloadState.failed),
            ("missingFiles", 1.0, DownloadState.failed),
        ],
    )
    async def test_state_mapping(
        self, serve: Any, state: str, progress: float, expected: DownloadState
    ):
        url = await serve(
            qbit_routes([{
                "hash": HASH, "name": "x", "state": state, "progress": progress,
                "content_path": "/data/x",
            }])
        )
        async with ClientSession() as cs:
            info = await QBittorrentClient(url).find(cs, client_id=HASH)

        assert info is not None
        assert info.state == expected

    async def test_an_unknown_hash_is_not_found(self, serve: Any):
        url = await serve(qbit_routes([]))
        async with ClientSession() as cs:
            assert await QBittorrentClient(url).find(cs, client_id=HASH) is None

    async def test_test_connection_reports_the_version(self, serve: Any):
        url = await serve(qbit_routes([], version="v4.6.5"))
        async with ClientSession() as cs:
            ok, message = await QBittorrentClient(url).test(cs)

        assert ok is True
        assert "4.6.5" in message

    async def test_an_unreachable_client_fails_cleanly(self):
        async with ClientSession() as cs:
            ok, message = await QBittorrentClient("http://127.0.0.1:9").test(cs)

        assert ok is False
        assert message


def sab_routes(history: list[dict[str, object]], queue: list[dict[str, object]]) -> list[Any]:
    async def api(request: web.Request) -> web.Response:
        mode = request.query.get("mode")
        if mode == "version":
            return web.json_response({"version": "4.2.0"})
        if mode == "history":
            return web.json_response({"history": {"slots": history}})
        if mode == "queue":
            return web.json_response({"queue": {"slots": queue}})
        return web.json_response({"error": "unknown mode"})

    return [web.get("/api", api)]


class TestSabnzbd:
    async def test_a_completed_job_reports_its_storage_path(self, serve: Any):
        url = await serve(sab_routes(
            history=[{
                "nzo_id": "SABnzbd_nzo_1", "name": "Some Audiobook",
                "status": "Completed", "storage": "/data/usenet/complete/Some Audiobook",
            }],
            queue=[],
        ))
        async with ClientSession() as cs:
            info = await SabnzbdClient(url, "key").find(cs, name="Some Audiobook")

        assert info is not None
        assert info.state == DownloadState.completed
        assert info.path == "/data/usenet/complete/Some Audiobook"

    async def test_a_failed_job_carries_its_message(self, serve: Any):
        url = await serve(sab_routes(
            history=[{
                "nzo_id": "x", "name": "Broken", "status": "Failed",
                "fail_message": "Unpacking failed",
            }],
            queue=[],
        ))
        async with ClientSession() as cs:
            info = await SabnzbdClient(url, "key").find(cs, name="Broken")

        assert info is not None
        assert info.state == DownloadState.failed
        assert info.error == "Unpacking failed"

    async def test_a_queued_job_is_still_downloading(self, serve: Any):
        url = await serve(sab_routes(
            history=[],
            queue=[{"nzo_id": "q1", "filename": "In Progress", "percentage": "42"}],
        ))
        async with ClientSession() as cs:
            info = await SabnzbdClient(url, "key").find(cs, name="In Progress")

        assert info is not None
        assert info.state == DownloadState.downloading
        assert info.progress == pytest.approx(0.42)

    async def test_history_wins_over_the_queue(self, serve: Any):
        """A job that finished while a stale queue entry lingers is finished."""
        url = await serve(sab_routes(
            history=[{"nzo_id": "n", "name": "Book", "status": "Completed", "storage": "/done"}],
            queue=[{"nzo_id": "n", "filename": "Book", "percentage": "80"}],
        ))
        async with ClientSession() as cs:
            info = await SabnzbdClient(url, "key").find(cs, name="Book")

        assert info is not None
        assert info.state == DownloadState.completed

    async def test_a_tidied_name_still_matches(self, serve: Any):
        """SABnzbd rewrites job names, so matching is fuzzy."""
        url = await serve(sab_routes(
            history=[{
                "nzo_id": "n", "name": "Silverthorn by Raymond E Feist ENG M4B",
                "status": "Completed", "storage": "/done/Silverthorn",
            }],
            queue=[],
        ))
        async with ClientSession() as cs:
            info = await SabnzbdClient(url, "key").find(
                cs, name="Silverthorn by Raymond E Feist [ENG / M4B]"
            )

        assert info is not None
        assert info.path == "/done/Silverthorn"

    async def test_an_unrelated_job_is_not_matched(self, serve: Any):
        url = await serve(sab_routes(
            history=[{"nzo_id": "n", "name": "Something Else Entirely",
                      "status": "Completed", "storage": "/done"}],
            queue=[],
        ))
        async with ClientSession() as cs:
            assert await SabnzbdClient(url, "key").find(cs, name="Silverthorn") is None

    async def test_test_connection(self, serve: Any):
        url = await serve(sab_routes([], []))
        async with ClientSession() as cs:
            ok, message = await SabnzbdClient(url, "key").test(cs)

        assert ok is True
        assert "4.2.0" in message
