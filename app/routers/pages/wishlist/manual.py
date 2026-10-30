import uuid
from typing import Annotated

from fastapi import APIRouter, BackgroundTasks, Depends, Form, Security
from sqlmodel import Session

from app.internal.audiobookshelf.client import background_abs_trigger_scan
from app.internal.audiobookshelf.config import abs_config
from app.internal.auth.authentication import EarrAuth, DetailedUser
from app.internal.db_queries import get_all_manual_requests, get_wishlist_counts
from app.internal.models import GroupEnum
from app.routers.api.requests import (
    DeclineBody,
    approve_request,
    decline_request,
    delete_manual_request,
    mark_manual_downloaded,
)
from app.util.db import get_session
from app.util.templates import catalog_response, catalog_response_toast

router = APIRouter(prefix="/manual")


@router.get("")
async def manual(
    session: Annotated[Session, Depends(get_session)],
    user: Annotated[DetailedUser, Security(EarrAuth())],
):
    results = get_all_manual_requests(session, user)
    counts = get_wishlist_counts(session, user)
    return catalog_response(
        "Wishlist.Manual",
        user=user,
        results=results,
        counts=counts,
    )


@router.patch("/hx-add/{id}")
async def downloaded_manual(
    id: uuid.UUID,
    session: Annotated[Session, Depends(get_session)],
    background_task: BackgroundTasks,
    admin_user: Annotated[DetailedUser, Security(EarrAuth(GroupEnum.admin))],
):
    await mark_manual_downloaded(id, session, background_task, admin_user)

    results = get_all_manual_requests(session, admin_user)
    counts = get_wishlist_counts(session, admin_user)

    if abs_config.is_valid(session):
        background_task.add_task(background_abs_trigger_scan)

    return catalog_response(
        "Wishlist.ManualWishlist",
        user=admin_user,
        results=results,
        counts=counts,
        update_tablist=True,
    )


@router.post("/hx-approve/{id}")
async def approve_manual(
    id: uuid.UUID,
    session: Annotated[Session, Depends(get_session)],
    background_task: BackgroundTasks,
    admin_user: Annotated[DetailedUser, Security(EarrAuth(GroupEnum.admin))],
):
    _ = await approve_request(str(id), session, background_task, admin_user)
    return catalog_response_toast(
        "Wishlist.ManualWishlist",
        "Approved",
        "success",
        user=admin_user,
        results=get_all_manual_requests(session, admin_user),
        counts=get_wishlist_counts(session, admin_user),
        update_tablist=True,
    )


@router.post("/hx-decline/{id}")
async def decline_manual(
    id: uuid.UUID,
    session: Annotated[Session, Depends(get_session)],
    admin_user: Annotated[DetailedUser, Security(EarrAuth(GroupEnum.admin))],
    reason: Annotated[str, Form()] = "",
):
    _ = await decline_request(str(id), DeclineBody(reason=reason), session, admin_user)
    return catalog_response_toast(
        "Wishlist.ManualWishlist",
        "Declined",
        "info",
        user=admin_user,
        results=get_all_manual_requests(session, admin_user),
        counts=get_wishlist_counts(session, admin_user),
        update_tablist=True,
    )


@router.delete("/hx-delete/{id}")
async def delete_manual(
    id: uuid.UUID,
    session: Annotated[Session, Depends(get_session)],
    admin_user: Annotated[DetailedUser, Security(EarrAuth(GroupEnum.admin))],
):
    await delete_manual_request(id, session, admin_user)

    results = get_all_manual_requests(session, admin_user)
    counts = get_wishlist_counts(session, admin_user)

    return catalog_response(
        "Wishlist.ManualWishlist",
        user=admin_user,
        results=results,
        counts=counts,
        update_tablist=True,
    )
