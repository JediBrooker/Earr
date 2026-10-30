"""Downloads on their way to the library, their history, and importing by hand."""

from typing import Annotated

from aiohttp import ClientSession
from fastapi import APIRouter, BackgroundTasks, Depends, Form, HTTPException, Security
from sqlmodel import Session

from app.internal.activity import history, queue
from app.internal.auth.authentication import DetailedUser, EarrAuth
from app.internal.library.config import library_config
from app.internal.library.manual_import import (
    ImportCandidate,
    ImportTarget,
    import_targets,
    list_candidates,
)
from app.internal.models import GroupEnum
from app.routers.api.library import ImportBody, import_download
from app.util.connection import get_connection
from app.util.db import get_session
from app.util.templates import catalog_response, catalog_response_toast

router = APIRouter(prefix="/activity")


@router.get("")
def activity_queue(
    user: Annotated[DetailedUser, Security(EarrAuth())],
):
    # the rows ask the download clients, so they load after the page
    return catalog_response("Activity.Queue", user=user)


@router.get("/hx-queue")
async def queue_rows(
    session: Annotated[Session, Depends(get_session)],
    client_session: Annotated[ClientSession, Depends(get_connection)],
    user: Annotated[DetailedUser, Security(EarrAuth())],
):
    return catalog_response(
        "Activity.QueueTable",
        user=user,
        items=await queue(session, client_session, user),
        organizing=library_config.get_enabled(session),
    )


@router.get("/history")
def activity_history(
    session: Annotated[Session, Depends(get_session)],
    user: Annotated[DetailedUser, Security(EarrAuth())],
):
    return catalog_response("Activity.History", user=user, items=history(session, user))


def _import_choices(
    session: Session,
) -> tuple[list[ImportTarget], list[ImportCandidate]]:
    targets = import_targets(session)
    return targets, list_candidates(session, targets)


@router.get("/import")
def manual_import(
    session: Annotated[Session, Depends(get_session)],
    admin_user: Annotated[DetailedUser, Security(EarrAuth(GroupEnum.admin))],
):
    root = library_config.get_root_dir(session)
    targets, candidates = _import_choices(session)
    return catalog_response(
        "Activity.Import",
        user=admin_user,
        has_download_dirs=bool(library_config.get_download_dirs(session)),
        root_dir=str(root) if root else None,
        targets=targets,
        candidates=candidates,
    )


@router.post("/import")
def import_one(
    source_path: Annotated[str, Form()],
    asin_or_uuid: Annotated[str, Form()],
    session: Annotated[Session, Depends(get_session)],
    background_task: BackgroundTasks,
    admin_user: Annotated[DetailedUser, Security(EarrAuth(GroupEnum.admin))],
):
    try:
        result = import_download(
            ImportBody(asin_or_uuid=asin_or_uuid, source_path=source_path),
            session,
            background_task,
            admin_user,
        )
    except HTTPException as e:
        targets, candidates = _import_choices(session)
        return catalog_response_toast(
            "Activity.ImportList",
            str(e.detail),
            "error",
            targets=targets,
            candidates=candidates,
        )
    targets, candidates = _import_choices(session)
    return catalog_response_toast(
        "Activity.ImportList",
        f"Imported into {result.target_path}",
        "success",
        targets=targets,
        candidates=candidates,
    )
