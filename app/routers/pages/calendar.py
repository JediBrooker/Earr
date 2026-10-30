"""When requested books come out."""

from typing import Annotated

from fastapi import APIRouter, Depends, Security
from sqlmodel import Session

from app.internal.auth.authentication import DetailedUser, EarrAuth
from app.internal.release_calendar import build
from app.util.db import get_session
from app.util.templates import catalog_response

router = APIRouter(prefix="/calendar")


@router.get("")
def calendar(
    session: Annotated[Session, Depends(get_session)],
    user: Annotated[DetailedUser, Security(EarrAuth())],
):
    return catalog_response("Calendar.Index", user=user, calendar=build(session, user))
