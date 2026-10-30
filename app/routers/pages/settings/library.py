from typing import Annotated

from aiohttp import ClientSession
from fastapi import (
    APIRouter,
    BackgroundTasks,
    Depends,
    Form,
    HTTPException,
    Request,
    Security,
)
from sqlmodel import Session, col, desc, select

from app.internal.auth.authentication import EarrAuth, DetailedUser
from app.internal.download_clients.config import (
    TORRENT_CLIENTS,
    USENET_CLIENTS,
    ClientId,
    list_categories,
)
from app.internal.library.config import library_config
from app.internal.library.naming import (
    TEMPLATE_EXAMPLES,
    TOKEN_DESCRIPTIONS,
    TemplateError,
    render_preview,
    validate_template,
)
from app.internal.library.scheduler import lifespan
from app.internal.models import GroupEnum, LibraryImport, OrganizeModeEnum
from app.routers.api.library import scan_now as api_scan_now
from app.routers.api.settings.library import (
    ClientUpdate,
    UpdateDownloadClients,
    UpdateLibrarySettings,
    read_settings,
)
from app.routers.api.settings.library import (
    test_download_clients as api_test_download_clients,
)
from app.routers.api.settings.library import (
    update_download_clients as api_update_download_clients,
)
from app.routers.api.settings.library import (
    update_library_settings as api_update_library_settings,
)
from app.util.connection import get_connection
from app.util.db import get_session
from app.util.templates import catalog_response
from app.util.toast import ToastException

router = APIRouter(prefix="/library", lifespan=lifespan)

RECENT_IMPORT_LIMIT = 15


def recent_imports(session: Session) -> list[LibraryImport]:
    return list(
        session.exec(
            select(LibraryImport)
            .order_by(desc(col(LibraryImport.created_at)))
            .limit(RECENT_IMPORT_LIMIT)
        ).all()
    )


@router.get("")
async def read_library(
    session: Annotated[Session, Depends(get_session)],
    client_session: Annotated[ClientSession, Depends(get_connection)],
    admin_user: Annotated[DetailedUser, Security(EarrAuth(GroupEnum.admin))],
):
    settings = read_settings(session)
    # populated from the clients so the category can be picked rather than typed
    for client_id, categories in (
        await list_categories(session, client_session)
    ).items():
        settings.download_clients.get(client_id).categories = categories
    return catalog_response(
        "Settings.Library.Index",
        user=admin_user,
        settings=settings,
        modes=list(OrganizeModeEnum),
        tokens=TOKEN_DESCRIPTIONS,
        examples=TEMPLATE_EXAMPLES,
        preview=render_preview(settings.folder_template),
        imports=recent_imports(session),
    )


@router.put("/hx-settings")
def update_library(
    mode: Annotated[OrganizeModeEnum, Form()],
    download_dir: Annotated[str, Form()],
    root_dir: Annotated[str, Form()],
    folder_template: Annotated[str, Form()],
    scan_interval: Annotated[int, Form()],
    match_threshold: Annotated[int, Form()],
    session: Annotated[Session, Depends(get_session)],
    admin_user: Annotated[DetailedUser, Security(EarrAuth(GroupEnum.admin))],
    enabled: Annotated[bool, Form()] = False,
    overwrite: Annotated[bool, Form()] = False,
    write_metadata: Annotated[bool, Form()] = False,
):
    try:
        api_update_library_settings(
            UpdateLibrarySettings(
                enabled=enabled,
                mode=mode,
                download_dir=download_dir,
                root_dir=root_dir,
                folder_template=folder_template,
                scan_interval=scan_interval,
                match_threshold=match_threshold,
                overwrite=overwrite,
                write_metadata=write_metadata,
            ),
            session,
            admin_user,
        )
    except HTTPException as e:
        raise ToastException(str(e.detail), "error") from None

    raise ToastException("Library settings updated", "success", cause_refresh=True)


@router.post("/hx-preview")
def preview_template(
    folder_template: Annotated[str, Form()],
    admin_user: Annotated[DetailedUser, Security(EarrAuth(GroupEnum.admin))],
):
    """Renders the sample books so the admin sees the effect while typing."""
    _ = admin_user
    error: str | None = None
    try:
        validate_template(folder_template)
    except TemplateError as e:
        error = str(e)

    return catalog_response(
        "Settings.Library.Preview",
        preview=render_preview(folder_template),
        error=error,
    )


@router.post("/hx-scan")
async def scan_now(
    background_task: BackgroundTasks,
    session: Annotated[Session, Depends(get_session)],
    client_session: Annotated[ClientSession, Depends(get_connection)],
    admin_user: Annotated[DetailedUser, Security(EarrAuth(GroupEnum.admin))],
):
    if not library_config.get_enabled(session):
        raise ToastException("Enable the library organizer first", "error")
    try:
        result = await api_scan_now(
            session, client_session, background_task, admin_user
        )
    except HTTPException as e:
        raise ToastException(str(e.detail), "error") from None

    if result.imported == 0:
        raise ToastException("No new downloads to organize", "info")
    raise ToastException(
        f"Organized {result.imported} download{'s' if result.imported > 1 else ''}",
        "success",
        cause_refresh=True,
    )


@router.put("/hx-download-clients")
async def update_download_clients(
    request: Request,
    session: Annotated[Session, Depends(get_session)],
    admin_user: Annotated[DetailedUser, Security(EarrAuth(GroupEnum.admin))],
):
    # fields are named "<client>_<field>". A browser leaves out unticked boxes
    # and empty inputs, and an empty secret means "keep the stored one"
    form = await request.form()

    def field(client_id: ClientId, name: str) -> str:
        value = form.get(f"{client_id}_{name}")
        return value if isinstance(value, str) else ""

    try:
        api_update_download_clients(
            UpdateDownloadClients(
                clients={
                    client_id: ClientUpdate(
                        enabled=field(client_id, "enabled") in ("on", "true", "1"),
                        url=field(client_id, "url"),
                        username=field(client_id, "username"),
                        secret=field(client_id, "secret") or None,
                        category=field(client_id, "category"),
                    )
                    for client_id in (*TORRENT_CLIENTS, *USENET_CLIENTS)
                }
            ),
            session,
            admin_user,
        )
    except HTTPException as e:
        raise ToastException(str(e.detail), "error") from None

    raise ToastException("Download clients updated", "success", cause_refresh=True)


@router.post("/hx-test-download-clients")
async def test_download_clients(
    session: Annotated[Session, Depends(get_session)],
    client_session: Annotated[ClientSession, Depends(get_connection)],
    admin_user: Annotated[DetailedUser, Security(EarrAuth(GroupEnum.admin))],
):
    results = await api_test_download_clients(session, client_session, admin_user)
    if not results:
        raise ToastException("No download client is enabled", "info")

    failed = [r for r in results if not r.ok]
    summary = "; ".join(f"{r.client}: {r.message}" for r in results)
    raise ToastException(summary, "error" if failed else "success")
