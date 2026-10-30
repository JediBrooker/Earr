"""Transmission, Deluge and NZBGet, each against a fake server that answers the
way the real one does."""

import json
from typing import Any

from aiohttp import ClientSession, web

from app.internal.download_clients.abstract import DownloadState
from app.internal.download_clients.deluge import DelugeClient
from app.internal.download_clients.nzbget import NzbgetClient
from app.internal.download_clients.transmission import TransmissionClient

HASH = "abc123def456abc123def456abc123def456abcd"


def transmission_routes(torrents: list[dict[str, Any]], calls: list[dict[str, Any]]):
    async def rpc(request: web.Request) -> web.Response:
        # the handshake every Transmission client has to do first
        if request.headers.get("X-Transmission-Session-Id") != "sid-1":
            return web.Response(
                status=409, headers={"X-Transmission-Session-Id": "sid-1"}
            )
        body = await request.json()
        calls.append(body)
        method = body["method"]
        if method == "session-get":
            return web.json_response(
                {"result": "success", "arguments": {"version": "4.0.5"}}
            )
        if method == "torrent-get":
            ids = body["arguments"].get("ids")
            found = [t for t in torrents if not ids or t["hashString"] in ids]
            return web.json_response(
                {"result": "success", "arguments": {"torrents": found}}
            )
        return web.json_response({"result": "success", "arguments": {}})

    return [web.post("/transmission/rpc", rpc)]


class TestTransmission:
    async def test_connects_after_the_session_handshake(self, serve: Any):
        url = await serve(transmission_routes([], []))
        async with ClientSession() as cs:
            ok, message = await TransmissionClient(url).test(cs)
        assert ok and message == "Connected to Transmission 4.0.5"

    async def test_a_seeding_torrent_is_complete_with_its_path(self, serve: Any):
        url = await serve(
            transmission_routes(
                [
                    {
                        "hashString": HASH,
                        "name": "The Way of Kings",
                        "percentDone": 1,
                        "leftUntilDone": 0,
                        "status": 6,
                        "downloadDir": "/data/torrents",
                        "error": 0,
                        "totalSize": 900,
                        "rateDownload": 0,
                        "eta": -1,
                    }
                ],
                [],
            )
        )
        async with ClientSession() as cs:
            info = await TransmissionClient(url).find(cs, client_id=HASH.upper())

        assert info is not None
        assert info.state == DownloadState.completed
        assert info.path == "/data/torrents/The Way of Kings"
        assert info.status == "seeding"

    async def test_a_tracker_error_is_not_a_failure_but_a_local_one_is(
        self, serve: Any
    ):
        torrents = [
            {
                "hashString": "aaa",
                "name": "A",
                "percentDone": 0.3,
                "status": 4,
                "error": 2,
                "errorString": "tracker down",
                "eta": 120,
            },
            {
                "hashString": "bbb",
                "name": "B",
                "percentDone": 0.3,
                "status": 0,
                "error": 3,
                "errorString": "No data found",
            },
        ]
        url = await serve(transmission_routes(torrents, []))
        async with ClientSession() as cs:
            client = TransmissionClient(url)
            a = await client.find(cs, client_id="aaa")
            b = await client.find(cs, client_id="bbb")

        assert a is not None and a.state == DownloadState.downloading and a.eta == 120
        assert b is not None and b.state == DownloadState.failed
        assert b.error == "No data found"

    async def test_removing_deletes_the_data(self, serve: Any):
        calls: list[dict[str, Any]] = []
        url = await serve(
            transmission_routes([{"hashString": HASH, "name": "X"}], calls)
        )
        async with ClientSession() as cs:
            assert await TransmissionClient(url).remove(cs, client_id=HASH)

        assert calls[-1] == {
            "method": "torrent-remove",
            "arguments": {"ids": [HASH], "delete-local-data": True},
        }

    async def test_the_label_is_set_by_hash(self, serve: Any):
        calls: list[dict[str, Any]] = []
        url = await serve(transmission_routes([], calls))
        async with ClientSession() as cs:
            assert await TransmissionClient(url).set_category(
                cs, "audiobooks", client_id=HASH
            )
        assert calls[-1]["arguments"] == {"ids": [HASH], "labels": ["audiobooks"]}


