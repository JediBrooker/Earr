"""Checks that the connections and folders Earr depends on actually work.

Shown on the System page and summed up by the dot next to System in the
sidebar, so a broken setup is noticed before a request quietly goes nowhere.
Results are kept for a minute: the sidebar asks on every page load, and most
checks talk to other services.
"""

import asyncio
import os
import time
from collections.abc import Awaitable
from datetime import datetime, timedelta
from enum import Enum
from pathlib import Path

from aiohttp import ClientSession
from pydantic import BaseModel
from sqlmodel import Session, col, func, select

from app.internal.audiobookshelf.client import abs_get_libraries
from app.internal.audiobookshelf.config import abs_config
from app.internal.download_clients.abstract import DownloadClient
from app.internal.download_clients.config import download_client_config
from app.internal.library.config import library_config
from app.internal.models import (
    Audiobook,
    DownloadStatusEnum,
    LibraryImport,
    LibraryImportStatusEnum,
    ManualBookRequest,
    OrganizeModeEnum,
)
from app.internal.prowlarr.prowlarr import get_indexers
from app.internal.prowlarr.util import prowlarr_config

CHECK_TIMEOUT = 10
"""Seconds to wait for another service before calling it unreachable."""
CACHE_SECONDS = 60
STUCK_AFTER = timedelta(days=1)
"""A grab that hasn't reached the library by then probably never will."""


class CheckLevel(str, Enum):
    ok = "ok"
    warning = "warning"
    error = "error"
    off = "off"
    """Not set up. Fine in itself; listed so it's clear what isn't in use."""


class HealthCheck(BaseModel):
    name: str
    level: CheckLevel
    message: str
    fix_path: str | None = None
    """The page that sets this up or fixes it."""


class HealthReport(BaseModel):
    checks: list[HealthCheck]
    checked_at: datetime

    @property
    def problems(self) -> list[HealthCheck]:
        return [
            c for c in self.checks if c.level in (CheckLevel.warning, CheckLevel.error)
        ]

    @property
    def level(self) -> CheckLevel:
        levels = {c.level for c in self.checks}
        if CheckLevel.error in levels:
            return CheckLevel.error
        if CheckLevel.warning in levels:
            return CheckLevel.warning
        return CheckLevel.ok


def _plural(count: int, word: str) -> str:
    return f"{count} {word}{'' if count == 1 else 's'}"


async def _prowlarr(session: Session, client_session: ClientSession) -> HealthCheck:
    name, fix = "Prowlarr", "/settings/prowlarr"
    if not prowlarr_config.is_valid(session):
        return HealthCheck(
            name=name,
            level=CheckLevel.warning,
            message="Not connected, so requests can't be downloaded automatically.",
            fix_path=fix,
        )
    response = await get_indexers(session, client_session)
    if not response.ok:
        return HealthCheck(
            name=name,
            level=CheckLevel.error,
            message=f"Couldn't reach Prowlarr: {response.error or 'no answer'}.",
            fix_path=fix,
        )
    enabled = [i for i in response.indexers.values() if i.enable]
    if not enabled:
        return HealthCheck(
            name=name,
            level=CheckLevel.warning,
            message="Connected, but no indexers are enabled in Prowlarr, so searches find nothing.",
            fix_path=fix,
        )
    return HealthCheck(
        name=name,
        level=CheckLevel.ok,
        message=f"Connected, {_plural(len(enabled), 'indexer')} enabled.",
    )


async def _download_client(
    name: str, client: DownloadClient | None, client_session: ClientSession
) -> HealthCheck:
    fix = "/settings/library"
    if client is None:
        return HealthCheck(
            name=name, level=CheckLevel.off, message="Not set up.", fix_path=fix
        )
    ok, message = await client.test(client_session)
    return HealthCheck(
        name=name,
        level=CheckLevel.ok if ok else CheckLevel.error,
        message=message,
        fix_path=None if ok else fix,
    )


async def _audiobookshelf(
    session: Session, client_session: ClientSession
) -> HealthCheck:
    name, fix = "Audiobookshelf", "/settings/audiobookshelf"
    if not abs_config.is_connected(session):
        return HealthCheck(
            name=name, level=CheckLevel.off, message="Not connected.", fix_path=fix
        )
    libraries = await abs_get_libraries(session, client_session)
    if not libraries:
        return HealthCheck(
            name=name,
            level=CheckLevel.error,
            message="Couldn't list Audiobookshelf's libraries. Check the URL and the API token.",
            fix_path=fix,
        )
    wanted = abs_config.get_library_id(session)
    library = next((lib for lib in libraries if lib.id == wanted), None)
    if library is None:
        return HealthCheck(
            name=name,
            level=CheckLevel.warning,
            message="Connected, but the selected library no longer exists.",
            fix_path=fix,
        )
    return HealthCheck(
        name=name,
        level=CheckLevel.ok,
        message=f"Connected, using the {library.name} library.",
    )


def _device(path: Path) -> int:
    return os.stat(path).st_dev


