"""Importing a Goodreads or StoryGraph want-to-read list into requests."""

import asyncio
from typing import Annotated

from aiohttp import ClientSession
from fastapi import (
    APIRouter,
    BackgroundTasks,
    Depends,
    File,
    Form,
    Security,
    UploadFile,
)
from sqlmodel import Session

from app.internal.audible.catalog import get_book_details
from app.internal.auth.authentication import DetailedUser, EarrAuth
from app.internal.db_queries import get_wishlist_counts
from app.internal.models import Audiobook, EventEnum
from app.internal.notifications import send_all_notifications
from app.internal.query import background_start_query
from app.internal.reading_list import (
    MAX_ENTRIES,
    ReadingListError,
    match_entries,
    parse_export,
)
from app.internal.request_limits import allowance
from app.internal.requesting import create_request, starts_download
from app.util.connection import get_connection
from app.util.db import get_session
from app.util.templates import catalog_response
from app.util.toast import toast_response

router = APIRouter(prefix="/import")

MAX_UPLOAD = 5 * 1024 * 1024


@router.get("")
def import_page(
    session: Annotated[Session, Depends(get_session)],
    user: Annotated[DetailedUser, Security(EarrAuth())],
):
    return catalog_response(
        "Wishlist.Import",
        user=user,
        counts=get_wishlist_counts(session, user),
        max_entries=MAX_ENTRIES,
    )


@router.post("/hx-match")
async def match(
    session: Annotated[Session, Depends(get_session)],
    client_session: Annotated[ClientSession, Depends(get_connection)],
    user: Annotated[DetailedUser, Security(EarrAuth())],
    file: Annotated[UploadFile, File()],
):
    data = await file.read(MAX_UPLOAD + 1)
    if len(data) > MAX_UPLOAD:
        return toast_response("That file is too big for a reading list export", "error")
    try:
        parsed = parse_export(data.decode("utf-8", errors="replace"))
    except ReadingListError as e:
        return toast_response(str(e), "error")
    if not parsed.entries:
        return toast_response(
            f"Nothing on your {parsed.source} want-to-read shelf", "info"
        )
    matches = await match_entries(session, client_session, user, parsed.entries)
    return catalog_response(
        "Wishlist.ImportMatches",
        matches=matches,
        source=parsed.source,
        skipped=parsed.skipped,
        allowance=allowance(session, user),
    )


@router.post("/hx-request")
async def request_selected(
    session: Annotated[Session, Depends(get_session)],
    client_session: Annotated[ClientSession, Depends(get_connection)],
    background_task: BackgroundTasks,
    user: Annotated[DetailedUser, Security(EarrAuth())],
    asin: Annotated[list[str] | None, Form()] = None,
):
    asins = list(dict.fromkeys(asin or []))[:MAX_ENTRIES]
    if not asins:
        return toast_response("Tick the books to request first", "info")

    async def lookup(one: str) -> Audiobook | None:
        stored = session.get(Audiobook, one)
        if stored is not None:
            return stored
        details = await get_book_details(client_session, one)
        return details.book if details else None

    books = await asyncio.gather(*(lookup(one) for one in asins))
    added = 0
    stopped_by_limit = False
    for book in books:
        if book is None:
            continue
        remaining = allowance(session, user)
        if remaining is not None and remaining.reached:
            stopped_by_limit = True
            break
        if create_request(session, user, book) is None:
            continue
        added += 1
        background_task.add_task(
            send_all_notifications,
            event_type=EventEnum.on_new_request,
            book_asin=book.asin,
        )
        if starts_download(session, user, book):
            background_task.add_task(
                background_start_query, asin_or_uuid=book.asin, auto_download=True
            )

    message = f"Requested {added} book{'' if added == 1 else 's'}"
    if stopped_by_limit:
        message += ". You reached your request limit, so the rest weren't requested"
    return toast_response(message, "info" if stopped_by_limit else "success")
