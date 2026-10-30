"""One book: what it is, where it stands, its series, and more by the author."""

from typing import Annotated

from aiohttp import ClientSession
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Security
from sqlmodel import Session

from app.internal.audible.types import get_region_from_settings
from app.internal.auth.authentication import DetailedUser, EarrAuth
from app.internal.book_page import build
from app.internal.models import EventEnum
from app.internal.notifications import send_all_notifications
from app.internal.query import background_start_query
from app.internal.ranking.quality import quality_config
from app.internal.request_limits import allowance
from app.internal.requesting import create_request, starts_download
from app.routers.api.requests import create_request as api_create_request
from app.util.connection import get_connection
from app.util.db import get_session
from app.util.templates import catalog_response, catalog_response_toast
from app.util.toast import toast_response

router = APIRouter(prefix="/book")


@router.get("/{asin}")
async def book_page(
    asin: str,
    session: Annotated[Session, Depends(get_session)],
    client_session: Annotated[ClientSession, Depends(get_connection)],
    user: Annotated[DetailedUser, Security(EarrAuth())],
):
    page = await build(session, client_session, user, asin)
    if page is None:
        raise HTTPException(status_code=404, detail="Book not found")
    return catalog_response(
        "Book.Detail",
        user=user,
        page=page,
        auto_start_download=quality_config.get_auto_download(session),
        region=get_region_from_settings(),
        allowance=allowance(session, user),
    )


@router.post("/{asin}/hx-request")
async def request_one(
    asin: str,
    session: Annotated[Session, Depends(get_session)],
    client_session: Annotated[ClientSession, Depends(get_connection)],
    background_task: BackgroundTasks,
    user: Annotated[DetailedUser, Security(EarrAuth())],
):
    try:
        book = await api_create_request(
            session=session,
            client_session=client_session,
            user=user,
            background_task=background_task,
            asin_or_uuid=asin,
        )
    except HTTPException as e:
        return toast_response(str(e.detail), "error")
    return catalog_response_toast(
        "Book.RequestButton",
        "Requested. It's on your wishlist.",
        "success",
        book_with_requests=book,
        user=user,
        auto_start_download=quality_config.get_auto_download(session),
    )


@router.post("/{asin}/hx-request-series")
async def request_series(
    asin: str,
    session: Annotated[Session, Depends(get_session)],
    client_session: Annotated[ClientSession, Depends(get_connection)],
    background_task: BackgroundTasks,
    user: Annotated[DetailedUser, Security(EarrAuth())],
):
    """Requests every book in the series that nobody has asked for yet."""
    page = await build(session, client_session, user, asin)
    if page is None or not page.series:
        return toast_response("This book isn't part of a series", "error")

    added = 0
    stopped_by_limit = False
    for book in page.missing_in_series:
        remaining = allowance(session, user)
        if remaining is not None and remaining.reached:
            stopped_by_limit = True
            break
        request = create_request(session, user, book)
        if request is None:
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

    message = (
        f"Requested {added} book{'' if added == 1 else 's'} from {page.series.title}"
    )
    if stopped_by_limit:
        message += ". You've reached your request limit, so the rest weren't requested"
    return toast_response(message, "info" if stopped_by_limit else "success")
