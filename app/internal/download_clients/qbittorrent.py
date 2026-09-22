"""qBittorrent WebUI API v2.

Torrents are looked up by info hash, which ABR already extracts from the
.torrent file when it starts a download, so this is an exact match rather than
a guess. `content_path` is what the client considers the root of the torrent's
data, which is exactly what needs organizing.
"""

import posixpath
from typing import cast
from urllib.parse import urljoin

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

# https://github.com/qbittorrent/qBittorrent/wiki/WebUI-API-(qBittorrent-4.1)
_DONE_STATES = {
    "uploading",
    "stalledUP",
    "pausedUP",
    "stoppedUP",
    "queuedUP",
    "forcedUP",
    "checkingUP",
}
_FAILED_STATES = {"error", "missingFiles"}


def _s(data: JsonObject, key: str) -> str:
    value = data.get(key)
    return str(value) if value is not None else ""


def _f(data: JsonObject, key: str) -> float:
    value = data.get(key)
    try:
        return float(value)  # pyright: ignore[reportArgumentType]
    except TypeError, ValueError:
        return 0.0


class QBittorrentClient(DownloadClient):
    name: str = "qBittorrent"

    def __init__(self, base_url: str, username: str = "", password: str = ""):
        self.base_url: str = base_url.rstrip("/")
        self.username: str = username
        self.password: str = password
        self._cookie: str | None = None

    def _url(self, path: str) -> str:
        return urljoin(self.base_url + "/", posixpath.join("api/v2", path))

    def _headers(self) -> dict[str, str]:
        headers = {"User-Agent": USER_AGENT, "Referer": self.base_url}
        if self._cookie:
            headers["Cookie"] = self._cookie
        return headers

    async def _login(self, client_session: ClientSession) -> bool:
        """qBittorrent allows unauthenticated access from whitelisted hosts, so
        a missing username is not an error."""
        if not self.username:
            return True
        async with client_session.post(
            self._url("auth/login"),
            data={"username": self.username, "password": self.password},
            headers={"User-Agent": USER_AGENT, "Referer": self.base_url},
        ) as response:
            body = (await response.text()).strip()
            if not response.ok or body.lower() == "fails.":
                logger.warning("qBittorrent: login rejected", status=response.status)
                return False
            cookie = response.headers.get("Set-Cookie", "")
            self._cookie = cookie.split(";")[0] if cookie else None
            return True

    @override
    async def test(self, client_session: ClientSession) -> tuple[bool, str]:
        try:
            if not await self._login(client_session):
                return False, "Login rejected. Check the username and password."
            async with client_session.get(
                self._url("app/version"), headers=self._headers()
            ) as response:
                if response.status in (401, 403):
                    return False, "Not authorised. Check the username and password."
                if not response.ok:
                    return False, f"{response.status}: {response.reason}"
                return (
                    True,
                    f"Connected to qBittorrent {(await response.text()).strip()}",
                )
        except Exception as e:
            return False, str(e)

    @override
    async def set_category(
        self,
        client_session: ClientSession,
        category: str,
        *,
        client_id: str | None = None,
        name: str | None = None,
    ) -> bool:
        """Note this can move files: with Auto Torrent Management on, and a save
        path set for the category, qBittorrent relocates the torrent's data."""
        if not client_id:
            info = await self.find(client_session, name=name)
            if info is None:
                return False
            # qBittorrent only accepts hashes here, so without one there is
            # nothing to act on
            return False
        if not await self._login(client_session):
            return False
        try:
            async with client_session.post(
                self._url("torrents/setCategory"),
                data={"hashes": client_id.lower(), "category": category},
                headers=self._headers(),
            ) as response:
                if response.status == 409:
                    logger.warning(
                        "qBittorrent: category does not exist", category=category
                    )
                    return False
                if not response.ok:
                    logger.warning(
                        "qBittorrent: could not set category",
                        status=response.status,
                        category=category,
                    )
                    return False
                return True
        except Exception as e:
            logger.warning("qBittorrent: set category failed", error=str(e))
            return False

    @override
    async def find(
        self,
        client_session: ClientSession,
        *,
        client_id: str | None = None,
        name: str | None = None,
    ) -> DownloadInfo | None:
        if not await self._login(client_session):
            return None

        params: dict[str, str] = {}
        if client_id:
            params["hashes"] = client_id.lower()

        try:
            async with client_session.get(
                self._url("torrents/info"), params=params, headers=self._headers()
            ) as response:
                if not response.ok:
                    logger.warning(
                        "qBittorrent: could not list torrents", status=response.status
                    )
                    return None
                torrents = cast(list[JsonObject], await response.json())
        except Exception as e:
            logger.warning("qBittorrent: request failed", error=str(e))
            return None

        match = self._pick(torrents, client_id, name)
        return self._to_info(match) if match else None

    def _pick(
        self,
        torrents: list[JsonObject],
        client_id: str | None,
        name: str | None,
    ) -> JsonObject | None:
        if client_id:
            wanted = client_id.lower()
            for t in torrents:
                if _s(t, "hash").lower() == wanted:
                    return t
            # the hashes filter already narrowed it, so anything left is ours
            return torrents[0] if torrents and not name else None
        if name:
            for t in torrents:
                if _s(t, "name") == name:
                    return t
        return None

    def _to_info(self, torrent: JsonObject) -> DownloadInfo:
        state = _s(torrent, "state")
        progress = _f(torrent, "progress")

        if state in _FAILED_STATES:
            resolved = DownloadState.failed
        elif state in _DONE_STATES or progress >= 1.0:
            resolved = DownloadState.completed
        else:
            resolved = DownloadState.downloading

        return DownloadInfo(
            state=resolved,
            name=_s(torrent, "name"),
            # content_path points at the torrent's own root, whether that is a
            # folder or a single file; save_path is only the parent directory
            path=_s(torrent, "content_path") or None,
            progress=progress,
            error=state if resolved == DownloadState.failed else None,
        )
