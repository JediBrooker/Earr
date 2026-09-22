"""Shared fixtures.

The application builds its database engine at import time from the settings,
so the config directory has to be pointed somewhere disposable before anything
under `app` is imported.
"""

import os
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Iterator

os.environ.setdefault(
    "EARR_APP__CONFIG_DIR", tempfile.mkdtemp(prefix="earr-tests-config-")
)

import pytest
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine

# app.main queries the database while it is being imported, so the real engine
# needs the schema even though every test talks to its own in-memory database.
# The models have to be imported first or SQLModel.metadata is still empty.
import app.internal.models  # noqa: F401  pyright: ignore[reportUnusedImport]
from app.util.db import engine as _real_engine

SQLModel.metadata.create_all(_real_engine)


def _seed_real_database() -> None:
    """The redirect-to-init middleware calls get_session() directly rather than
    through Depends, so it reads the real database and cannot be overridden. It
    sends every GET to /init while no user exists, so one has to be there."""
    from app.internal.models import GroupEnum as _Group
    from app.internal.models import User as _User

    with Session(_real_engine) as s:
        if s.get(_User, "test-bootstrap") is None:
            s.add(
                _User(
                    username="test-bootstrap",
                    password="unused",
                    group=_Group.admin,
                    root=True,
                )
            )
            s.commit()


_seed_real_database()

from app.internal.audiobookshelf.config import abs_config
from app.internal.library.config import library_config
from app.internal.models import Audiobook, ManualBookRequest
from app.internal.prowlarr.util import (
    prowlarr_config,
    prowlarr_indexer_cache,
    prowlarr_source_cache,
)
from app.internal.models import APIKey, GroupEnum, User
from app.internal.ranking.quality import quality_config

_CONFIGS = (library_config, prowlarr_config, abs_config, quality_config)
_CACHES = (prowlarr_source_cache, prowlarr_indexer_cache)


@pytest.fixture(autouse=True)
def _clean_caches() -> Iterator[None]:
    """Config and source caches are module level singletons.

    Without this a value set by one test is still cached for the next one, even
    though each test gets a fresh database.
    """
    # clear in place rather than calling flush(), which rebinds _cache and would
    # hide the very class-attribute sharing that test_cache.py checks for
    def clear() -> None:
        for c in (*_CONFIGS, *_CACHES):
            c._cache.clear()  # pyright: ignore[reportPrivateUsage]

    clear()
    yield
    clear()


@pytest.fixture
def session() -> Iterator[Session]:
    """An isolated in-memory database with the full schema."""
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(engine)
    with Session(engine) as s:
        yield s


@pytest.fixture
def library(tmp_path: Path) -> tuple[Path, Path]:
    """A downloads folder and a library folder on the same filesystem."""
    downloads = tmp_path / "downloads"
    root = tmp_path / "library"
    downloads.mkdir()
    root.mkdir()
    return downloads, root


def make_download(downloads: Path, name: str, files: dict[str, str]) -> Path:
    """Creates a finished download with the given relative files."""
    folder = downloads / name
    for relative, content in files.items():
        path = folder / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        _ = path.write_text(content)
    return folder


def series_book(asin: str = "B1", **overrides: object) -> Audiobook:
    defaults: dict[str, object] = {
        "asin": asin,
        "title": "The Way of Kings",
        "subtitle": "The Stormlight Archive, Book 1",
        "authors": ["Brandon Sanderson"],
        "narrators": ["Michael Kramer", "Kate Reading"],
        "cover_image": None,
        "release_date": datetime(2010, 8, 31),
        "runtime_length_min": 2734,
        "series": "The Stormlight Archive",
        "series_position": "1",
    }
    return Audiobook(**(defaults | overrides))  # pyright: ignore[reportArgumentType]


def standalone_book(asin: str = "B2", **overrides: object) -> Audiobook:
    defaults: dict[str, object] = {
        "asin": asin,
        "title": "The Martian",
        "subtitle": None,
        "authors": ["Andy Weir"],
        "narrators": ["R. C. Bray"],
        "cover_image": None,
        "release_date": datetime(2013, 3, 22),
        "runtime_length_min": 636,
    }
    return Audiobook(**(defaults | overrides))  # pyright: ignore[reportArgumentType]


def manual_request(**overrides: object) -> ManualBookRequest:
    defaults: dict[str, object] = {
        "user_username": "someone",
        "title": "An Indie Book",
        "authors": ["Jane Doe"],
        "narrators": [],
        "publish_date": "2021-06-02",
    }
    return ManualBookRequest(**(defaults | overrides))  # pyright: ignore[reportArgumentType]


# --- HTTP layer -------------------------------------------------------------


@pytest.fixture
def client(session: Session) -> Iterator["TestClient"]:
    """A test client wired to the isolated database.

    The auth classes are instantiated per route, so they cannot be overridden;
    requests authenticate with a real API key instead, which exercises the
    actual auth path rather than bypassing it.
    """
    from fastapi.testclient import TestClient

    from app.main import app
    from app.util.db import get_session

    app.dependency_overrides[get_session] = lambda: session
    # deliberately not used as a context manager: that would run the lifespans
    # and start the background schedulers for the duration of the test
    yield TestClient(app)
    app.dependency_overrides.clear()


def api_key_for(session: Session, group: GroupEnum, username: str | None = None) -> str:
    """Creates a user in the given group and returns a usable bearer token."""
    from app.internal.auth.authentication import create_api_key

    username = username or f"{group.value}-user"
    user = User(username=username, password="unused", group=group, root=False)
    session.add(user)
    session.commit()

    api_key, private_key = create_api_key(user, "tests")
    session.add(api_key)
    session.commit()
    return private_key


def auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}
