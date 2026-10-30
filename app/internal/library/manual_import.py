"""Finding downloads the library watcher couldn't match, so they can be imported by hand.

The usual cause is a download client that renamed the folder: a release listed
as "Silverthorn by Raymond E Feist [ENG / M4B]" arriving as "03 Silverthorn".
"""

import os
from datetime import datetime
from pathlib import Path

from pydantic import BaseModel
from rapidfuzz import fuzz, utils
from sqlmodel import Session, col, select

from app.internal.library.config import library_config
from app.internal.library.organizer import has_incomplete_files
from app.internal.models import (
    Audiobook,
    AudiobookRequest,
    DownloadStatusEnum,
    LibraryImport,
    LibraryImportStatusEnum,
    ManualBookRequest,
)

MAX_CANDIDATES = 100
SUGGEST_THRESHOLD = 70
"""How closely a folder name has to resemble a request to be preselected for it."""


class ImportTarget(BaseModel):
    """A request a download can be imported for."""

    asin_or_uuid: str
    label: str
    match_text: str


class ImportCandidate(BaseModel):
    path: str
    name: str
    folder: str
    """The completed downloads folder it was found in."""
    size: int
    files: int
    modified: datetime
    incomplete: bool
    """Partial files are still being written, so it isn't ready."""
    suggestion: str | None
    """asin_or_uuid of the request it most likely belongs to."""

    @property
    def size_label(self) -> str:
        size = float(self.size)
        for unit in ("B", "KB", "MB", "GB"):
            if size < 1024:
                return (
                    f"{size:.0f} {unit}"
                    if unit in ("B", "KB")
                    else f"{size:.1f} {unit}"
                )
            size /= 1024
        return f"{size:.1f} TB"


def import_targets(session: Session) -> list[ImportTarget]:
    """Requested books that aren't confirmed in the library yet."""
    targets: list[ImportTarget] = []
    books = session.exec(
        select(Audiobook).where(
            col(Audiobook.asin).in_(select(AudiobookRequest.asin)),
            col(Audiobook.download_status).is_distinct_from(
                DownloadStatusEnum.downloaded
            ),
        )
    ).all()
    for book in books:
        author = book.authors[0] if book.authors else ""
        targets.append(
            ImportTarget(
                asin_or_uuid=book.asin,
                label=f"{book.title} by {author}" if author else book.title,
                match_text=f"{book.title} {author}",
            )
        )
    manual = session.exec(
        select(ManualBookRequest).where(
            col(ManualBookRequest.download_status).is_distinct_from(
                DownloadStatusEnum.downloaded
            )
        )
    ).all()
    for request in manual:
        author = request.authors[0] if request.authors else ""
        targets.append(
            ImportTarget(
                asin_or_uuid=str(request.id),
                label=f"{request.title} by {author} (manual)"
                if author
                else f"{request.title} (manual)",
                match_text=f"{request.title} {author}",
            )
        )
    return sorted(targets, key=lambda t: t.label.lower())


def _measure(path: Path) -> tuple[int, int]:
    """Total size and number of files."""
    if path.is_file():
        return path.stat().st_size, 1
    size = files = 0
    for root, _, names in os.walk(path):
        for name in names:
            try:
                size += (Path(root) / name).stat().st_size
            except OSError:
                continue
            files += 1
    return size, files


def _suggest(name: str, targets: list[ImportTarget]) -> str | None:
    wanted = utils.default_process(name)
    best, best_score = None, float(SUGGEST_THRESHOLD)
    for target in targets:
        score = fuzz.token_set_ratio(wanted, utils.default_process(target.match_text))
        if score >= best_score:
            best, best_score = target.asin_or_uuid, score
    return best


def list_candidates(
    session: Session, targets: list[ImportTarget]
) -> list[ImportCandidate]:
    """Top level entries of each completed downloads folder that haven't been imported."""
    imported = {
        path
        for path in session.exec(
            select(LibraryImport.source_path).where(
                LibraryImport.status == LibraryImportStatusEnum.imported
            )
        ).all()
        if path
    }

    entries: list[tuple[Path, Path, float]] = []
    for folder in library_config.get_download_dirs(session):
        if not folder.is_dir():
            continue
        for entry in folder.iterdir():
            if entry.name.startswith(".") or str(entry.resolve()) in imported:
                continue
            try:
                entries.append((folder, entry, entry.stat().st_mtime))
            except OSError:
                continue

    entries.sort(key=lambda e: e[2], reverse=True)
    candidates: list[ImportCandidate] = []
    for folder, entry, mtime in entries[:MAX_CANDIDATES]:
        size, files = _measure(entry)
        candidates.append(
            ImportCandidate(
                path=str(entry),
                name=entry.name,
                folder=str(folder),
                size=size,
                files=files,
                modified=datetime.fromtimestamp(mtime),
                incomplete=has_incomplete_files(entry),
                suggestion=_suggest(
                    entry.stem if entry.is_file() else entry.name, targets
                ),
            )
        )
    return candidates
