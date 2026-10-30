"""NZBGet JSON-RPC.

Like SABnzbd there is no hash, so jobs are matched by name against NZBGet's
own record of them: the queue while downloading, the history once finished.
"""

import base64
import posixpath
from typing import cast

from aiohttp import ClientSession
from rapidfuzz import fuzz, utils
from typing_extensions import override

from app.internal.download_clients.abstract import (
    DownloadClient,
    DownloadInfo,
    DownloadState,
)
from app.util.connection import USER_AGENT
from app.util.log import logger

JsonObject = dict[str, object]

NAME_MATCH_THRESHOLD = 85
_DONE = ("SUCCESS", "WARNING/SCRIPT")
"""History statuses that mean the files are there. A failed post-processing
script doesn't change that."""


def _s(data: JsonObject, key: str) -> str:
    value = data.get(key)
    return str(value) if value is not None else ""


def _num(data: JsonObject, key: str) -> float:
    value = data.get(key)
    return float(value) if isinstance(value, (int, float)) else 0.0


class NzbgetClient(DownloadClient):
    name: str = "NZBGet"

    def __init__(self, base_url: str, username: str = "", password: str = ""):
        self.url: str = posixpath.join(base_url.rstrip("/"), "jsonrpc")
        self.username: str = username
        self.password: str = password

    def _headers(self) -> dict[str, str]:
        headers = {"User-Agent": USER_AGENT}
        if self.username or self.password:
            token = base64.b64encode(
                f"{self.username}:{self.password}".encode()
            ).decode()
            headers["Authorization"] = f"Basic {token}"
        return headers

    async def _call(
        self, client_session: ClientSession, method: str, params: list[object]
    ) -> object:
        try:
            async with client_session.post(
                self.url,
                json={"version": "1.1", "method": method, "params": params},
                headers=self._headers(),
            ) as response:
                if response.status == 401:
                    logger.warning("NZBGet: login rejected")
                    return None
                if not response.ok:
                    logger.warning(
                        "NZBGet: request failed", method=method, status=response.status
                    )
                    return None
                payload = cast(JsonObject, await response.json(content_type=None))
        except Exception as e:
            logger.warning("NZBGet: request failed", method=method, error=str(e))
            return None
        if payload.get("error"):
            logger.warning("NZBGet: request refused", method=method)
            return None
        return payload.get("result")

    async def _list(
        self, client_session: ClientSession, method: str, params: list[object]
    ) -> list[JsonObject] | None:
        result = await self._call(client_session, method, params)
        if result is None:
            return None
        if not isinstance(result, list):
            return []
        return [
            cast(JsonObject, item)
            for item in cast(list[object], result)
            if isinstance(item, dict)
        ]

    def _pick(
        self, items: list[JsonObject], client_id: str | None, name: str | None
    ) -> JsonObject | None:
        if client_id:
            for item in items:
                if _s(item, "NZBID") == client_id:
                    return item
        if not name:
            return None
        wanted = utils.default_process(name)
        if not wanted:
            return None
        best: JsonObject | None = None
        best_score = float(NAME_MATCH_THRESHOLD)
        for item in items:
            candidate = _s(item, "NZBName") or _s(item, "Name")
            score = fuzz.ratio(wanted, utils.default_process(candidate))
            if score >= best_score:
                best, best_score = item, score
        return best

    @override
    async def test(self, client_session: ClientSession) -> tuple[bool, str]:
        version = await self._call(client_session, "version", [])
        if version is None:
            return False, "Could not reach NZBGet. Check the URL and login."
        return True, f"Connected to NZBGet {version}"

    @override
    async def list_categories(self, client_session: ClientSession) -> list[str]:
        config = await self._list(client_session, "config", [])
        if not config:
            return []
        return sorted(
            _s(entry, "Value")
            for entry in config
            if _s(entry, "Name").startswith("Category")
            and _s(entry, "Name").endswith(".Name")
            and _s(entry, "Value")
        )

    @override
    async def set_category(
        self,
        client_session: ClientSession,
        category: str,
        *,
        client_id: str | None = None,
        name: str | None = None,
    ) -> bool:
        queue = await self._list(client_session, "listgroups", [0]) or []
        item = self._pick(queue, client_id, name)
        if item is None:
            return False
        result = await self._call(
            client_session,
            "editqueue",
            ["GroupApplyCategory", category, [int(_num(item, "NZBID"))]],
        )
        return result is True

    @override
    async def remove(
        self,
        client_session: ClientSession,
        *,
        client_id: str | None = None,
        name: str | None = None,
        delete_files: bool = True,
    ) -> bool:
        steps: tuple[tuple[str, list[object], str], ...] = (
            ("listgroups", [0], "GroupFinalDelete" if delete_files else "GroupDelete"),
            (
                "history",
                [False],
                "HistoryFinalDelete" if delete_files else "HistoryDelete",
            ),
        )
        for method, params, command in steps:
            item = self._pick(
                await self._list(client_session, method, params) or [], client_id, name
            )
            if item is None:
                continue
            result = await self._call(
                client_session, "editqueue", [command, "", [int(_num(item, "NZBID"))]]
            )
            return result is True
        return False

    @override
    async def find(
        self,
        client_session: ClientSession,
        *,
        client_id: str | None = None,
        name: str | None = None,
    ) -> DownloadInfo | None:
        history = await self._list(client_session, "history", [False])
        if history is not None:
            item = self._pick(history, client_id, name)
            if item is not None:
                return self._from_history(item)
        queue = await self._list(client_session, "listgroups", [0])
        if queue is not None:
            item = self._pick(queue, client_id, name)
            if item is not None:
                return self._from_queue(item)
        return None

    def _from_history(self, item: JsonObject) -> DownloadInfo:
        status = _s(item, "Status").upper()
        size = int(_num(item, "FileSizeMB") * 1024 * 1024) or None
        if status.startswith(_DONE):
            return DownloadInfo(
                state=DownloadState.completed,
                name=_s(item, "Name"),
                path=_s(item, "FinalDir") or _s(item, "DestDir") or None,
                progress=1.0,
                size=size,
                status="completed",
            )
        return DownloadInfo(
            state=DownloadState.failed,
            name=_s(item, "Name"),
            error=status.lower() or "failed",
            size=size,
            status=status.lower() or None,
        )

    def _from_queue(self, item: JsonObject) -> DownloadInfo:
        total = _num(item, "FileSizeMB")
        remaining = _num(item, "RemainingSizeMB")
        progress = (1 - remaining / total) if total else 0.0
        return DownloadInfo(
            state=DownloadState.downloading,
            name=_s(item, "NZBName"),
            progress=max(0.0, min(progress, 1.0)),
            size=int(total * 1024 * 1024) or None,
            status=_s(item, "Status").lower().replace("_", " ") or None,
        )