def _library_folders(session: Session) -> HealthCheck:
    name, fix = "Library folders", "/settings/library"
    if not library_config.get_enabled(session):
        return HealthCheck(
            name=name,
            level=CheckLevel.off,
            message="Organizing downloads into the library is off.",
            fix_path=fix,
        )

    root = library_config.get_root_dir(session)
    mode = library_config.get_mode(session)
    problems: list[tuple[CheckLevel, str]] = []

    if root is None:
        problems.append((CheckLevel.error, "No library folder is set."))
    elif not root.is_dir():
        problems.append(
            (CheckLevel.error, f"The library folder {root} doesn't exist here.")
        )
    elif not os.access(root, os.W_OK):
        problems.append(
            (CheckLevel.error, f"Earr can't write to the library folder {root}.")
        )
    usable_root = root if root is not None and root.is_dir() else None

    downloads = library_config.get_download_dirs(session)
    if not downloads and not download_client_config.any_enabled(session):
        problems.append(
            (
                CheckLevel.error,
                "Set a completed downloads folder or a download client, or finished downloads can't be found.",
            )
        )
    for folder in downloads:
        if not folder.is_dir():
            problems.append(
                (
                    CheckLevel.warning,
                    f"The completed downloads folder {folder} doesn't exist here.",
                )
            )
            continue
        if mode == OrganizeModeEnum.move and not os.access(folder, os.W_OK):
            problems.append(
                (
                    CheckLevel.error,
                    f"Move mode deletes from {folder}, but Earr can't write there.",
                )
            )
        if (
            mode == OrganizeModeEnum.hardlink
            and usable_root is not None
            and _device(folder) != _device(usable_root)
        ):
            problems.append(
                (
                    CheckLevel.error,
                    f"{folder} and {usable_root} are on different filesystems, so hardlinks"
                    + " will fail. Mount them from the same volume, or switch Mode to copy.",
                )
            )

    if not problems:
        return HealthCheck(
            name=name,
            level=CheckLevel.ok,
            message=f"Organizing into {root} by {mode.value}.",
        )
    level = (
        CheckLevel.error
        if any(level == CheckLevel.error for level, _ in problems)
        else CheckLevel.warning
    )
    return HealthCheck(
        name=name,
        level=level,
        message=" ".join(text for _, text in problems),
        fix_path=fix,
    )


def _downloads(session: Session) -> HealthCheck:
    name = "Downloads"
    stuck = session.exec(
        select(func.count())
        .select_from(LibraryImport)
        .where(
            LibraryImport.status == LibraryImportStatusEnum.pending,
            col(LibraryImport.created_at) < datetime.now() - STUCK_AFTER,
        )
    ).one()
    failed = sum(
        session.exec(
            select(func.count())
            .select_from(table)
            .where(table.download_status == DownloadStatusEnum.failed)
        ).one()
        for table in (Audiobook, ManualBookRequest)
    )
    messages: list[str] = []
    if stuck:
        messages.append(
            f"{_plural(stuck, 'download')} waiting more than a day to reach the library."
        )
    if failed:
        messages.append(
            f"{_plural(failed, 'request')} failed to download and went back on the wishlist."
        )
    if not messages:
        return HealthCheck(name=name, level=CheckLevel.ok, message="Nothing stuck.")
    return HealthCheck(
        name=name,
        level=CheckLevel.warning,
        message=" ".join(messages),
        fix_path="/activity" if stuck else "/wishlist",
    )


async def _within_time(
    name: str, fix_path: str | None, check: Awaitable[HealthCheck]
) -> HealthCheck:
    """A check that hangs or crashes is reported, never allowed to stall the page."""
    try:
        return await asyncio.wait_for(check, CHECK_TIMEOUT)
    except TimeoutError:
        return HealthCheck(
            name=name,
            level=CheckLevel.error,
            message=f"No answer within {CHECK_TIMEOUT} seconds.",
            fix_path=fix_path,
        )
    except Exception as e:
        return HealthCheck(
            name=name,
            level=CheckLevel.error,
            message=str(e) or type(e).__name__,
            fix_path=fix_path,
        )


async def run_checks(session: Session, client_session: ClientSession) -> HealthReport:
    remote = await asyncio.gather(
        _within_time(
            "Prowlarr", "/settings/prowlarr", _prowlarr(session, client_session)
        ),
        _within_time(
            "qBittorrent",
            "/settings/library",
            _download_client(
                "qBittorrent",
                download_client_config.build_qbit(session),
                client_session,
            ),
        ),
        _within_time(
            "SABnzbd",
            "/settings/library",
            _download_client(
                "SABnzbd", download_client_config.build_sab(session), client_session
            ),
        ),
        _within_time(
            "Audiobookshelf",
            "/settings/audiobookshelf",
            _audiobookshelf(session, client_session),
        ),
    )
    return HealthReport(
        checks=[*remote, _library_folders(session), _downloads(session)],
        checked_at=datetime.now(),
    )


class _Cache:
    report: HealthReport | None = None
    at: float = 0.0


def forget_report() -> None:
    """Drops the cached result, so the next request checks again."""
    _Cache.report = None


async def get_report(
    session: Session, client_session: ClientSession, refresh: bool = False
) -> HealthReport:
    if (
        not refresh
        and _Cache.report is not None
        and time.monotonic() - _Cache.at < CACHE_SECONDS
    ):
        return _Cache.report
    report = await run_checks(session, client_session)
    _Cache.report, _Cache.at = report, time.monotonic()
    return report
