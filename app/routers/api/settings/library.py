from typing import Annotated, Literal

from aiohttp import ClientSession
from fastapi import APIRouter, Depends, HTTPException, Response, Security
from pydantic import BaseModel
from sqlmodel import Session

from app.internal.auth.authentication import AnyAuth, DetailedUser
from app.internal.download_clients.config import (
    CLIENT_NAMES,
    TORRENT_CLIENTS,
    USENET_CLIENTS,
    ClientId,
    download_client_config,
)
from app.internal.library.config import MIN_SCAN_INTERVAL, library_config
from app.internal.library.naming import (
    TEMPLATE_EXAMPLES,
    TOKEN_DESCRIPTIONS,
    TemplateError,
    validate_template,
)
from app.internal.library.scheduler import reschedule
from app.internal.models import GroupEnum, OrganizeModeEnum
from app.util.connection import get_connection
from app.util.db import get_session

router = APIRouter(prefix="/library")


class ClientSettings(BaseModel):
    id: ClientId
    name: str
    protocol: Literal["torrent", "usenet"]
    enabled: bool
    url: str
    username: str
    secret_set: bool
    """Whether a password (an API key for SABnzbd) is stored. It is never sent back."""
    category: str
    categories: list[str] = []
    """What the client offers. Empty when it is unreachable or off."""

    @property
    def has_username(self) -> bool:
        return self.id not in ("sab", "deluge")

    @property
    def secret_label(self) -> str:
        return "API key" if self.id == "sab" else "password"


class DownloadClientSettings(BaseModel):
    clients: list[ClientSettings]

    def get(self, client_id: ClientId) -> ClientSettings:
        return next(c for c in self.clients if c.id == client_id)


class LibrarySettings(BaseModel):
    enabled: bool
    mode: OrganizeModeEnum
    download_dir: str
    root_dir: str
    folder_template: str
    scan_interval: int
    match_threshold: int
    overwrite: bool
    write_metadata: bool
    download_clients: DownloadClientSettings


def read_settings(session: Session) -> LibrarySettings:
    download_dirs = library_config.get_download_dirs(session)
    root_dir = library_config.get_root_dir(session)
    return LibrarySettings(
        enabled=library_config.get_enabled(session),
        mode=library_config.get_mode(session),
        download_dir="\n".join(str(d) for d in download_dirs),
        root_dir=str(root_dir) if root_dir else "",
        folder_template=library_config.get_folder_template(session),
        scan_interval=library_config.get_scan_interval(session),
        match_threshold=library_config.get_match_threshold(session),
        overwrite=library_config.get_overwrite(session),
        write_metadata=library_config.get_write_metadata(session),
        download_clients=DownloadClientSettings(
            clients=[
                ClientSettings(
                    id=client_id,
                    name=CLIENT_NAMES[client_id],
                    protocol="torrent" if client_id in TORRENT_CLIENTS else "usenet",
                    enabled=download_client_config.get_enabled(session, client_id),
                    url=download_client_config.get_field(session, client_id, "url"),
                    username=download_client_config.get_field(
                        session, client_id, "username"
                    ),
                    # never send secrets back out, only whether one is stored
                    secret_set=bool(
                        download_client_config.get_field(session, client_id, "password")
                    ),
                    category=download_client_config.get_field(
                        session, client_id, "category"
                    ),
                )
                for client_id in (*TORRENT_CLIENTS, *USENET_CLIENTS)
            ]
        ),
    )


@router.get("", response_model=LibrarySettings)
def get_library_settings(
    session: Annotated[Session, Depends(get_session)],
    _: Annotated[DetailedUser, Security(AnyAuth(GroupEnum.admin))],
):
    return read_settings(session)


class LibraryTokens(BaseModel):
    tokens: dict[str, str]
    examples: list[str]


@router.get("/placeholders", response_model=LibraryTokens)
def get_library_placeholders(
    _: Annotated[DetailedUser, Security(AnyAuth(GroupEnum.admin))],
):
    """Lists every placeholder that can be used in the folder structure."""
    return LibraryTokens(tokens=TOKEN_DESCRIPTIONS, examples=TEMPLATE_EXAMPLES)


