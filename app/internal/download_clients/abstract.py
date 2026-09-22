"""Talking to the download client directly, so the watcher does not have to
guess which folder on disk belongs to which grab.

Prowlarr hands a release to a download client and tells us nothing further, so
completion is otherwise inferred by matching the release title against folder
names. That is fragile: a release listed as "Silverthorn by Raymond E Feist
[ENG / M4B]" can arrive as "03 Silverthorn".

Asking the client removes the guesswork. For torrents the info hash is an exact
key. For usenet there is no equivalent, so the name is still matched, but
against the client's own record of the job rather than a directory listing.
"""

from abc import ABC, abstractmethod
from enum import Enum

from aiohttp import ClientSession
from pydantic import BaseModel


class DownloadState(str, Enum):
    downloading = "downloading"
    completed = "completed"
    failed = "failed"
    """Errored, or removed from the client without finishing."""


class DownloadInfo(BaseModel):
    """What a client knows about one job."""

    state: DownloadState
    name: str
    path: str | None = None
    """Where the finished files are. Only meaningful once completed."""
    progress: float = 0.0
    """0 to 1."""
    error: str | None = None


class DownloadClient(ABC):
    """A download client ABR can ask about a grab it handed to Prowlarr."""

    name: str
    """Shown in settings and in logs."""

    @abstractmethod
    async def test(self, client_session: ClientSession) -> tuple[bool, str]:
        """Returns whether the client is reachable, and a message either way."""

    @abstractmethod
    async def find(
        self,
        client_session: ClientSession,
        *,
        client_id: str | None = None,
        name: str | None = None,
    ) -> DownloadInfo | None:
        """Looks a job up by its client side id, falling back to its name.

        Returns None when the client has never heard of it, which is different
        from a job that exists and failed.
        """
