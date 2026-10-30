"""Signing in with an Audiobookshelf account.

Everyone who listens already has an Audiobookshelf login, so they can use it
here instead of an admin creating a second account for them. The password is
checked against Audiobookshelf; the first successful sign-in creates the Earr
account, in the group the admin chose (Audiobookshelf admins can be made Earr
admins).

The password is also stored as a hash, like any Earr account, so Basic Auth
doesn't ask Audiobookshelf on every request. It is checked with Audiobookshelf
again on every form sign-in and every few minutes otherwise, so a password
changed or an account disabled there stops working here too.

Accounts that exist only in Earr are never checked against Audiobookshelf, so
an Audiobookshelf user can't take over a local account with the same name.
"""

import time
from typing import Literal, cast

import aiohttp
from aiohttp import ClientSession
from pydantic import BaseModel
from sqlmodel import Session

from app.internal.audiobookshelf.config import abs_config
from app.internal.auth.passwords import authenticate_user, create_user, ph
from app.internal.models import GroupEnum, User
from app.util.cache import StringConfigCache
from app.util.censor import censor
from app.util.connection import USER_AGENT
from app.util.log import logger

RECHECK_SECONDS = 15 * 60

AbsLoginKey = Literal["abs_login_enabled", "abs_login_group", "abs_login_admins"]


class AbsLoginConfig(StringConfigCache[AbsLoginKey]):
    def get_enabled(self, session: Session) -> bool:
        return bool(self.get_bool(session, "abs_login_enabled") or False)

    def set_enabled(self, session: Session, enabled: bool):
        self.set_bool(session, "abs_login_enabled", enabled)

    def get_group(self, session: Session) -> GroupEnum:
        """The group new accounts start in. Never admin; that comes from below."""
        value = self.get(session, "abs_login_group", GroupEnum.untrusted.value)
        return GroupEnum.trusted if value == "trusted" else GroupEnum.untrusted

    def set_group(self, session: Session, group: GroupEnum):
        self.set(session, "abs_login_group", group.value)

    def get_admins_are_admins(self, session: Session) -> bool:
        value = self.get_bool(session, "abs_login_admins")
        return False if value is None else value

    def set_admins_are_admins(self, session: Session, enabled: bool):
        self.set_bool(session, "abs_login_admins", enabled)

    def is_active(self, session: Session) -> bool:
        return self.get_enabled(session) and abs_config.is_connected(session)


abs_login_config = AbsLoginConfig()


class AbsAccount(BaseModel):
    id: str
    username: str
    type: str
    """root, admin, user or guest."""


_verified_at: dict[str, float] = {}
"""When each Audiobookshelf account last proved its password."""


async def check_password(
    session: Session, client_session: ClientSession, username: str, password: str
) -> AbsAccount | None:
    """Asks Audiobookshelf whether this login is right and the account active."""
    base_url = abs_config.get_base_url(session)
    if not base_url:
        return None
    try:
        async with client_session.post(
            f"{base_url}/login",
            json={"username": username, "password": password},
            headers={"User-Agent": USER_AGENT},
        ) as response:
            if response.status in (401, 403):
                return None
            if not response.ok:
                logger.warning(
                    "Audiobookshelf sign-in: unexpected answer", status=response.status
                )
                return None
            payload = cast(dict[str, object], await response.json(content_type=None))
    except Exception as e:
        logger.warning("Audiobookshelf sign-in: could not reach it", error=str(e))
        return None
    user = payload.get("user")
    if not isinstance(user, dict):
        return None
    data = cast(dict[str, object], user)
    if data.get("isActive") is False:
        return None
    return AbsAccount(
        id=str(data.get("id") or ""),
        username=str(data.get("username") or username),
        type=str(data.get("type") or "user"),
    )


def _group_for(session: Session, account: AbsAccount) -> GroupEnum:
    if account.type in ("root", "admin") and abs_login_config.get_admins_are_admins(
        session
    ):
        return GroupEnum.admin
    return abs_login_config.get_group(session)


async def authenticate(
    session: Session,
    username: str,
    password: str,
    *,
    recheck: bool,
    client_session: ClientSession | None = None,
) -> User | None:
    """Signs in a local account, or an Audiobookshelf one when that is turned on.

    `recheck` asks Audiobookshelf even if the stored hash matches, which a form
    sign-in always does.
    """
    user = session.get(User, username)
    if user is not None and not user.abs_user_id:
        return authenticate_user(session, username, password)
    if not abs_login_config.is_active(session):
        return None

    if (
        user is not None
        and not recheck
        and time.time() - _verified_at.get(username, 0) < RECHECK_SECONDS
        and authenticate_user(session, username, password) is not None
    ):
        return user

    if client_session is None:
        async with ClientSession(timeout=aiohttp.ClientTimeout(total=15)) as own:
            account = await check_password(session, own, username, password)
    else:
        account = await check_password(session, client_session, username, password)
    if account is None:
        _ = _verified_at.pop(username, None)
        return None

    if user is None:
        user = create_user(username, password, _group_for(session, account))
        user.abs_user_id = account.id
        logger.info(
            "Created an account for an Audiobookshelf user",
            username=censor(username),
            group=user.group,
        )
    else:
        # keeps Basic Auth working locally between checks
        user.password = ph.hash(password)
    session.add(user)
    session.commit()
    _verified_at[username] = time.time()
    return user
