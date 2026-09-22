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

from app.internal.audiobookshelf.config import abs_config
from app.internal.library.config import library_config
from app.internal.models import Audiobook, ManualBookRequest
from app.internal.prowlarr.util import (
    prowlarr_config,
    prowlarr_indexer_cache,
    prowlarr_source_cache,
)
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