def deluge_routes(
    torrents: dict[str, dict[str, Any]],
    calls: list[tuple[str, list[Any]]],
    *,
    connected: bool = True,
    password: str = "deluge",
):
    state = {"connected": connected}

    async def rpc(request: web.Request) -> web.Response:
        body = json.loads(await request.text())
        method, params = body["method"], body["params"]
        calls.append((method, params))
        result: Any = None
        if method == "auth.login":
            result = params == [password]
            return web.json_response(
                {"result": result, "error": None, "id": body["id"]},
                headers={"Set-Cookie": "_session_id=xyz; Path=/json"},
            )
        if "_session_id=xyz" not in request.headers.get("Cookie", ""):
            return web.json_response(
                {
                    "result": None,
                    "error": {"message": "Not authenticated"},
                    "id": body["id"],
                }
            )
        if method == "web.connected":
            result = state["connected"]
        elif method == "web.get_hosts":
            result = [["host-1", "127.0.0.1", 58846, "localclient"]]
        elif method == "web.connect":
            state["connected"] = True
        elif method == "daemon.info":
            result = "2.1.1"
        elif method == "core.get_torrents_status":
            wanted = params[0].get("id")
            result = {k: v for k, v in torrents.items() if not wanted or k in wanted}
        elif method == "label.get_labels":
            result = ["audiobooks", "tv"]
        elif method in ("core.remove_torrent",):
            result = True
        return web.json_response({"result": result, "error": None, "id": body["id"]})

    return [web.post("/json", rpc)]


class TestDeluge:
    async def test_connects_the_web_ui_to_a_daemon_when_needed(self, serve: Any):
        calls: list[tuple[str, list[Any]]] = []
        url = await serve(deluge_routes({}, calls, connected=False))
        async with ClientSession() as cs:
            ok, message = await DelugeClient(url, "deluge").test(cs)

        assert ok and message == "Connected to Deluge 2.1.1"
        assert ("web.connect", ["host-1"]) in calls

    async def test_a_wrong_password_is_reported(self, serve: Any):
        url = await serve(deluge_routes({}, []))
        async with ClientSession() as cs:
            ok, _ = await DelugeClient(url, "wrong").test(cs)
        assert not ok

    async def test_finds_a_torrent_by_hash(self, serve: Any):
        torrents = {
            HASH: {
                "hash": HASH,
                "name": "Mistborn",
                "state": "Downloading",
                "progress": 42.0,
                "total_size": 1000,
                "download_payload_rate": 50,
                "eta": 300,
                "save_path": "/downloads",
                "message": "OK",
            }
        }
        url = await serve(deluge_routes(torrents, []))
        async with ClientSession() as cs:
            info = await DelugeClient(url, "deluge").find(cs, client_id=HASH.upper())

        assert info is not None
        assert info.state == DownloadState.downloading
        assert round(info.progress, 2) == 0.42
        assert info.path == "/downloads/Mistborn"

    async def test_labels_come_from_the_label_plugin(self, serve: Any):
        url = await serve(deluge_routes({}, []))
        async with ClientSession() as cs:
            assert await DelugeClient(url, "deluge").list_categories(cs) == [
                "audiobooks",
                "tv",
            ]

    async def test_removing_takes_the_data_too(self, serve: Any):
        calls: list[tuple[str, list[Any]]] = []
        url = await serve(deluge_routes({HASH: {"name": "X"}}, calls))
        async with ClientSession() as cs:
            assert await DelugeClient(url, "deluge").remove(cs, client_id=HASH)
        assert ("core.remove_torrent", [HASH, True]) in calls


