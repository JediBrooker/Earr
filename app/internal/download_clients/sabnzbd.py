"""SABnzbd JSON API.

Unlike a torrent there is no hash to key off, and ABR never learns the nzo_id
that SABnzbd assigns, so jobs are matched by name. That is still better than
reading the filesystem: the name is SABnzbd's own record of what it was given,
and the completed path comes back with it rather than being inferred.

An in-progress job lives in the queue and a finished one moves to history, so
both are consulted.
"""

import posixpath
from typing import cast
from urllib.parse import urljoin

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
"""SABnzbd strips and tidies job names, so an exact match is not guaranteed."""


def _s(data: JsonObject, key: str) -> str:
    value = data.get(key)
    return str(value) if value is not None else ""


def _slots(payload: JsonObject, section: str) -> list[JsonObject]:
    block = payload.get(section)
    if not isinstance(block, dict):
        return []
    slots = cast(JsonObject, block).get("slots")
    if not isinstance(slots, list):
        return []
    return [s for s in cast(list[object], slots) if isinstance(s, dict)]


class SabnzbdClient(DownloadClient):
    name: str = "SABnzbd"

    def __init__(self, base_url: str, api_key: str):
        self.base_url: str = base_url.rstrip("/")
        self.api_key: str = api_key

    def _url(self) -> str:
        return urljoin(self.base_url + "/", posixpath.join("api"))

    async def _call(
        self, client_session: ClientSession, mode: str, **extra: str
    ) -> JsonObject | None:
        params = {"mode": mode, "output": "json", "apikey": self.api_key, **extra}
        try:
            async with client_session.get(
                self._url(), params=params, headers={"User-Agent": USER_AGENT}
            ) as response:
                if not response.ok:
                    logger.warning(
                        "SABnzbd: request failed", mode=mode, status=response.status
                    )
                    return None
                return cast(JsonObject, await response.json())
        except Exception as e:
            logger.warning("SABnzbd: request failed", mode=mode, error=str(e))
            return None

    @override
    async def test(self, client_session: ClientSession) -> tuple[bool, str]:
        payload = await self._call(client_session, "version")
        if payload is None:
            return False, "Could not reach SABnzbd. Check the URL."
        if "error" in payload:
            return False, _s(payload, "error")
        version = _s(payload, "version")
        if not version:
            return False, "Unexpected response. Check the API key."
        return True, f"Connected to SABnzbd {version}"

    @override
    async def set_category(
        self,
        client_session: ClientSession,
        category: str,
        *,
        client_id: str | None = None,
        name: str | None = None,
    ) -> bool:
        """Only works while the job is still in the queue; SABnzbd will not
        recategorise something it has already finished."""
        nzo_id = client_id
        if not nzo_id:
            queue = await self._call(client_session, "queue", limit="200")
            slot = (
                self._pick(_slots(queue, "queue"), None, name, "nzo_id")
                if queue
                else None
            )
            nzo_id = _s(slot, "nzo_id") if slot else None
        if not nzo_id:
            return False

        payload = await self._call(
            client_session, "change_cat", value=nzo_id, value2=category
        )
        return bool(payload and payload.get("status") is True)

    @override
    async def find(
        self,
        client_session: ClientSession,
        *,
        client_id: str | None = None,
        name: str | None = None,
    ) -> DownloadInfo | None:
        # a finished job is in history, so look there first
        history = await self._call(client_session, "history", limit="200")
        if history is not None:
            slot = self._pick(_slots(history, "history"), client_id, name, "nzo_id")
            if slot is not None:
                return self._from_history(slot)

        queue = await self._call(client_session, "queue", limit="200")
        if queue is not None:
            slot = self._pick(_slots(queue, "queue"), client_id, name, "nzo_id")
            if slot is not None:
                return self._from_queue(slot)
        return None

    def _pick(
        self,
        slots: list[JsonObject],
        client_id: str | None,
        name: str | None,
        id_key: str,
    ) -> JsonObject | None:
        if client_id:
            for slot in slots:
                if _s(slot, id_key) == client_id:
                    return slot
        if not name:
            return None

        wanted = utils.default_process(name)
        if not wanted:
            return None
        best: JsonObject | None = None
        best_score = float(NAME_MATCH_THRESHOLD)
        for slot in slots:
            candidate = _s(slot, "name") or _s(slot, "filename")
            score = fuzz.ratio(wanted, utils.default_process(candidate))
            if score >= best_score:
                best = slot
                best_score = score
        return best

    def _from_history(self, slot: JsonObject) -> DownloadInfo:
        status = _s(slot, "status").lower()
        name = _s(slot, "name")
        if status == "completed":
            return DownloadInfo(
                state=DownloadState.completed,
                name=name,
                # where SABnzbd put the finished job
                path=_s(slot, "storage") or None,
                progress=1.0,
            )
        if status in ("failed", "deleted"):
            return DownloadInfo(
                state=DownloadState.failed,
                name=name,
                error=_s(slot, "fail_message") or status,
            )
        # still extracting or repairing
        return DownloadInfo(state=DownloadState.downloading, name=name)

    def _from_queue(self, slot: JsonObject) -> DownloadInfo:
        percentage = _s(slot, "percentage") or "0"
        try:
            progress = float(percentage) / 100
        except ValueError:
            progress = 0.0
        return DownloadInfo(
            state=DownloadState.downloading,
            name=_s(slot, "filename") or _s(slot, "name"),
            progress=progress,
        )
