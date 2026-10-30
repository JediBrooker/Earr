"""Downloads on their way to the library, and their history."""

from typing import Annotated

from aiohttp import ClientSession
from fastapi import APIRouter, Depends, Security
from sqlmodel import Session

from app.internal.activity import history, queue
from app.internal.auth.authentication import DetailedUser, EarrAuth
from app.internal.library.config import library_config
from app.util.connection import get_connection
from app.util.db import get_session
from app.util.templates import catalog_response

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
