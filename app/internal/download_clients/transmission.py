"""Transmission RPC.

Looked up by info hash, like qBittorrent. Transmission has no categories, only
free-form labels (3.0 and later), so the "category" here is a label: useful for
sorting in the client, though unlike a qBittorrent category it never moves the
files.
"""

import base64
import posixpath
from typing import cast

from aiohttp import ClientSession
from typing_extensions import override

from app.internal.download_clients.abstract import (
    DownloadClient,
    DownloadInfo,
    DownloadState,
)
from app.util.connection import USER_AGENT
from app.util.log import logger

JsonObject = dict[str, object]

_FIELDS = [
    "hashString",
    "name",
    "percentDone",
    "leftUntilDone",
    "rateDownload",
    "eta",
    "status",
    "totalSize",
    "downloadDir",
    "error",
    "errorString",
    "labels",
]
_STATUS = {
    0: "paused",
    1: "waiting to check files",
    2: "checking files",
    3: "queued",
    4: "downloading",
    5: "queued to seed",
    6: "seeding",
}
_LOCAL_ERROR = 3
"""Errors 1 and 2 are tracker warnings and errors, which a torrent survives."""


def _num(data: JsonObject, key: str) -> float:
    value = data.get(key)
    return float(value) if isinstance(value, (int, float)) else 0.0


def _s(data: JsonObject, key: str) -> str:
    value = data.get(key)
    return str(value) if value is not None else ""


class TransmissionClient(DownloadClient):
    name: str = "Transmission"

    def __init__(self, base_url: str, username: str = "", password: str = ""):
        base = base_url.rstrip("/")
        self.rpc_url: str = (
            base if base.endswith("/rpc") else posixpath.join(base, "transmission/rpc")
        )
        self.username: str = username
        self.password: str = password
        self._session_id: str = ""

    def _headers(self) -> dict[str, str]:
        headers = {
            "User-Agent": USER_AGENT,
            "X-Transmission-Session-Id": self._session_id,
        }
        if self.username or self.password:
            token = base64.b64encode(
                f"{self.username}:{self.password}".encode()
            ).decode()
            headers["Authorization"] = f"Basic {token}"
        return headers

    async def _call(
        self, client_session: ClientSession, method: str, arguments: JsonObject
    ) -> JsonObject | None:
        """Transmission answers the first call with 409 and a session id to retry with."""
        body = {"method": method, "arguments": arguments}
        try:
            for _ in range(2):
                async with client_session.post(
                    self.rpc_url, json=body, headers=self._headers()
                ) as response:
                    if response.status == 409:
                        self._session_id = response.headers.get(
                            "X-Transmission-Session-Id", ""
                        )
                        continue
                    if response.status == 401:
                        logger.warning("Transmission: login rejected")
                        return None
                    if not response.ok:
                        logger.warning(
                            "Transmission: request failed",
                            method=method,
                            status=response.status,
                        )
                        return None
                    payload = cast(JsonObject, await response.json(content_type=None))
                    if payload.get("result") != "success":
                        logger.warning(
                            "Transmission: request refused",
                            method=method,
                            result=payload.get("result"),
                        )
                        return None
                    arguments_out = payload.get("arguments")
                    return (
                        cast(JsonObject, arguments_out)
                        if isinstance(arguments_out, dict)
                        else {}
                    )
        except Exception as e:
            logger.warning("Transmission: request failed", method=method, error=str(e))
        return None

    async def _torrents(
        self, client_session: ClientSession, client_id: str | None
    ) -> list[JsonObject] | None:
        arguments: JsonObject = {"fields": _FIELDS}
        if client_id:
            arguments["ids"] = [client_id.lower()]
        result = await self._call(client_session, "torrent-get", arguments)
        if result is None:
            return None
        torrents = result.get("torrents")
        if not isinstance(torrents, list):
            return []
        return [
            cast(JsonObject, t)
            for t in cast(list[object], torrents)
            if isinstance(t, dict)
        ]

    def _pick(
        self, torrents: list[JsonObject], client_id: str | None, name: str | None
    ) -> JsonObject | None:
        for t in torrents:
            if client_id and _s(t, "hashString").lower() == client_id.lower():
                return t
        if name:
            for t in torrents:
                if _s(t, "name") == name:
                    return t
        return None

    @override
    async def test(self, client_session: ClientSession) -> tuple[bool, str]:
        result = await self._call(
            client_session, "session-get", {"fields": ["version"]}
        )
        if result is None:
            return False, "Could not reach Transmission. Check the URL and login."
        return True, f"Connected to Transmission {_s(result, 'version')}".strip()

    @override
    async def list_categories(self, client_session: ClientSession) -> list[str]:
        """Transmission keeps no list of labels, so these are the ones in use."""
        torrents = await self._torrents(client_session, None) or []
        labels: set[str] = set()
        for t in torrents:
            found = t.get("labels")
            if isinstance(found, list):
                labels.update(str(label) for label in cast(list[object], found))
        return sorted(labels)

    @override
    async def set_category(
        self,
        client_session: ClientSession,
        category: str,
        *,
        client_id: str | None = None,
        name: str | None = None,
    ) -> bool:
        torrent_hash = client_id
        if not torrent_hash:
            match = self._pick(
                await self._torrents(client_session, None) or [], None, name
            )
            torrent_hash = _s(match, "hashString") if match else None
        if not torrent_hash:
            return False
        result = await self._call(
            client_session,
            "torrent-set",
            {"ids": [torrent_hash.lower()], "labels": [category]},
        )
        return result is not None

    @override
    async def remove(
        self,
        client_session: ClientSession,
        *,
        client_id: str | None = None,
        name: str | None = None,
        delete_files: bool = True,
    ) -> bool:
        match = self._pick(
            await self._torrents(client_session, client_id) or [], client_id, name
        )
        if match is None:
            return False
        result = await self._call(
            client_session,
            "torrent-remove",
            {"ids": [_s(match, "hashString")], "delete-local-data": delete_files},
        )
        return result is not None

    @override
    async def find(
        self,
        client_session: ClientSession,
        *,
        client_id: str | None = None,
        name: str | None = None,
    ) -> DownloadInfo | None:
        torrents = await self._torrents(client_session, client_id)
        if torrents is None:
            return None
        match = self._pick(torrents, client_id, name)
        return self._to_info(match) if match else None

    def _to_info(self, torrent: JsonObject) -> DownloadInfo:
        status = int(_num(torrent, "status"))
        progress = _num(torrent, "percentDone")
        failed = int(_num(torrent, "error")) == _LOCAL_ERROR
        done = progress >= 1.0 or (
            "leftUntilDone" in torrent and _num(torrent, "leftUntilDone") == 0
        )
        state = (
            DownloadState.failed
            if failed
            else DownloadState.completed
            if done and status not in (1, 2)
            else DownloadState.downloading
        )
        eta = int(_num(torrent, "eta"))
        directory = _s(torrent, "downloadDir")
        name = _s(torrent, "name")
        return DownloadInfo(
            state=state,
            name=name,
            path=posixpath.join(directory, name) if directory and name else None,
            progress=progress,
            error=_s(torrent, "errorString") or None if failed else None,
            size=int(_num(torrent, "totalSize")) or None,
            speed=int(_num(torrent, "rateDownload")),
            eta=eta if eta > 0 else None,
            status="error" if failed else _STATUS.get(status),
        )
