"""Signing in with an Audiobookshelf account."""

from typing import Any

import pytest
from aiohttp import web
from sqlmodel import Session

from app.internal.audiobookshelf.config import abs_config
from app.internal.auth import abs_login
from app.internal.auth.abs_login import abs_login_config, authenticate
from app.internal.auth.authentication import create_user
from app.internal.models import GroupEnum, User

ACCOUNTS = {
    "listener": ("secret", "user", True),
    "boss": ("hunter2", "admin", True),
    "gone": ("secret", "user", False),
    "local": ("abs-password", "user", True),
}


def abs_routes(calls: list[str]) -> list[Any]:
    async def login(request: web.Request) -> web.Response:
        body = await request.json()
        calls.append(body["username"])
        account = ACCOUNTS.get(body["username"])
        if not account or account[0] != body["password"]:
            return web.Response(status=401, text="Invalid credentials")
        return web.json_response(
            {
                "user": {
                    "id": f"usr_{body['username']}",
                    "username": body["username"],
                    "type": account[1],
                    "isActive": account[2],
                }
            }
        )

    return [web.post("/login", login)]


@pytest.fixture(autouse=True)
def _fresh():
    abs_login_config._cache.clear()  # pyright: ignore[reportPrivateUsage]
    abs_login._verified_at.clear()  # pyright: ignore[reportPrivateUsage]
    yield


@pytest.fixture
async def calls(session: Session, serve: Any) -> list[str]:
    seen: list[str] = []
    url = await serve(abs_routes(seen))
    abs_config.set_base_url(session, url)
    abs_config.set_api_token(session, "token")
    abs_config.set_library_id(session, "lib")
    abs_login_config.set_enabled(session, True)
    return seen


async def test_first_sign_in_creates_the_account(session: Session, calls: list[str]):
    user = await authenticate(session, "listener", "secret", recheck=True)

    assert user is not None
    assert user.abs_user_id == "usr_listener"
    assert user.group == GroupEnum.untrusted
    assert calls == ["listener"]


async def test_admins_only_become_admins_when_allowed(
    session: Session, calls: list[str]
):
    user = await authenticate(session, "boss", "hunter2", recheck=True)
    assert user is not None and user.group == GroupEnum.untrusted

    session.delete(user)
    session.commit()
    abs_login_config.set_admins_are_admins(session, True)
    user = await authenticate(session, "boss", "hunter2", recheck=True)
    assert user is not None and user.group == GroupEnum.admin


async def test_wrong_passwords_and_disabled_accounts_are_refused(
    session: Session, calls: list[str]
):
    assert await authenticate(session, "listener", "nope", recheck=True) is None
    assert await authenticate(session, "gone", "secret", recheck=True) is None
    assert session.get(User, "gone") is None


async def test_a_local_account_is_never_checked_against_audiobookshelf(
    session: Session, calls: list[str]
):
    session.add(create_user("local", "earr-password", GroupEnum.trusted))
    session.commit()

    assert await authenticate(session, "local", "abs-password", recheck=True) is None
    assert await authenticate(session, "local", "earr-password", recheck=True)
    assert calls == []


async def test_basic_auth_rechecks_only_now_and_then(
    session: Session, calls: list[str]
):
    assert await authenticate(session, "listener", "secret", recheck=True)
    assert await authenticate(session, "listener", "secret", recheck=False)
    assert calls == ["listener"]

    abs_login._verified_at["listener"] = 0  # pyright: ignore[reportPrivateUsage]
    assert await authenticate(session, "listener", "secret", recheck=False)
    assert calls == ["listener", "listener"]


async def test_turning_it_off_locks_audiobookshelf_accounts_out(
    session: Session, calls: list[str]
):
    assert await authenticate(session, "listener", "secret", recheck=True)
    abs_login_config.set_enabled(session, False)
    assert await authenticate(session, "listener", "secret", recheck=False) is None
