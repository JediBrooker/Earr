"""Settings for talking to the download clients directly.

Both can be configured at once, which is the normal case: torrents go to
qBittorrent and usenet to SABnzbd, and a grab lands in whichever matches its
protocol.
"""

from typing import Literal

from aiohttp import ClientSession
from sqlmodel import Session

from app.internal.download_clients.abstract import DownloadClient
from app.internal.download_clients.qbittorrent import QBittorrentClient
from app.internal.download_clients.sabnzbd import SabnzbdClient
from app.util.cache import StringConfigCache
from app.util.log import logger

DownloadClientConfigKey = Literal[
    "dc_qbit_enabled",
    "dc_qbit_url",
    "dc_qbit_username",
    "dc_qbit_password",
    "dc_qbit_category",
    "dc_sab_enabled",
    "dc_sab_url",
    "dc_sab_api_key",
    "dc_sab_category",
]


class DownloadClientConfig(StringConfigCache[DownloadClientConfigKey]):
    def get_qbit_enabled(self, session: Session) -> bool:
        return bool(self.get_bool(session, "dc_qbit_enabled") or False)

    def set_qbit_enabled(self, session: Session, enabled: bool):
        self.set_bool(session, "dc_qbit_enabled", enabled)

    def get_qbit_url(self, session: Session) -> str | None:
        value = self.get(session, "dc_qbit_url")
        return value.rstrip("/") if value else None

    def set_qbit_url(self, session: Session, url: str):
        self.set(session, "dc_qbit_url", url.strip().rstrip("/"))

    def get_qbit_username(self, session: Session) -> str:
        return self.get(session, "dc_qbit_username", "")

    def set_qbit_username(self, session: Session, username: str):
        self.set(session, "dc_qbit_username", username.strip())

    def get_qbit_password(self, session: Session) -> str:
        return self.get(session, "dc_qbit_password", "")

    def set_qbit_password(self, session: Session, password: str):
        self.set(session, "dc_qbit_password", password)

    def get_qbit_category(self, session: Session) -> str:
        """Category to move a grab into. Empty leaves Prowlarr's choice alone."""
        return self.get(session, "dc_qbit_category", "")

    def set_qbit_category(self, session: Session, category: str):
        self.set(session, "dc_qbit_category", category.strip())

    def get_sab_enabled(self, session: Session) -> bool:
        return bool(self.get_bool(session, "dc_sab_enabled") or False)

    def set_sab_enabled(self, session: Session, enabled: bool):
        self.set_bool(session, "dc_sab_enabled", enabled)

    def get_sab_url(self, session: Session) -> str | None:
        value = self.get(session, "dc_sab_url")
        return value.rstrip("/") if value else None

    def set_sab_url(self, session: Session, url: str):
        self.set(session, "dc_sab_url", url.strip().rstrip("/"))

    def get_sab_api_key(self, session: Session) -> str:
        return self.get(session, "dc_sab_api_key", "")

    def set_sab_api_key(self, session: Session, api_key: str):
        self.set(session, "dc_sab_api_key", api_key.strip())

    def get_sab_category(self, session: Session) -> str:
        return self.get(session, "dc_sab_category", "")

    def set_sab_category(self, session: Session, category: str):
        self.set(session, "dc_sab_category", category.strip())

    def build_qbit(self, session: Session) -> QBittorrentClient | None:
        url = self.get_qbit_url(session)
        if not self.get_qbit_enabled(session) or not url:
            return None
        return QBittorrentClient(
            url, self.get_qbit_username(session), self.get_qbit_password(session)
        )

    def build_sab(self, session: Session) -> SabnzbdClient | None:
        url = self.get_sab_url(session)
        if not self.get_sab_enabled(session) or not url:
            return None
        return SabnzbdClient(url, self.get_sab_api_key(session))

    def clients_for(
        self, session: Session, protocol: str | None
    ) -> list[DownloadClient]:
        """The clients that could plausibly hold a grab of this protocol.

        An unknown protocol asks both, since guessing wrong means falling back
        to matching folder names.
        """
        clients: list[DownloadClient] = []
        if protocol in (None, "torrent"):
            qbit = self.build_qbit(session)
            if qbit:
                clients.append(qbit)
        if protocol in (None, "usenet"):
            sab = self.build_sab(session)
            if sab:
                clients.append(sab)
        return clients

    def any_enabled(self, session: Session) -> bool:
        return bool(self.build_qbit(session) or self.build_sab(session))

    async def test_all(
        self, session: Session, client_session: ClientSession
    ) -> list[tuple[str, bool, str]]:
        results: list[tuple[str, bool, str]] = []
        for client in (self.build_qbit(session), self.build_sab(session)):
            if client is None:
                continue
            ok, message = await client.test(client_session)
            results.append((client.name, ok, message))
        return results


download_client_config = DownloadClientConfig()


async def apply_category(
    session: Session,
    client_session: ClientSession,
    *,
    protocol: str | None,
    client_id: str | None,
    name: str | None,
) -> None:
    """Moves a freshly grabbed download into the configured category.

    Prowlarr picks the category when it hands a release to the client and its
    API offers no way to override it, so this is done afterwards. Best effort:
    a failure is logged and nothing else changes, since the download is already
    running and the library watcher does not depend on where it sits.
    """
    if protocol == "torrent":
        client = download_client_config.build_qbit(session)
        category = download_client_config.get_qbit_category(session)
    elif protocol == "usenet":
        client = download_client_config.build_sab(session)
        category = download_client_config.get_sab_category(session)
    else:
        return

    if client is None or not category:
        return

    ok = await client.set_category(
        client_session, category, client_id=client_id, name=name
    )
    logger.info(
        "Moved grab into category" if ok else "Could not set the category",
        client=client.name,
        category=category,
        release=name,
    )
