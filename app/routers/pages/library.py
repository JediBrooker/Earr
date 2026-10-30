"""The Library page: what is already in the audiobook library."""

import re
from typing import Annotated

from aiohttp import ClientSession
from fastapi import APIRouter, Depends, HTTPException, Response, Security
from sqlmodel import Session

from app.internal.audiobookshelf.config import abs_config
from app.internal.audiobookshelf.library import (
    browse_audiobookshelf,
    browse_earr,
    fetch_cover,
)
from app.internal.auth.authentication import DetailedUser, EarrAuth
from app.internal.env_settings import Settings
from app.util.connection import get_connection
from app.util.db import get_session
from app.util.templates import catalog_response

router = APIRouter(prefix="/library")

_ITEM_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


@router.get("")
async def library(
    session: Annotated[Session, Depends(get_session)],
    client_session: Annotated[ClientSession, Depends(get_connection)],
    user: Annotated[DetailedUser, Security(EarrAuth())],
    q: str = "",
    page: int = 0,
):
    page = max(0, page)
    query = q.strip()
    if abs_config.is_connected(session):
        result = await browse_audiobookshelf(
            session,
            client_session,
            page=page,
            query=query,
            cover_path=f"{Settings().app.base_url.rstrip('/')}/library/cover",
        )
    else:
        result = browse_earr(session, page=page, query=query)
    return catalog_response("Library.Index", user=user, result=result, query=query)


@router.get("/cover/{item_id}")
async def cover(
    item_id: str,
    session: Annotated[Session, Depends(get_session)],
    client_session: Annotated[ClientSession, Depends(get_connection)],
    _: Annotated[DetailedUser, Security(EarrAuth())],
):
    if not _ITEM_ID.match(item_id):
        raise HTTPException(status_code=404)
    found = await fetch_cover(session, client_session, item_id)
    if found is None:
        raise HTTPException(status_code=404)
    content, media_type = found
    return Response(
        content,
        media_type=media_type,
        headers={"Cache-Control": "private, max-age=86400"},
    )
