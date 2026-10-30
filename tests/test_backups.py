"""Database backups."""

import sqlite3
from pathlib import Path
from typing import Iterator
from unittest import mock

import pytest

from app.internal import backups

HEAD = "e5ebf7234f65"


def make_database(path: Path, users: list[str], revision: str = HEAD) -> None:
    db = sqlite3.connect(path)
    db.executescript(
        "CREATE TABLE alembic_version (version_num VARCHAR(32));"
        "CREATE TABLE user (username TEXT PRIMARY KEY);"
        "CREATE TABLE config (key TEXT PRIMARY KEY, value TEXT);"
    )
    db.execute("INSERT INTO alembic_version VALUES (?)", (revision,))
    db.executemany("INSERT INTO user VALUES (?)", [(u,) for u in users])
    db.commit()
    db.close()


def usernames(path: Path) -> list[str]:
    db = sqlite3.connect(path)
    try:
        return [r[0] for r in db.execute("SELECT username FROM user ORDER BY 1")]
    finally:
        db.close()


@pytest.fixture
def live(tmp_path: Path) -> Iterator[Path]:
    """A database and backup folder of their own, away from the test engine."""
    database = tmp_path / "db.sqlite"
    folder = tmp_path / "backups"
    folder.mkdir()
    make_database(database, ["alice"])
    with (
        mock.patch.object(backups, "_database_path", return_value=database),
        mock.patch.object(backups, "backup_dir", return_value=folder),
        mock.patch.object(backups.command, "upgrade") as upgrade,
        mock.patch.object(backups.engine, "dispose"),
    ):
        upgrade.return_value = None
        yield database


def test_a_backup_is_a_full_copy(live: Path):
    made = backups.create_backup("manual")

    assert made.kind == "manual"
    copy = backups.path_for(made.name)
    assert copy is not None and usernames(copy) == ["alice"]
    assert [b.name for b in backups.list_backups()] == [made.name]


def test_only_names_earr_wrote_can_be_reached(live: Path):
    assert backups.path_for("../db.sqlite") is None
    assert backups.path_for("earr-20260101-000000-manual.sqlite") is None


def test_pruning_only_touches_daily_backups(live: Path, tmp_path: Path):
    folder = tmp_path / "backups"
    for stamp in ("20260101", "20260102", "20260103"):
        make_database(folder / f"earr-{stamp}-030000-scheduled.sqlite", [])
    make_database(folder / "earr-20251201-030000-manual.sqlite", [])

    assert backups.prune(keep=2) == 1
    names = [b.name for b in backups.list_backups()]
    assert "earr-20260101-030000-scheduled.sqlite" not in names
    assert "earr-20251201-030000-manual.sqlite" in names


def test_restore_replaces_the_data_after_saving_the_current_state(live: Path):
    made = backups.create_backup("manual")
    db = sqlite3.connect(live)
    db.execute("INSERT INTO user VALUES ('bob')")
    db.commit()
    db.close()

    safety = backups.restore_backup(made.name)

    assert usernames(live) == ["alice"]
    safety_path = backups.path_for(safety.name)
    assert safety.kind == "before-restore"
    assert safety_path is not None and usernames(safety_path) == ["alice", "bob"]
    backups.command.upgrade.assert_called_once()  # pyright: ignore[reportFunctionMemberAccess]


def test_uploads_must_be_an_earr_database(live: Path, tmp_path: Path):
    with pytest.raises(backups.BackupError, match="not a SQLite database"):
        _ = backups.save_upload(b"hello")

    other = tmp_path / "other.sqlite"
    db = sqlite3.connect(other)
    db.execute("CREATE TABLE something (x)")
    db.close()
    with pytest.raises(backups.BackupError, match="not an Earr database"):
        _ = backups.save_upload(other.read_bytes())

    future = tmp_path / "future.sqlite"
    make_database(future, [], revision="ffffffffffff")
    with pytest.raises(backups.BackupError, match="newer version"):
        _ = backups.save_upload(future.read_bytes())

    good = tmp_path / "good.sqlite"
    make_database(good, ["carol"])
    saved = backups.save_upload(good.read_bytes())
    assert saved.kind == "uploaded"
    assert [b.kind for b in backups.list_backups()] == ["uploaded"]
