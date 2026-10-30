"""Following authors and series, so their new books are requested automatically."""

import asyncio
import uuid
from typing import Annotated, Literal

from aiohttp import ClientSession
from fastapi import APIRouter, Depends, Form, HTTPException, Security
from pydantic import BaseModel
from sqlmodel import Session

from app.internal.audible.catalog import get_book_details
from app.internal.audible.search import search_audible_books
from app.internal.auth.authentication import DetailedUser, EarrAuth
from app.internal.automation_scheduler import lifespan
from app.internal.follows import (
    check_follow,
    find_follow,
    list_follows,
    start_following,
    stop_following,
    summaries,
)
from app.internal.models import Follow, FollowKindEnum
from app.util.connection import get_connection
from app.util.db import get_session
from app.util.templates import catalog_response, catalog_response_toast
from app.util.toast import toast_response

router = APIRouter(prefix="/following", lifespan=lifespan)


class FollowCandidate(BaseModel):
    kind: FollowKindEnum
    audible_id: str
    name: str
    detail: str
    """A book of theirs, so two people with the same name can be told apart."""
    following: bool


@router.get("")
def following(
    session: Annotated[Session, Depends(get_session)],
    user: Annotated[DetailedUser, Security(EarrAuth())],
):
    return catalog_response(
        "Following.Index",
        user=user,
        follows=summaries(session, list_follows(session, user)),
    )


@router.get("/hx-search")
async def search(
    session: Annotated[Session, Depends(get_session)],
    client_session: Annotated[ClientSession, Depends(get_connection)],
    user: Annotated[DetailedUser, Security(EarrAuth())],
    q: str = "",
):
    """Authors and series from the books matching a search."""
    query = q.strip()
    candidates: list[FollowCandidate] = []
    if query:
        books = (await search_audible_books(client_session, query, num_results=20))[:10]
        found = await asyncio.gather(
            *(get_book_details(client_session, book.asin) for book in books)
        )
        seen: set[tuple[FollowKindEnum, str]] = set()
        seen_names: set[str] = set()
        for book, details in zip(books, found, strict=True):
            if details is None:
                continue
            for author in details.authors:
                key = (FollowKindEnum.author, author.asin or author.name)
                # Audible sometimes has two ids for one person; one entry will do
                if key in seen or author.name.casefold() in seen_names:
                    continue
                seen.add(key)
                seen_names.add(author.name.casefold())
                candidates.append(
                    FollowCandidate(
                        kind=FollowKindEnum.author,
                        audible_id=key[1],
                        name=author.name,
                        detail=f"Wrote {book.title}",
                        following=find_follow(session, user.username, *key) is not None,
                    )
                )
            for series in details.series:
                key = (FollowKindEnum.series, series.asin)
                if key in seen:
                    continue
                seen.add(key)
                candidates.append(
                    FollowCandidate(
                        kind=FollowKindEnum.series,
                        audible_id=series.asin,
                        name=series.title,
                        detail=f"Includes {book.title}",
                        following=find_follow(session, user.username, *key) is not None,
                    )
                )
    # authors first, in the order they turned up
    candidates.sort(key=lambda c: c.kind != FollowKindEnum.author)
    return catalog_response(
        "Following.SearchResults", candidates=candidates[:16], query=query
    )


@router.post("/hx-follow")
async def follow(
    kind: Annotated[FollowKindEnum, Form()],
    audible_id: Annotated[str, Form()],
    name: Annotated[str, Form()],
    session: Annotated[Session, Depends(get_session)],
    client_session: Annotated[ClientSession, Depends(get_connection)],
    user: Annotated[DetailedUser, Security(EarrAuth())],
    style: Literal["button", "badge"] = "button",
):
    created = await start_following(
        session, client_session, user, kind, audible_id.strip(), name.strip()
    )
    return catalog_response_toast(
        "FollowButton",
        f"Following {created.name}. New books will be requested for you.",
        "success",
        kind=kind.value,
        audible_id=created.audible_id,
        label=created.name,
        follow=created,
        style=style,
    )


@router.delete("/hx-follow/{follow_id}")
def unfollow(
    follow_id: uuid.UUID,
    session: Annotated[Session, Depends(get_session)],
    user: Annotated[DetailedUser, Security(EarrAuth())],
    style: Literal["button", "badge", "row"] = "button",
):
    existing = session.get(Follow, follow_id)
    if existing is None or not stop_following(session, user, follow_id):
        raise HTTPException(status_code=404, detail="Not following that")
    if style == "row":
        return toast_response(f"Stopped following {existing.name}")
    return catalog_response(
        "FollowButton",
        kind=existing.kind.value,
        audible_id=existing.audible_id,
        label=existing.name,
        follow=None,
        style=style,
    )


@router.post("/{follow_id}/hx-check")
async def check_now(
    follow_id: uuid.UUID,
    session: Annotated[Session, Depends(get_session)],
    client_session: Annotated[ClientSession, Depends(get_connection)],
    user: Annotated[DetailedUser, Security(EarrAuth())],
):
    found = session.get(Follow, follow_id)
    if found is None or (found.user_username != user.username and not user.is_admin()):
        raise HTTPException(status_code=404, detail="Not following that")
    requested = await check_follow(session, client_session, found)
    if found.last_error:
        return toast_response(found.last_error, "error")
    if requested:
        books = f"{len(requested)} new book{'' if len(requested) == 1 else 's'}"
        return toast_response(f"Requested {books} from {found.name}")
    return toast_response(f"Nothing new from {found.name}", "info")