def nzbget_routes(
    history: list[dict[str, Any]],
    queue: list[dict[str, Any]],
    calls: list[tuple[str, list[Any]]],
):
    async def rpc(request: web.Request) -> web.Response:
        if request.headers.get("Authorization") != "Basic bnpiZ2V0OnRlZ2J6bg==":
            return web.Response(status=401)
        body = await request.json()
        method, params = body["method"], body["params"]
        calls.append((method, params))
        result: Any = True
        if method == "version":
            result = "24.3"
        elif method == "history":
            result = history
        elif method == "listgroups":
            result = queue
        elif method == "config":
            result = [
                {"Name": "Category1.Name", "Value": "Books"},
                {"Name": "Category1.DestDir", "Value": "/x"},
                {"Name": "Category2.Name", "Value": "Audiobooks"},
            ]
        return web.json_response({"version": "1.1", "result": result})

    return [web.post("/jsonrpc", rpc)]


class TestNzbget:
    async def test_needs_the_login(self, serve: Any):
        url = await serve(nzbget_routes([], [], []))
        async with ClientSession() as cs:
            ok, message = await NzbgetClient(url, "nzbget", "tegbzn").test(cs)
            bad, _ = await NzbgetClient(url, "nzbget", "nope").test(cs)
        assert ok and message == "Connected to NZBGet 24.3"
        assert not bad

    async def test_a_finished_job_is_found_in_history(self, serve: Any):
        history = [
            {
                "NZBID": 7,
                "Name": "Brandon.Sanderson-The.Way.of.Kings",
                "Status": "SUCCESS/ALL",
                "FinalDir": "/downloads/Books/The Way of Kings",
                "FileSizeMB": 900,
            }
        ]
        url = await serve(nzbget_routes(history, [], []))
        async with ClientSession() as cs:
            info = await NzbgetClient(url, "nzbget", "tegbzn").find(
                cs, name="Brandon Sanderson The Way of Kings"
            )

        assert info is not None
        assert info.state == DownloadState.completed
        assert info.path == "/downloads/Books/The Way of Kings"

    async def test_a_failed_job_is_a_failure(self, serve: Any):
        history = [{"NZBID": 8, "Name": "Broken", "Status": "FAILURE/PAR"}]
        url = await serve(nzbget_routes(history, [], []))
        async with ClientSession() as cs:
            info = await NzbgetClient(url, "nzbget", "tegbzn").find(cs, client_id="8")
        assert info is not None and info.state == DownloadState.failed
        assert info.error == "failure/par"

    async def test_a_queued_job_reports_progress(self, serve: Any):
        queue = [
            {
                "NZBID": 9,
                "NZBName": "Mistborn",
                "Status": "DOWNLOADING",
                "FileSizeMB": 1000,
                "RemainingSizeMB": 250,
            }
        ]
        url = await serve(nzbget_routes([], queue, []))
        async with ClientSession() as cs:
            info = await NzbgetClient(url, "nzbget", "tegbzn").find(cs, name="Mistborn")
        assert info is not None and info.state == DownloadState.downloading
        assert info.progress == 0.75

    async def test_categories_and_removal(self, serve: Any):
        calls: list[tuple[str, list[Any]]] = []
        queue = [{"NZBID": 9, "NZBName": "Mistborn", "Status": "QUEUED"}]
        url = await serve(nzbget_routes([], queue, calls))
        async with ClientSession() as cs:
            client = NzbgetClient(url, "nzbget", "tegbzn")
            assert await client.list_categories(cs) == ["Audiobooks", "Books"]
            assert await client.set_category(cs, "Audiobooks", name="Mistborn")
            assert await client.remove(cs, name="Mistborn")

        assert ("editqueue", ["GroupApplyCategory", "Audiobooks", [9]]) in calls
        assert ("editqueue", ["GroupFinalDelete", "", [9]]) in calls
