"""The System page, and the counts and health light the sidebar loads after the page."""

from typing import Annotated

from aiohttp import ClientSession
from fastapi import APIRouter, Depends, Security
from sqlmodel import Session

from app.internal.activity import pending_count
from app.internal.auth.authentication import DetailedUser, EarrAuth
from app.internal.env_settings import Settings
from app.internal.health import get_report
from app.internal.models import GroupEnum
from app.util.connection import get_connection
from app.util.db import get_session
from app.util.templates import catalog_response

router = APIRouter(prefix="/system")


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
