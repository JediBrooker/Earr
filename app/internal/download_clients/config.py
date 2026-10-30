"""Settings for talking to the download clients directly.

Several can be configured at once, which is the normal case: torrents go to
one torrent client and usenet to one usenet client, and a grab is looked up in
whichever match its protocol.
"""

from typing import Literal

from aiohttp import ClientSession
from sqlmodel import Session

from app.internal.download_clients.abstract import DownloadClient
from app.internal.download_clients.deluge import DelugeClient
from app.internal.download_clients.nzbget import NzbgetClient
from app.internal.download_clients.qbittorrent import QBittorrentClient
from app.internal.download_clients.sabnzbd import SabnzbdClient
from app.internal.download_clients.transmission import TransmissionClient
from app.util.cache import StringConfigCache
from app.util.log import logger

ClientId = Literal["qbit", "transmission", "deluge", "sab", "nzbget"]
TORRENT_CLIENTS: tuple[ClientId, ...] = ("qbit", "transmission", "deluge")
USENET_CLIENTS: tuple[ClientId, ...] = ("sab", "nzbget")
CLIENT_NAMES: dict[ClientId, str] = {
    "qbit": "qBittorrent",
    "transmission": "Transmission",
    "deluge": "Deluge",
    "sab": "SABnzbd",
    "nzbget": "NZBGet",
}

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
    "dc_transmission_enabled",
    "dc_transmission_url",
    "dc_transmission_username",
    "dc_transmission_password",
    "dc_transmission_category",
    "dc_deluge_enabled",
    "dc_deluge_url",
    "dc_deluge_password",
    "dc_deluge_category",
    "dc_nzbget_enabled",
    "dc_nzbget_url",
    "dc_nzbget_username",
    "dc_nzbget_password",
    "dc_nzbget_category",
]
ClientField = Literal["enabled", "url", "username", "password", "category"]


class DownloadClientConfig(StringConfigCache[DownloadClientConfigKey]):
    def _key(self, client: ClientId, field: ClientField) -> DownloadClientConfigKey:
        if client == "sab" and field == "password":
            return "dc_sab_api_key"
        return f"dc_{client}_{field}"  # pyright: ignore[reportReturnType]

    def get_field(self, session: Session, client: ClientId, field: ClientField) -> str:
        value = self.get(session, self._key(client, field), "")
        return value.rstrip("/") if field == "url" else value

    def set_field(
        self, session: Session, client: ClientId, field: ClientField, value: str
    ):
        if field == "url":
            value = value.strip().rstrip("/")
        elif field != "password":
            value = value.strip()
        self.set(session, self._key(client, field), value)

    def get_enabled(self, session: Session, client: ClientId) -> bool:
        return bool(self.get_bool(session, self._key(client, "enabled")) or False)

    def set_enabled(self, session: Session, client: ClientId, enabled: bool):
        self.set_bool(session, self._key(client, "enabled"), enabled)

    def build(self, session: Session, client: ClientId) -> DownloadClient | None:
        """The client, if it is turned on and has a URL."""
        url = self.get_field(session, client, "url")
        if not self.get_enabled(session, client) or not url:
            return None
        username = self.get_field(session, client, "username")
        password = self.get_field(session, client, "password")
        match client:
            case "qbit":
                return QBittorrentClient(url, username, password)
            case "sab":
                return SabnzbdClient(url, password)
            case "transmission":
                return TransmissionClient(url, username, password)
            case "deluge":
                return DelugeClient(url, password)
            case "nzbget":
                return NzbgetClient(url, username, password)

    def enabled(self, session: Session) -> list[tuple[ClientId, DownloadClient]]:
        found: list[tuple[ClientId, DownloadClient]] = []
        for client_id in (*TORRENT_CLIENTS, *USENET_CLIENTS):
            client = self.build(session, client_id)
            if client is not None:
                found.append((client_id, client))
        return found

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

        An unknown protocol asks them all, since guessing wrong means falling
        back to matching folder names.
        """
        wanted: list[ClientId] = []
        if protocol in (None, "torrent"):
            wanted.extend(TORRENT_CLIENTS)
        if protocol in (None, "usenet"):
            wanted.extend(USENET_CLIENTS)
        return [c for client_id in wanted if (c := self.build(session, client_id))]

    def any_enabled(self, session: Session) -> bool:
        return bool(self.enabled(session))

    async def test_all(
        self, session: Session, client_session: ClientSession
    ) -> list[tuple[str, bool, str]]:
        results: list[tuple[str, bool, str]] = []
        for _, client in self.enabled(session):
            ok, message = await client.test(client_session)
            results.append((client.name, ok, message))
        return results


download_client_config = DownloadClientConfig()


async def list_categories(
    session: Session, client_session: ClientSession
) -> dict[ClientId, list[str]]:
    """The categories each enabled client offers, for populating the settings page.

    An unreachable client returns nothing rather than raising, so the settings
    page still renders when a client is down.
    """
    return {
        client_id: await client.list_categories(client_session)
        for client_id, client in download_client_config.enabled(session)
    }


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
        candidates = TORRENT_CLIENTS
    elif protocol == "usenet":
        candidates = USENET_CLIENTS
    else:
        return

    for candidate in candidates:
        client = download_client_config.build(session, candidate)
        category = download_client_config.get_field(session, candidate, "category")
        if client is None or not category:
            continue
        ok = await client.set_category(
            client_session, category, client_id=client_id, name=name
        )
        logger.info(
            "Moved grab into category" if ok else "Could not set the category",
            client=client.name,
            category=category,
            release=name,
        )
        if ok:
            return
