from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Response, Security
from pydantic import BaseModel
from sqlmodel import Session

from app.internal.auth.authentication import AnyAuth, DetailedUser
from app.internal.library.config import MIN_SCAN_INTERVAL, library_config
from app.internal.library.naming import (
    TEMPLATE_EXAMPLES,
    TOKEN_DESCRIPTIONS,
    TemplateError,
    validate_template,
)
from app.internal.library.scheduler import reschedule
from app.internal.models import GroupEnum, OrganizeModeEnum
from app.util.db import get_session

router = APIRouter(prefix="/library")


class LibrarySettings(BaseModel):
    enabled: bool
    mode: OrganizeModeEnum
    download_dir: str
    root_dir: str
    folder_template: str
    scan_interval: int
    match_threshold: int
    overwrite: bool
    write_metadata: bool


def read_settings(session: Session) -> LibrarySettings:
    download_dir = library_config.get_download_dir(session)
    root_dir = library_config.get_root_dir(session)
    return LibrarySettings(
        enabled=library_config.get_enabled(session),
        mode=library_config.get_mode(session),
        download_dir=str(download_dir) if download_dir else "",
        root_dir=str(root_dir) if root_dir else "",
        folder_template=library_config.get_folder_template(session),
        scan_interval=library_config.get_scan_interval(session),
        match_threshold=library_config.get_match_threshold(session),
        overwrite=library_config.get_overwrite(session),
        write_metadata=library_config.get_write_metadata(session),
    )


@router.get("", response_model=LibrarySettings)
def get_library_settings(
    session: Annotated[Session, Depends(get_session)],
    _: Annotated[DetailedUser, Security(AnyAuth(GroupEnum.admin))],
):
    return read_settings(session)


class LibraryTokens(BaseModel):
    tokens: dict[str, str]
    examples: list[str]


@router.get("/placeholders", response_model=LibraryTokens)
def get_library_placeholders(
    _: Annotated[DetailedUser, Security(AnyAuth(GroupEnum.admin))],
):
    """Lists every placeholder that can be used in the folder structure."""
    return LibraryTokens(tokens=TOKEN_DESCRIPTIONS, examples=TEMPLATE_EXAMPLES)


class UpdateLibrarySettings(BaseModel):
    enabled: bool
    mode: OrganizeModeEnum
    download_dir: str
    root_dir: str
    folder_template: str
    scan_interval: int
    match_threshold: int
    overwrite: bool
    write_metadata: bool


@router.patch("", status_code=204)
def update_library_settings(
    body: UpdateLibrarySettings,
    session: Annotated[Session, Depends(get_session)],
    _: Annotated[DetailedUser, Security(AnyAuth(GroupEnum.admin))],
):
    try:
        validate_template(body.folder_template)
    except TemplateError as e:
        raise HTTPException(status_code=422, detail=str(e))

    if body.enabled and not body.download_dir.strip():
        raise HTTPException(
            status_code=422, detail="Completed downloads folder is required"
        )
    if body.enabled and not body.root_dir.strip():
        raise HTTPException(status_code=422, detail="Library folder is required")
    if body.scan_interval < MIN_SCAN_INTERVAL:
        raise HTTPException(
            status_code=422,
            detail=f"Scan interval has to be at least {MIN_SCAN_INTERVAL} seconds",
        )
    if not 0 <= body.match_threshold <= 100:
        raise HTTPException(
            status_code=422, detail="Match threshold has to be between 0 and 100"
        )

    library_config.set_enabled(session, body.enabled)
    library_config.set_mode(session, body.mode)
    library_config.set_download_dir(session, body.download_dir.strip())
    library_config.set_root_dir(session, body.root_dir.strip())
    library_config.set_folder_template(session, body.folder_template)
    library_config.set_scan_interval(session, body.scan_interval)
    library_config.set_match_threshold(session, body.match_threshold)
    library_config.set_overwrite(session, body.overwrite)
    library_config.set_write_metadata(session, body.write_metadata)

    reschedule(library_config.get_scan_interval(session))

    return Response(status_code=204)
