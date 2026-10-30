"""The System pages (status, backups, logs), and the counts and health light the
sidebar loads after the page."""

from typing import Annotated, Literal

from aiohttp import ClientSession
from fastapi import (
    APIRouter,
    Depends,
    File,
    Form,
    HTTPException,
    Response,
    Security,
    UploadFile,
)
from fastapi.responses import FileResponse, PlainTextResponse
from sqlmodel import Session

from app.internal import backups
from app.internal.activity import pending_count
from app.internal.auth.authentication import DetailedUser, EarrAuth
from app.internal.env_settings import Settings
from app.internal.health import get_report
from app.internal.models import GroupEnum
from app.util.connection import get_connection
from app.util.db import get_session
from app.util.log import LogLine, recent
from app.util.templates import catalog_response, catalog_response_toast
from app.util.toast import toast_response

router = APIRouter(prefix="/system")

LogLevel = Literal["debug", "info", "warning", "error"]
_LEVELS: dict[str, int] = {
    "debug": 0,
    "info": 1,
    "warning": 2,
    "error": 3,
    "critical": 3,
}
MAX_LINES = 500
"""Lines shown on the page; the download has the whole buffer."""


@router.get("/status")
async def status(
    session: Annotated[Session, Depends(get_session)],
    client_session: Annotated[ClientSession, Depends(get_connection)],
    admin_user: Annotated[DetailedUser, Security(EarrAuth(GroupEnum.admin))],
):
    settings = Settings()
    return catalog_response(
        "System.Status",
        user=admin_user,
        report=await get_report(session, client_session),
        database="Postgres" if settings.db.use_postgres else "SQLite",
        config_dir=settings.app.config_dir,
    )


@router.get("/hx-checks")
async def checks(
    session: Annotated[Session, Depends(get_session)],
    client_session: Annotated[ClientSession, Depends(get_connection)],
    _: Annotated[DetailedUser, Security(EarrAuth(GroupEnum.admin))],
    refresh: bool = False,
):
    return catalog_response(
        "System.Checks",
        report=await get_report(session, client_session, refresh=refresh),
    )


@router.get("/backups")
def backups_page(
    session: Annotated[Session, Depends(get_session)],
    admin_user: Annotated[DetailedUser, Security(EarrAuth(GroupEnum.admin))],
):
    return catalog_response(
        "System.Backups",
        user=admin_user,
        supported=backups.supported(),
        files=backups.list_backups(),
        enabled=backups.backup_config.get_enabled(session),
        keep=backups.backup_config.get_keep(session),
        folder=str(backups.backup_dir()) if backups.supported() else "",
    )


@router.post("/backups/hx-create")
def create_backup(
    _: Annotated[DetailedUser, Security(EarrAuth(GroupEnum.admin))],
):
    try:
        made = backups.create_backup("manual")
    except backups.BackupError as e:
        return toast_response(str(e), "error")
    return catalog_response_toast(
        "System.BackupList",
        f"Saved {made.name}",
        "success",
        files=backups.list_backups(),
    )


@router.post("/backups/hx-settings")
def backup_settings(
    session: Annotated[Session, Depends(get_session)],
    _: Annotated[DetailedUser, Security(EarrAuth(GroupEnum.admin))],
    keep: Annotated[int, Form()] = 7,
    enabled: Annotated[bool, Form()] = False,
):
    backups.backup_config.set_enabled(session, enabled)
    backups.backup_config.set_keep(session, keep)
    return toast_response("Backup settings saved")


@router.get("/backups/download/{name}")
def download_backup(
    name: str,
    _: Annotated[DetailedUser, Security(EarrAuth(GroupEnum.admin))],
):
    path = backups.path_for(name)
    if path is None:
        raise HTTPException(status_code=404, detail="No such backup")
    return FileResponse(path, filename=name, media_type="application/vnd.sqlite3")


@router.post("/backups/hx-restore/{name}")
def restore_backup(
    name: str,
    _user: Annotated[DetailedUser, Security(EarrAuth(GroupEnum.admin))],
):
    try:
        _ = backups.restore_backup(name)
    except backups.BackupError as e:
        return toast_response(str(e), "error")
    # settings, users and sessions may all have changed underneath the page
    return Response(
        headers={"HX-Redirect": f"{Settings().app.base_url.rstrip('/')}/system/backups"}
    )


@router.delete("/backups/hx/{name}")
def delete_backup(
    name: str,
    _: Annotated[DetailedUser, Security(EarrAuth(GroupEnum.admin))],
):
    if not backups.delete_backup(name):
        raise HTTPException(status_code=404, detail="No such backup")
    return catalog_response("System.BackupList", files=backups.list_backups())


@router.post("/backups/hx-upload")
async def upload_backup(
    _: Annotated[DetailedUser, Security(EarrAuth(GroupEnum.admin))],
    file: Annotated[UploadFile, File()],
):
    try:
        saved = backups.save_upload(await file.read())
    except backups.BackupError as e:
        return toast_response(str(e), "error")
    return catalog_response_toast(
        "System.BackupList",
        f"Uploaded as {saved.name}. Restore it from the list.",
        "success",
        files=backups.list_backups(),
    )


def _filtered(level: LogLevel, q: str) -> list[LogLine]:
    floor = _LEVELS[level]
    needle = q.strip().lower()
    lines = [
        line
        for line in recent
        if _LEVELS.get(line.level, 1) >= floor
        and (
            not needle
            or needle in line.event.lower()
            or any(needle in v.lower() for v in line.fields.values())
        )
    ]
    lines.reverse()
    return lines


@router.get("/logs")
def logs_page(
    admin_user: Annotated[DetailedUser, Security(EarrAuth(GroupEnum.admin))],
    level: LogLevel = "info",
    q: str = "",
):
    return catalog_response(
        "System.Logs",
        user=admin_user,
        lines=_filtered(level, q)[:MAX_LINES],
        level=level,
        query=q,
        debug_kept=Settings().app.log_level.strip().upper() == "DEBUG",
    )


@router.get("/hx-logs")
def logs_rows(
    _: Annotated[DetailedUser, Security(EarrAuth(GroupEnum.admin))],
    level: LogLevel = "info",
    q: str = "",
):
    return catalog_response(
        "System.LogLines", lines=_filtered(level, q)[:MAX_LINES], level=level, query=q
    )


@router.get("/logs/download")
def download_logs(
    _: Annotated[DetailedUser, Security(EarrAuth(GroupEnum.admin))],
):
    text = "\n".join(
        f"{line.time} [{line.level}] {line.event}"
        + "".join(f" {k}={v}" for k, v in line.fields.items())
        + (f"\n{line.exception}" if line.exception else "")
        for line in recent
    )
    return PlainTextResponse(
        text + "\n",
        headers={"Content-Disposition": 'attachment; filename="earr.log"'},
    )


@router.get("/hx-sidebar")
async def sidebar(
    session: Annotated[Session, Depends(get_session)],
    client_session: Annotated[ClientSession, Depends(get_connection)],
    user: Annotated[DetailedUser, Security(EarrAuth())],
):
    """Swapped into the sidebar out of band, so pages never wait on the checks."""
    return catalog_response(
        "SidebarStatus",
        activity=pending_count(session, user),
        report=await get_report(session, client_session) if user.is_admin() else None,
    )