class UpdateLibrarySettings(BaseModel):
    enabled: bool
    mode: OrganizeModeEnum
    download_dir: str
    root_dir: str
    folder_template: str
    scan_interval: int
    match_threshold: int
    overwrite: bool
    write_metadata: bool


@router.patch("", status_code=204)
def update_library_settings(
    body: UpdateLibrarySettings,
    session: Annotated[Session, Depends(get_session)],
    _: Annotated[DetailedUser, Security(AnyAuth(GroupEnum.admin))],
):
    try:
        validate_template(body.folder_template)
    except TemplateError as e:
        raise HTTPException(status_code=422, detail=str(e))

    if (
        body.enabled
        and not body.download_dir.strip()
        and not download_client_config.any_enabled(session)
    ):
        raise HTTPException(
            status_code=422,
            detail=(
                "Set at least one completed downloads folder, or configure a "
                "download client that can report paths itself"
            ),
        )
    if body.enabled and not body.root_dir.strip():
        raise HTTPException(status_code=422, detail="Library folder is required")
    if body.scan_interval < MIN_SCAN_INTERVAL:
        raise HTTPException(
            status_code=422,
            detail=f"Scan interval has to be at least {MIN_SCAN_INTERVAL} seconds",
        )
    if not 0 <= body.match_threshold <= 100:
        raise HTTPException(
            status_code=422, detail="Match threshold has to be between 0 and 100"
        )

    library_config.set_enabled(session, body.enabled)
    library_config.set_mode(session, body.mode)
    library_config.set_download_dirs(session, body.download_dir)
    library_config.set_root_dir(session, body.root_dir.strip())
    library_config.set_folder_template(session, body.folder_template)
    library_config.set_scan_interval(session, body.scan_interval)
    library_config.set_match_threshold(session, body.match_threshold)
    library_config.set_overwrite(session, body.overwrite)
    library_config.set_write_metadata(session, body.write_metadata)

    reschedule(library_config.get_scan_interval(session))

    return Response(status_code=204)


class ClientUpdate(BaseModel):
    enabled: bool = False
    url: str = ""
    username: str = ""
    secret: str | None = None
    """Password, or API key for SABnzbd. Left out to keep the stored one."""
    category: str = ""


class UpdateDownloadClients(BaseModel):
    clients: dict[ClientId, ClientUpdate]
    """Only the clients listed are changed."""


@router.put("/download-clients", status_code=204)
def update_download_clients(
    body: UpdateDownloadClients,
    session: Annotated[Session, Depends(get_session)],
    _: Annotated[DetailedUser, Security(AnyAuth(GroupEnum.admin))],
):
    for client_id, update in body.clients.items():
        if update.enabled and not update.url.strip():
            raise HTTPException(
                status_code=422, detail=f"{CLIENT_NAMES[client_id]} URL is required"
            )

    for client_id, update in body.clients.items():
        download_client_config.set_enabled(session, client_id, update.enabled)
        download_client_config.set_field(session, client_id, "url", update.url)
        download_client_config.set_field(
            session, client_id, "username", update.username
        )
        download_client_config.set_field(
            session, client_id, "category", update.category
        )
        if update.secret is not None:
            download_client_config.set_field(
                session, client_id, "password", update.secret
            )

    return Response(status_code=204)


class ClientTestResult(BaseModel):
    client: str
    ok: bool
    message: str


@router.post("/download-clients/test", response_model=list[ClientTestResult])
async def test_download_clients(
    session: Annotated[Session, Depends(get_session)],
    client_session: Annotated[ClientSession, Depends(get_connection)],
    _: Annotated[DetailedUser, Security(AnyAuth(GroupEnum.admin))],
):
    """Checks each configured client is reachable and the credentials work."""
    results = await download_client_config.test_all(session, client_session)
    return [ClientTestResult(client=n, ok=ok, message=m) for n, ok, m in results]
