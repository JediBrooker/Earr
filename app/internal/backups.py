"""Copies of the SQLite database, taken daily and on demand.

A backup is the whole database, so it holds every setting, user, request and
follow. Restoring one replaces all of that, after first taking a backup of the
current state so the restore itself can be undone.

Postgres is not covered: it has its own tools (pg_dump), and a copy taken from
inside the app would be no better than those.
"""

import re
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Literal, cast

from alembic.config import Config as AlembicConfig
from alembic.script import ScriptDirectory
from alembic import command
from pydantic import BaseModel
from sqlmodel import Session

from app.internal.env_settings import Settings
from app.util.cache import StringConfigCache
from app.util.db import engine, get_session
from app.util.log import logger

BackupKind = Literal["scheduled", "manual", "before-restore", "uploaded"]
_NAME = re.compile(
    r"^earr-(\d{8}-\d{6})-(scheduled|manual|before-restore|uploaded)\.sqlite$"
)
_SQLITE_HEADER = b"SQLite format 3\x00"


class BackupError(Exception):
    pass


class BackupConfig(StringConfigCache[Literal["backup_enabled", "backup_keep"]]):
    def get_enabled(self, session: Session) -> bool:
        enabled = self.get_bool(session, "backup_enabled")
        return True if enabled is None else enabled

    def set_enabled(self, session: Session, enabled: bool):
        self.set_bool(session, "backup_enabled", enabled)

    def get_keep(self, session: Session) -> int:
        """How many scheduled backups to keep. Others are never pruned."""
        return max(1, self.get_int(session, "backup_keep", 7))

    def set_keep(self, session: Session, keep: int):
        self.set_int(session, "backup_keep", max(1, keep))


backup_config = BackupConfig()


class BackupFile(BaseModel):
    name: str
    kind: BackupKind
    created: datetime
    size: int

    @property
    def size_label(self) -> str:
        if self.size < 1024 * 1024:
            return f"{self.size / 1024:.0f} KB"
        return f"{self.size / 1024 / 1024:.1f} MB"

    @property
    def kind_label(self) -> str:
        return {
            "scheduled": "Daily",
            "manual": "Manual",
            "before-restore": "Before a restore",
            "uploaded": "Uploaded",
        }[self.kind]


def supported() -> bool:
    return not Settings().db.use_postgres


def backup_dir() -> Path:
    path = Path(Settings().app.config_dir) / "backups"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _database_path() -> Path:
    return Path(Settings().get_sqlite_path())


def _parse(path: Path) -> BackupFile | None:
    match = _NAME.match(path.name)
    if not match:
        return None
    return BackupFile(
        name=path.name,
        kind=match.group(2),  # pyright: ignore[reportArgumentType]
        created=datetime.strptime(match.group(1), "%Y%m%d-%H%M%S"),
        size=path.stat().st_size,
    )


def list_backups() -> list[BackupFile]:
    if not supported():
        return []
    found = [b for p in backup_dir().iterdir() if (b := _parse(p))]
    return sorted(found, key=lambda b: b.created, reverse=True)


def path_for(name: str) -> Path | None:
    """Only names Earr itself wrote, so a request can't reach any other file."""
    if not _NAME.match(name):
        return None
    path = backup_dir() / name
    return path if path.is_file() else None


def _copy(source: Path, target: Path) -> None:
    """SQLite's online backup, which is safe while the app keeps writing."""
    src = sqlite3.connect(source)
    dst = sqlite3.connect(target)
    try:
        src.backup(dst)
    finally:
        dst.close()
        src.close()


def create_backup(kind: BackupKind = "manual") -> BackupFile:
    if not supported():
        raise BackupError(
            "Backups are only available with SQLite. Use pg_dump for Postgres."
        )
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    target = backup_dir() / f"earr-{stamp}-{kind}.sqlite"
    partial = target.with_suffix(".partial")
    _copy(_database_path(), partial)
    _ = partial.replace(target)
    logger.info("Backed up the database", file=target.name)
    backup = _parse(target)
    assert backup is not None
    return backup


def prune(keep: int) -> int:
    """Deletes the oldest daily backups beyond `keep`. Returns how many went."""
    scheduled = [b for b in list_backups() if b.kind == "scheduled"]
    removed = 0
    for old in scheduled[keep:]:
        (backup_dir() / old.name).unlink(missing_ok=True)
        removed += 1
    return removed


def delete_backup(name: str) -> bool:
    path = path_for(name)
    if path is None:
        return False
    path.unlink()
    return True


def _alembic() -> AlembicConfig:
    # no ini file, so alembic leaves the app's logging alone
    config = AlembicConfig()
    config.set_main_option("script_location", "alembic")
    return config


def _check(path: Path) -> None:
    """Refuses anything that is not an Earr database this version can upgrade."""
    with path.open("rb") as f:
        if f.read(len(_SQLITE_HEADER)) != _SQLITE_HEADER:
            raise BackupError("That file is not a SQLite database.")
    try:
        db = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            rows = cast(
                list[tuple[str]],
                db.execute("SELECT version_num FROM alembic_version").fetchall(),
            )
            tables = {
                r[0]
                for r in cast(
                    list[tuple[str]],
                    db.execute(
                        "SELECT name FROM sqlite_master WHERE type='table'"
                    ).fetchall(),
                )
            }
        finally:
            db.close()
    except sqlite3.Error as e:
        raise BackupError(f"That is not an Earr database: {e}") from e
    if "user" not in tables or "config" not in tables:
        raise BackupError("That is not an Earr database.")
    scripts = ScriptDirectory.from_config(_alembic())
    for (revision,) in rows:
        try:
            _ = scripts.get_revision(revision)
        except Exception as e:
            raise BackupError(
                "That backup comes from a newer version of Earr. Update Earr first."
            ) from e


def restore_backup(name: str) -> BackupFile:
    """Replaces the database with a backup. Returns the backup of what was there."""
    path = path_for(name)
    if path is None:
        raise BackupError("No such backup.")
    _check(path)
    safety = create_backup("before-restore")
    engine.dispose()
    _copy(path, _database_path())
    engine.dispose()
    # a backup from an older version needs the newer tables and columns
    command.upgrade(_alembic(), "heads")
    StringConfigCache.forget_all()
    logger.warning("Restored the database from a backup", file=name, safety=safety.name)
    return safety


def save_upload(data: bytes) -> BackupFile:
    """Stores an uploaded backup next to the others, after checking it."""
    if not supported():
        raise BackupError("Backups are only available with SQLite.")
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    target = backup_dir() / f"earr-{stamp}-uploaded.sqlite"
    partial = target.with_suffix(".partial")
    _ = partial.write_bytes(data)
    try:
        _check(partial)
    except BackupError:
        partial.unlink(missing_ok=True)
        raise
    _ = partial.replace(target)
    backup = _parse(target)
    assert backup is not None
    return backup


async def scheduled_backup() -> None:
    """Scheduled entry point. Never raises so the scheduler keeps running."""
    if not supported():
        return
    try:
        with next(get_session()) as session:
            if not backup_config.get_enabled(session):
                return
            keep = backup_config.get_keep(session)
        _ = create_backup("scheduled")
        _ = prune(keep)
    except Exception as e:
        logger.error("Scheduled backup failed", error=str(e))
