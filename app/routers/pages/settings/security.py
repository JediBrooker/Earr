from typing import Annotated, Literal, cast

from aiohttp import ClientSession
from fastapi import APIRouter, Depends, Form, HTTPException, Response, Security
from pydantic import BaseModel
from sqlmodel import Session

from app.internal.audiobookshelf.config import abs_config
from app.internal.auth.abs_login import abs_login_config
from app.internal.auth.authentication import EarrAuth, DetailedUser
from app.internal.auth.config import auth_config
from app.internal.auth.login_types import LoginTypeEnum
from app.internal.auth.oidc_config import oidc_config
from app.internal.env_settings import Settings
from app.internal.models import GroupEnum
from app.routers.api.settings.security import (
    UpdateSecuritySettings,
)
from app.routers.api.settings.security import (
    reset_auth_secret as api_reset_auth_secret,
)
from app.routers.api.settings.security import (
    update_security_settings as api_update_security_settings,
)
from app.util.connection import get_connection
from app.util.db import get_session
from app.util.log import logger
from app.util.templates import catalog_response, catalog_response_toast
from app.util.toast import ToastException, toast_response

router = APIRouter(prefix="/security")


class AbsLoginSettings(BaseModel):
    connected: bool
    enabled: bool
    group: str
    admins_are_admins: bool


def _abs_login_settings(session: Session) -> AbsLoginSettings:
    return AbsLoginSettings(
        connected=abs_config.is_connected(session),
        enabled=abs_login_config.get_enabled(session),
        group=abs_login_config.get_group(session).value,
        admins_are_admins=abs_login_config.get_admins_are_admins(session),
    )


@router.post("/hx-abs-login")
def update_abs_login(
    session: Annotated[Session, Depends(get_session)],
    _: Annotated[DetailedUser, Security(EarrAuth(GroupEnum.admin))],
    enabled: Annotated[bool, Form()] = False,
    group: Annotated[GroupEnum, Form()] = GroupEnum.untrusted,
    admins: Annotated[bool, Form()] = False,
):
    if enabled and not abs_config.is_connected(session):
        return toast_response("Connect Audiobookshelf first", "error")
    abs_login_config.set_enabled(session, enabled)
    abs_login_config.set_group(
        session,
        GroupEnum.trusted if group == GroupEnum.trusted else GroupEnum.untrusted,
    )
    abs_login_config.set_admins_are_admins(session, admins)
    return toast_response("Audiobookshelf sign-in saved")


@router.get("")
def read_security(
    session: Annotated[Session, Depends(get_session)],
    admin_user: Annotated[DetailedUser, Security(EarrAuth(GroupEnum.admin))],
):
    try:
        force_login_type = Settings().app.get_force_login_type()
    except ValueError as e:
        logger.error("Invalid force login type", exc_info=e)
        force_login_type = None

    return catalog_response(
        "Settings.Security.Index",
        user=admin_user,
        login_type=auth_config.get_login_type(session),
        access_token_expiry=auth_config.get_access_token_expiry_minutes(session),
        min_password_length=auth_config.get_min_password_length(session),
        oidc_endpoint=oidc_config.get(session, "oidc_endpoint", ""),
        oidc_client_secret=oidc_config.get(session, "oidc_client_secret", ""),
        oidc_client_id=oidc_config.get(session, "oidc_client_id", ""),
        oidc_scope=oidc_config.get(session, "oidc_scope", ""),
        oidc_username_claim=oidc_config.get(session, "oidc_username_claim", ""),
        oidc_group_claim=oidc_config.get(session, "oidc_group_claim", ""),
        oidc_redirect_scheme=oidc_config.get_redirect_scheme(session),
        oidc_logout_url=oidc_config.get(session, "oidc_logout_url", ""),
        force_login_type=force_login_type,
        abs_login=_abs_login_settings(session),
    )


@router.post("/hx-reset-auth")
def reset_auth_secret(
    session: Annotated[Session, Depends(get_session)],
    admin_user: Annotated[DetailedUser, Security(EarrAuth(GroupEnum.admin))],
):
    api_reset_auth_secret(session, admin_user)
    return Response(status_code=204, headers={"HX-Refresh": "true"})


@router.post("/hx-update")
async def update_security(
    session: Annotated[Session, Depends(get_session)],
    client_session: Annotated[ClientSession, Depends(get_connection)],
    admin_user: Annotated[DetailedUser, Security(EarrAuth(GroupEnum.admin))],
    login_type: Annotated[LoginTypeEnum | None, Form()] = None,
    access_token_expiry: Annotated[int | None, Form()] = None,
    min_password_length: Annotated[int | None, Form()] = None,
    oidc_endpoint: Annotated[str | None, Form()] = None,
    oidc_client_id: Annotated[str | None, Form()] = None,
    oidc_client_secret: Annotated[str | None, Form()] = None,
    oidc_scope: Annotated[str | None, Form()] = None,
    oidc_username_claim: Annotated[str | None, Form()] = None,
    oidc_group_claim: Annotated[str | None, Form()] = None,
    oidc_redirect_scheme: Annotated[str | None, Form()] = None,
    oidc_logout_url: Annotated[str | None, Form()] = None,
):
    try:
        force_login_type = Settings().app.get_force_login_type()
    except ValueError as e:
        logger.error("Invalid force login type", exc_info=e)
        raise ToastException("Invalid force login type configured", "error") from e

    if force_login_type is not None:
        login_type = force_login_type
    elif login_type is None:
        raise ToastException("Login type is required", "error")

    redirect_scheme: Literal["auto", "http", "https"] = "auto"
    if oidc_redirect_scheme in ["http", "https"]:
        redirect_scheme = cast(Literal["http", "https"], oidc_redirect_scheme)

    try:
        await api_update_security_settings(
            UpdateSecuritySettings(
                login_type=login_type,
                access_token_expiry=access_token_expiry,
                min_password_length=min_password_length,
                oidc_endpoint=oidc_endpoint,
                oidc_client_id=oidc_client_id,
                oidc_client_secret=oidc_client_secret,
                oidc_scope=oidc_scope,
                oidc_username_claim=oidc_username_claim,
                oidc_group_claim=oidc_group_claim,
                oidc_redirect_scheme=redirect_scheme,
                oidc_logout_url=oidc_logout_url,
            ),
            session,
            client_session,
            admin_user,
        )
    except HTTPException as e:
        raise ToastException(e.detail, "error") from None

    old = auth_config.get_login_type(session)

    return catalog_response_toast(
        "Settings.Security.Form",
        "Settings updated",
        "success",
        login_type=auth_config.get_login_type(session),
        access_token_expiry=auth_config.get_access_token_expiry_minutes(session),
        min_password_length=auth_config.get_min_password_length(session),
        oidc_endpoint=oidc_config.get(session, "oidc_endpoint", ""),
        oidc_client_secret=oidc_config.get(session, "oidc_client_secret", ""),
        oidc_client_id=oidc_config.get(session, "oidc_client_id", ""),
        oidc_scope=oidc_config.get(session, "oidc_scope", ""),
        oidc_username_claim=oidc_config.get(session, "oidc_username_claim", ""),
        oidc_group_claim=oidc_config.get(session, "oidc_group_claim", ""),
        oidc_redirect_scheme=oidc_config.get_redirect_scheme(session),
        oidc_logout_url=oidc_config.get(session, "oidc_logout_url", ""),
        force_login_type=force_login_type,
        headers={} if old == login_type else {"HX-Refresh": "true"},
    )
