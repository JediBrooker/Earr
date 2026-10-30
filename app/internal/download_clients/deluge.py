"""Deluge through its Web UI's JSON-RPC.

The Web UI is a front end to a daemon, and may need telling which daemon to
talk to, so a session logs in and then connects to the first host if it isn't
connected yet. Categories are labels from Deluge's Label plugin.
"""

import itertools
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

_KEYS = [
    "hash",
    "name",
    "state",
    "progress",
    "total_size",
    "download_payload_rate",
    "eta",
    "save_path",
    "message",
]
_STATUS = {
    "Downloading": "downloading",
    "Seeding": "seeding",
    "Paused": "paused",
    "Checking": "checking files",
    "Queued": "queued",
    "Moving": "moving files",
    "Allocating": "allocating space",
    "Error": "error",
}


class _RpcError(Exception):
    pass


def _num(data: JsonObject, key: str) -> float:
    value = data.get(key)
    return float(value) if isinstance(value, (int, float)) else 0.0


def _s(data: JsonObject, key: str) -> str:
    value = data.get(key)
    return str(value) if value is not None else ""


class DelugeClient(DownloadClient):
    name: str = "Deluge"

    def __init__(self, base_url: str, password: str = ""):
        self.url: str = posixpath.join(base_url.rstrip("/"), "json")
        self.password: str = password
        self._cookie: str | None = None
        self._ids: itertools.count[int] = itertools.count(1)

    async def _raw(
        self, client_session: ClientSession, method: str, params: list[object]
    ) -> object:
        headers = {"User-Agent": USER_AGENT}
        if self._cookie:
            headers["Cookie"] = self._cookie
        async with client_session.post(
            self.url,
            json={"method": method, "params": params, "id": next(self._ids)},
            headers=headers,
        ) as response:
            if not response.ok:
                raise _RpcError(f"{response.status}: {response.reason}")
            cookie = response.headers.get("Set-Cookie")
            if cookie:
                self._cookie = cookie.split(";")[0]
            payload = cast(JsonObject, await response.json(content_type=None))
        error = payload.get("error")
        if error:
            message = (
                _s(cast(JsonObject, error), "message")
                if isinstance(error, dict)
                else str(error)
            )
            raise _RpcError(message or "Deluge returned an error")
        return payload.get("result")

    async def _connect(self, client_session: ClientSession) -> bool:
        """Logs in, and points the Web UI at a daemon if it has none."""
        if not await self._raw(client_session, "auth.login", [self.password]):
            logger.warning("Deluge: login rejected")
            return False
        if await self._raw(client_session, "web.connected", []):
            return True
        hosts = await self._raw(client_session, "web.get_hosts", [])
        if not isinstance(hosts, list) or not hosts:
            logger.warning("Deluge: the Web UI has no daemon to connect to")
            return False
        first = cast(list[object], hosts)[0]
        host_id = cast(list[object], first)[0] if isinstance(first, list) else None
        if host_id is None:
            return False
        _ = await self._raw(client_session, "web.connect", [host_id])
        return True

    async def _call(
        self, client_session: ClientSession, method: str, params: list[object]
    ) -> object:
        try:
            if not await self._connect(client_session):
                return None
            return await self._raw(client_session, method, params)
        except Exception as e:
            logger.warning("Deluge: request failed", method=method, error=str(e))
            return None

    async def _torrents(
        self, client_session: ClientSession, client_id: str | None
    ) -> dict[str, JsonObject] | None:
        filters: JsonObject = {"id": [client_id.lower()]} if client_id else {}
        result = await self._call(
            client_session, "core.get_torrents_status", [filters, _KEYS]
        )
        if result is None:
            return None
        if not isinstance(result, dict):
            return {}
        return {
            str(k): cast(JsonObject, v)
            for k, v in cast(dict[object, object], result).items()
            if isinstance(v, dict)
        }

    def _pick(
        self, torrents: dict[str, JsonObject], client_id: str | None, name: str | None
    ) -> tuple[str, JsonObject] | None:
        if client_id and client_id.lower() in torrents:
            return client_id.lower(), torrents[client_id.lower()]
        if name:
            for torrent_id, t in torrents.items():
                if _s(t, "name") == name:
                    return torrent_id, t
        return None

    @override
    async def test(self, client_session: ClientSession) -> tuple[bool, str]:
        try:
            if not await self._connect(client_session):
                return False, "Login rejected, or no Deluge daemon to connect to."
            version = await self._raw(client_session, "daemon.info", [])
        except Exception as e:
            return False, f"Could not reach Deluge: {e}"
        return True, f"Connected to Deluge {version or ''}".strip()

    @override
    async def list_categories(self, client_session: ClientSession) -> list[str]:
        labels = await self._call(client_session, "label.get_labels", [])
        if not isinstance(labels, list):
            return []
        return sorted(str(label) for label in cast(list[object], labels))

    @override
    async def set_category(
        self,
        client_session: ClientSession,
        category: str,
        *,
        client_id: str | None = None,
        name: str | None = None,
    ) -> bool:
        found = self._pick(
            await self._torrents(client_session, client_id) or {}, client_id, name
        )
        if found is None:
            return False
        # the Label plugin only knows lower case labels
        result = await self._call(
            client_session, "label.set_torrent", [found[0], category.lower()]
        )
        return result is None or result is True

    @override
    async def remove(
        self,
        client_session: ClientSession,
        *,
        client_id: str | None = None,
        name: str | None = None,
        delete_files: bool = True,
    ) -> bool:
        found = self._pick(
            await self._torrents(client_session, client_id) or {}, client_id, name
        )
        if found is None:
            return False
        result = await self._call(
            client_session, "core.remove_torrent", [found[0], delete_files]
        )
        return result is True

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
        found = self._pick(torrents, client_id, name)
        return self._to_info(found[1]) if found else None

    def _to_info(self, torrent: JsonObject) -> DownloadInfo:
        state = _s(torrent, "state")
        progress = _num(torrent, "progress") / 100
        if state == "Error":
            resolved = DownloadState.failed
        elif state == "Seeding" or progress >= 1.0:
            resolved = DownloadState.completed
        else:
            resolved = DownloadState.downloading
        name = _s(torrent, "name")
        save_path = _s(torrent, "save_path")
        eta = int(_num(torrent, "eta"))
        return DownloadInfo(
            state=resolved,
            name=name,
            path=posixpath.join(save_path, name) if save_path and name else None,
            progress=min(progress, 1.0),
            error=_s(torrent, "message") or "error"
            if resolved == DownloadState.failed
            else None,
            size=int(_num(torrent, "total_size")) or None,
            speed=int(_num(torrent, "download_payload_rate")),
            eta=eta if eta > 0 else None,
            status=_STATUS.get(state, state.lower() or None),
        )
