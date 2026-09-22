from typing import Annotated

from fastapi import APIRouter, BackgroundTasks, Depends, Form, HTTPException, Security
from sqlmodel import Session, col, desc, select

from app.internal.auth.authentication import ABRAuth, DetailedUser
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
    UpdateLibrarySettings,
    read_settings,
)
from app.routers.api.settings.library import (
    update_library_settings as api_update_library_settings,
)
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
def read_library(
    session: Annotated[Session, Depends(get_session)],
    admin_user: Annotated[DetailedUser, Security(ABRAuth(GroupEnum.admin))],
):
    settings = read_settings(session)
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
    admin_user: Annotated[DetailedUser, Security(ABRAuth(GroupEnum.admin))],
    enabled: Annotated[bool, Form()] = False,
    overwrite: Annotated[bool, Form()] = False,
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
    admin_user: Annotated[DetailedUser, Security(ABRAuth(GroupEnum.admin))],
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
def scan_now(
    background_task: BackgroundTasks,
    session: Annotated[Session, Depends(get_session)],
    admin_user: Annotated[DetailedUser, Security(ABRAuth(GroupEnum.admin))],
):
    if not library_config.get_enabled(session):
        raise ToastException("Enable the library organizer first", "error")
    try:
        result = api_scan_now(session, background_task, admin_user)
    except HTTPException as e:
        raise ToastException(str(e.detail), "error") from None

    if result.imported == 0:
        raise ToastException("No new downloads to organize", "info")
    raise ToastException(
        f"Organized {result.imported} download{'s' if result.imported > 1 else ''}",
        "success",
        cause_refresh=True,
    )
