from pathlib import Path
from typing import Annotated

from aiohttp import ClientSession
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Security
from pydantic import BaseModel
from sqlmodel import Session, col, desc, select

from app.internal.audiobookshelf.client import background_abs_trigger_scan
from app.internal.audiobookshelf.config import abs_config
from app.internal.auth.authentication import AnyAuth, DetailedUser
from app.internal.library.config import LibraryMisconfigured, library_config
from app.internal.library.metadata import write_metadata
from app.internal.library.organizer import OrganizeError, organize, resolve_target_dir
from app.internal.library.watcher import get_book, scan
from app.internal.models import (
    GroupEnum,
    LibraryImport,
    LibraryImportStatusEnum,
)
from app.util.connection import get_connection
from app.util.db import get_session

router = APIRouter(prefix="/library", tags=["Library"])


@router.get("/imports", response_model=list[LibraryImport])
def list_imports(
    session: Annotated[Session, Depends(get_session)],
    _: Annotated[DetailedUser, Security(AnyAuth(GroupEnum.admin))],
    status: LibraryImportStatusEnum | None = None,
    limit: int = 50,
):
    """Lists the downloads the library watcher knows about, newest first."""
    query = select(LibraryImport).order_by(desc(col(LibraryImport.created_at)))
    if status is not None:
        query = query.where(LibraryImport.status == status)
    return session.exec(query.limit(max(1, min(limit, 200)))).all()


class ScanResult(BaseModel):
    imported: int


@router.post("/scan", response_model=ScanResult)
async def scan_now(
    session: Annotated[Session, Depends(get_session)],
    client_session: Annotated[ClientSession, Depends(get_connection)],
    background_task: BackgroundTasks,
    _: Annotated[DetailedUser, Security(AnyAuth(GroupEnum.admin))],
):
    """Runs the completed downloads scan immediately instead of waiting for the interval."""
    try:
        library_config.raise_if_invalid(session)
    except LibraryMisconfigured as e:
        raise HTTPException(status_code=400, detail=str(e))

    imported = await scan(session, client_session)
    if imported and abs_config.is_valid(session):
        background_task.add_task(background_abs_trigger_scan)
    return ScanResult(imported=imported)


class ImportBody(BaseModel):
    asin_or_uuid: str
    source_path: str
    """Absolute path, or a path relative to the completed downloads folder."""


class ImportResult(BaseModel):
    target_path: str


def resolve_source(session: Session, source_path: str) -> Path:
    """Confines a caller supplied path to the completed downloads folder."""
    download_dir = library_config.get_download_dir(session)
    if download_dir is None:
        raise HTTPException(
            status_code=400, detail="Completed downloads folder not set"
        )

    download_dir = download_dir.resolve()
    source = Path(source_path)
    source = source if source.is_absolute() else download_dir / source
    source = source.resolve()

    if not source.is_relative_to(download_dir):
        raise HTTPException(
            status_code=400,
            detail=f"Path has to be inside the completed downloads folder ({download_dir})",
        )
    if not source.exists():
        raise HTTPException(status_code=404, detail=f"Path not found: {source}")
    return source


@router.post("/import", response_model=ImportResult)
def import_download(
    body: ImportBody,
    session: Annotated[Session, Depends(get_session)],
    background_task: BackgroundTasks,
    _: Annotated[DetailedUser, Security(AnyAuth(GroupEnum.admin))],
):
    """Organizes a download that the watcher did not pick up by itself.

    Useful when the release title and the folder the download client created
    are too different to match automatically.
    """
    book = get_book(session, body.asin_or_uuid)
    if book is None:
        raise HTTPException(status_code=404, detail="Book not found")

    source = resolve_source(session, body.source_path)

    try:
        target_dir = organize(
            source=source,
            target_dir=resolve_target_dir(session, book),
            mode=library_config.get_mode(session),
            overwrite=library_config.get_overwrite(session),
        )
    except (OrganizeError, OSError) as e:
        raise HTTPException(status_code=400, detail=str(e))

    if library_config.get_write_metadata(session):
        write_metadata(target_dir, book, library_config.get_overwrite(session))

    entry = session.exec(
        select(LibraryImport).where(
            LibraryImport.asin_or_uuid == body.asin_or_uuid,
            LibraryImport.status == LibraryImportStatusEnum.pending,
        )
    ).first()
    if entry is None:
        entry = LibraryImport(
            asin_or_uuid=body.asin_or_uuid,
            book_title=book.title,
            release_title=source.name,
        )
    entry.status = LibraryImportStatusEnum.imported
    entry.source_path = str(source)
    entry.target_path = str(target_dir)
    entry.error = None
    session.add(entry)
    session.commit()

    if abs_config.is_valid(session):
        background_task.add_task(background_abs_trigger_scan)

    return ImportResult(target_path=str(target_dir))
