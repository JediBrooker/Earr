"""Problems reported with books in the library."""

import uuid
from typing import Annotated

from aiohttp import ClientSession
from fastapi import APIRouter, BackgroundTasks, Depends, Form, Security
from sqlmodel import Session

from app.internal.auth.authentication import DetailedUser, EarrAuth
from app.internal.issues import (
    IssueError,
    list_issues,
    notify_reported,
    replace_download,
    report,
    resolve,
)
from app.internal.models import GroupEnum, IssueKindEnum, IssueStatusEnum
from app.util.connection import get_connection
from app.util.db import get_session
from app.util.templates import catalog_response
from app.util.toast import toast_response

router = APIRouter(prefix="/issues")


@router.get("")
def issues_page(
    session: Annotated[Session, Depends(get_session)],
    user: Annotated[DetailedUser, Security(EarrAuth())],
    status: IssueStatusEnum = IssueStatusEnum.open,
):
    return catalog_response(
        "Issues.Index",
        user=user,
        issues=list_issues(session, user, status),
        status=status.value,
    )


@router.post("/hx-report/{asin}")
async def report_problem(
    asin: str,
    session: Annotated[Session, Depends(get_session)],
    background_task: BackgroundTasks,
    user: Annotated[DetailedUser, Security(EarrAuth())],
    kind: Annotated[IssueKindEnum, Form()],
    message: Annotated[str, Form()] = "",
):
    try:
        issue = report(session, user, asin, kind, message[:2000])
    except IssueError as e:
        return toast_response(str(e), "error")
    background_task.add_task(notify_reported, issue)
    return toast_response("Thanks. An admin will take a look.")


@router.post("/{issue_id}/hx-resolve")
async def resolve_issue(
    issue_id: uuid.UUID,
    session: Annotated[Session, Depends(get_session)],
    admin_user: Annotated[DetailedUser, Security(EarrAuth(GroupEnum.admin))],
    resolution: Annotated[str, Form()] = "",
):
    try:
        _ = await resolve(session, issue_id, admin_user, resolution[:2000])
    except IssueError as e:
        return toast_response(str(e), "error")
    return toast_response("Marked as resolved")


@router.post("/{issue_id}/hx-replace")
async def replace(
    issue_id: uuid.UUID,
    session: Annotated[Session, Depends(get_session)],
    client_session: Annotated[ClientSession, Depends(get_connection)],
    admin_user: Annotated[DetailedUser, Security(EarrAuth(GroupEnum.admin))],
):
    try:
        message = await replace_download(session, client_session, issue_id, admin_user)
    except IssueError as e:
        return toast_response(str(e), "error")
    return toast_response(message)
