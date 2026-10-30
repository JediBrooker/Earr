from typing import Annotated

from fastapi import APIRouter, Depends, Form, HTTPException, Security
from pydantic import BaseModel
from sqlmodel import Session, select

from app.internal.auth.authentication import EarrAuth, DetailedUser
from app.internal.auth.config import auth_config
from app.internal.auth.login_types import LoginTypeEnum
from app.internal.models import GroupEnum, User
from app.internal.request_limits import LimitPeriod, request_limit_config
from app.routers.api.users import (
    UserCreate,
    UserUpdate,
)
from app.routers.api.users import (
    create_new_user as api_create_new_user,
)
from app.routers.api.users import (
    delete_user as api_delete_user,
)
from app.routers.api.users import (
    update_user as api_update_user,
)
from app.util.db import get_session
from app.util.templates import catalog_response, catalog_response_toast
from app.util.toast import ToastException, toast_response

router = APIRouter(prefix="/users")


@router.get("")
def read_users(
    session: Annotated[Session, Depends(get_session)],
    admin_user: Annotated[DetailedUser, Security(EarrAuth(GroupEnum.admin))],
):
    users = session.exec(select(User)).all()
    is_oidc = auth_config.get_login_type(session) == LoginTypeEnum.oidc

    return catalog_response(
        "Settings.Users.Index",
        user=admin_user,
        users=users,
        is_oidc=is_oidc,
        limits=RequestLimitSettings(
            untrusted=request_limit_config.get_group_limit(
                session, GroupEnum.untrusted
            ),
            trusted=request_limit_config.get_group_limit(session, GroupEnum.trusted),
            period=request_limit_config.get_period(session).value,
        ),
    )


class RequestLimitSettings(BaseModel):
    untrusted: int
    trusted: int
    period: str


@router.post("/hx-limits")
def update_limits(
    session: Annotated[Session, Depends(get_session)],
    _: Annotated[DetailedUser, Security(EarrAuth(GroupEnum.admin))],
    untrusted: Annotated[int, Form()] = 0,
    trusted: Annotated[int, Form()] = 0,
    period: Annotated[LimitPeriod, Form()] = LimitPeriod.week,
):
    request_limit_config.set_group_limit(session, GroupEnum.untrusted, untrusted)
    request_limit_config.set_group_limit(session, GroupEnum.trusted, trusted)
    request_limit_config.set_period(session, period)
    return toast_response("Request limits saved")


@router.patch("/hx-limit/{username}")
def update_user_limit(
    username: str,
    session: Annotated[Session, Depends(get_session)],
    _: Annotated[DetailedUser, Security(EarrAuth(GroupEnum.admin))],
    request_limit: Annotated[str, Form()] = "",
):
    target = session.get(User, username)
    if target is None:
        raise HTTPException(status_code=404, detail="No such user")
    value = request_limit.strip()
    if value and not value.isdigit():
        return toast_response("Enter a number, or leave it empty", "error")
    target.request_limit = int(value) if value else None
    session.add(target)
    session.commit()
    if target.request_limit is None:
        return toast_response(f"{username} uses their group's limit")
    if target.request_limit == 0:
        return toast_response(f"{username} has no request limit")
    return toast_response(f"{username} may request {target.request_limit} per period")


@router.post("/hx-add")
def create_new_user(
    username: Annotated[str, Form()],
    password: Annotated[str, Form()],
    group: Annotated[str, Form()],
    session: Annotated[Session, Depends(get_session)],
    admin_user: Annotated[DetailedUser, Security(EarrAuth(GroupEnum.admin))],
):
    if username.strip() == "":
        raise ToastException("Invalid username", "error")

    if group not in GroupEnum.__members__:
        raise ToastException("Invalid group selected", "error")

    try:
        api_create_new_user(
            UserCreate(
                username=username,
                password=password,
                group=GroupEnum[group],
                root=False,
                extra_data=None,
            ),
            session,
            admin_user,
        )
    except HTTPException as e:
        raise ToastException(e.detail, "error")

    users = session.exec(select(User)).all()

    return catalog_response_toast(
        "Settings.Users.List",
        "Created user",
        "success",
        user=admin_user,
        users=users,
    )


@router.delete("/hx-delete/{username}")
def delete_user(
    username: str,
    session: Annotated[Session, Depends(get_session)],
    admin_user: Annotated[DetailedUser, Security(EarrAuth(GroupEnum.admin))],
):
    try:
        api_delete_user(username, session, admin_user)
    except HTTPException as e:
        raise ToastException(e.detail, "error")

    users = session.exec(select(User)).all()

    return catalog_response_toast(
        "Settings.Users.List",
        "Deleted user",
        "success",
        user=admin_user,
        users=users,
    )


@router.patch("/hx-update/{username}")
def update_user(
    username: str,
    session: Annotated[Session, Depends(get_session)],
    admin_user: Annotated[DetailedUser, Security(EarrAuth(GroupEnum.admin))],
    group: Annotated[GroupEnum | None, Form()] = None,
    extra_data: Annotated[str | None, Form()] = None,
):
    try:
        api_update_user(
            admin_user,
            session=session,
            username=username,
            user_data=UserUpdate(
                password=None,
                group=group,
                extra_data=extra_data,
            ),
        )
    except HTTPException as e:
        raise ToastException(e.detail, "error")

    if group is None and extra_data is None:
        success_msg = "No changes made"
    elif group is not None and extra_data is not None:
        success_msg = "Updated group and extra data"
    elif group is not None:
        success_msg = "Updated group"
    elif extra_data is not None:
        success_msg = "Updated extra data"
    else:
        success_msg = "Updated user"

    users = session.exec(select(User)).all()

    return catalog_response_toast(
        "Settings.Users.List",
        success_msg,
        "success",
        user=admin_user,
        users=users,
    )
