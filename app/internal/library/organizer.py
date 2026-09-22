"""Places the files of a finished download into the library folder structure."""

import errno
import os
import shutil
from pathlib import Path
from typing import Iterator

from sqlmodel import Session

from app.internal.library.config import library_config
from app.internal.library.naming import build_tokens, render_template
from app.internal.models import Audiobook, ManualBookRequest, OrganizeModeEnum
from app.util.log import logger

INCOMPLETE_SUFFIXES = (".part", ".!qb", ".!ut", ".crdownload", ".tmp")
"""Markers download clients leave behind while a file is still being written."""


class OrganizeError(Exception):
    """Raised when a download could not be placed into the library."""


def iter_source_files(source: Path) -> Iterator[Path]:
    """Yields every file below `source` as a path relative to it.

    A single file download yields just its own name so that it still ends up
    inside a folder of its own.
    """
    if source.is_file():
        yield Path(source.name)
        return
    for root, _, files in os.walk(source):
        for name in sorted(files):
            yield (Path(root) / name).relative_to(source)


def has_incomplete_files(source: Path) -> bool:
    return any(
        path.name.lower().endswith(INCOMPLETE_SUFFIXES)
        for path in ([source] if source.is_file() else source.rglob("*"))
        if path.is_file()
    )


def resolve_target_dir(session: Session, book: Audiobook | ManualBookRequest) -> Path:
    """Builds the absolute library folder a book belongs into.

    Raises an OrganizeError if the rendered path would escape the library root,
    which can happen when a book title consists only of dots.
    """
    root = library_config.get_root_dir(session)
    if root is None:
        raise OrganizeError("Library folder not set")

    template = library_config.get_folder_template(session)
    segments = render_template(template, build_tokens(book), validate=False)
    if not segments:
        raise OrganizeError(
            f"Folder structure '{template}' renders to an empty path for '{book.title}'"
        )

    root = root.resolve()
    target = root.joinpath(*segments)
    if not target.resolve().is_relative_to(root):
        raise OrganizeError(
            f"Refusing to write outside of the library folder: {target}"
        )
    return target


def _link(source: Path, target: Path) -> None:
    try:
        os.link(source, target)
    except OSError as e:
        if e.errno == errno.EXDEV:
            raise OrganizeError(
                "Hardlinking only works when the completed downloads folder and the"
                + " library folder are on the same filesystem. Mount them from the"
                + " same volume or switch the mode to copy."
            ) from e
        if e.errno == errno.EPERM:
            raise OrganizeError(
                f"The filesystem holding {target.parent} does not support hardlinks."
                + " Switch the mode to copy."
            ) from e
        raise


def organize(
    *,
    source: Path,
    target_dir: Path,
    mode: OrganizeModeEnum,
    overwrite: bool,
) -> Path:
    """Copies, moves or hardlinks `source` into `target_dir`.

    The layout below `source` is kept, so a download that already separates
    files into disc folders stays that way. Returns the folder that was written.
    """
    if not source.exists():
        raise OrganizeError(f"Download not found: {source}")

    files = list(iter_source_files(source))
    if not files:
        raise OrganizeError(f"Download contains no files: {source}")

    created: list[Path] = []
    try:
        target_dir.mkdir(parents=True, exist_ok=True)
        for relative in files:
            source_file = source if source.is_file() else source / relative
            target_file = target_dir / relative

            if target_file.exists():
                if not overwrite:
                    logger.debug(
                        "Library: skipping existing file", target=str(target_file)
                    )
                    continue
                target_file.unlink()

            target_file.parent.mkdir(parents=True, exist_ok=True)
            match mode:
                case OrganizeModeEnum.hardlink:
                    _link(source_file, target_file)
                case OrganizeModeEnum.copy:
                    _ = shutil.copy2(source_file, target_file)
                case OrganizeModeEnum.move:
                    _ = shutil.move(str(source_file), str(target_file))
            created.append(target_file)
    except Exception:
        # A half written book is worse than none, so undo what can be undone.
        # Moved files are already gone from the source and are left in place.
        if mode != OrganizeModeEnum.move:
            _rollback(created)
        raise

    if mode == OrganizeModeEnum.move:
        _prune_empty_dirs(source)

    logger.info(
        "Library: organized download",
        source=str(source),
        target=str(target_dir),
        mode=mode.value,
        files=len(created),
    )
    return target_dir


def _rollback(created: list[Path]) -> None:
    for path in reversed(created):
        try:
            path.unlink(missing_ok=True)
        except OSError as e:
            logger.warning(
                "Library: failed to clean up after an error",
                path=str(path),
                error=str(e),
            )


def _prune_empty_dirs(source: Path) -> None:
    """Removes the leftovers of a moved download, deepest folder first."""
    if source.is_file():
        return
    for root, _, _ in os.walk(source, topdown=False):
        try:
            if not os.listdir(root):
                os.rmdir(root)
        except OSError as e:
            logger.debug(
                "Library: could not remove leftover folder", path=root, error=str(e)
            )
