"""Problems people report with a book that reached the library.

The wrong book, bad audio, missing parts. A report reaches the admins through
notifications and the Issues page, where it can be resolved with a note to the
reporter, or fixed by blocklisting the release and grabbing another, whose
files then replace the bad ones.
"""

import uuid
from datetime import datetime

from aiohttp import ClientSession
from sqlmodel import Session, col, desc, func, select

from app.internal import blocklist
from app.internal.models import (
    Audiobook,
    EventEnum,
    Issue,
    IssueKindEnum,
    IssueStatusEnum,
    LibraryImport,
    LibraryImportStatusEnum,
    User,
)
from app.internal.notifications import send_all_notifications
from app.util.log import logger


class IssueError(Exception):
    pass


def report(
    session: Session, user: User, asin: str, kind: IssueKindEnum, message: str | None
) -> Issue:
    book = session.get(Audiobook, asin)
    if book is None:
        raise IssueError("That book isn't known to Earr")
    existing = session.exec(
        select(Issue).where(
            Issue.asin == asin,
            Issue.user_username == user.username,
            Issue.status == IssueStatusEnum.open,
        )
    ).first()
    if existing:
        raise IssueError("You've already reported a problem with this book")
    issue = Issue(
        asin=asin,
        book_title=book.title,
        user_username=user.username,
        kind=kind,
        message=(message or "").strip() or None,
    )
    session.add(issue)
    session.commit()
    logger.info("Problem reported", asin=asin, kind=kind.value)
    return issue


def list_issues(session: Session, user: User, status: IssueStatusEnum) -> list[Issue]:
    """Admins see everyone's; everyone else their own."""
    query = select(Issue).where(Issue.status == status)
    if not user.is_admin():
        query = query.where(Issue.user_username == user.username)
    order = (
        col(Issue.created_at)
        if status == IssueStatusEnum.open
        else col(Issue.resolved_at)
    )
    return list(session.exec(query.order_by(desc(order))).all())


def open_count(session: Session) -> int:
    return session.exec(
        select(func.count()).where(Issue.status == IssueStatusEnum.open)
    ).one()


def _replacements(issue: Issue) -> dict[str, str]:
    replacements = {
        "issueKind": issue.kind.label,
        "issueMessage": issue.message or "",
        "issueReporter": issue.user_username,
        "issueResolution": issue.resolution or "",
    }
    return replacements


async def notify_reported(issue: Issue) -> None:
    await send_all_notifications(
        EventEnum.on_issue_reported, issue.asin, _replacements(issue)
    )


async def resolve(
    session: Session, issue_id: uuid.UUID, admin: User, resolution: str | None
) -> Issue:
    issue = session.get(Issue, issue_id)
    if issue is None:
        raise IssueError("No such report")
    issue.status = IssueStatusEnum.resolved
    issue.resolution = (resolution or "").strip() or "Resolved"
    issue.resolved_by = admin.username
    issue.resolved_at = datetime.now()
    session.add(issue)
    session.commit()
    try:
        await send_all_notifications(
            EventEnum.on_issue_resolved, issue.asin, _replacements(issue)
        )
    except Exception as e:
        logger.error("Could not send the resolved notification", error=str(e))
    return issue


async def replace_download(
    session: Session, client_session: ClientSession, issue_id: uuid.UUID, admin: User
) -> str:
    """Blocklists the release that was imported and looks for another.

    The bad files stay in the library until the new release is imported, which
    then removes them. Returns a message for the admin.
    """
    from app.internal.query import query_sources

    issue = session.get(Issue, issue_id)
    if issue is None:
        raise IssueError("No such report")
    book = session.get(Audiobook, issue.asin)
    if book is None:
        raise IssueError("The book is no longer known to Earr")

    imported = session.exec(
        select(LibraryImport)
        .where(
            LibraryImport.asin_or_uuid == issue.asin,
            LibraryImport.status == LibraryImportStatusEnum.imported,
        )
        .order_by(desc(col(LibraryImport.updated_at)))
    ).first()
    if imported is not None:
        _ = blocklist.add_import(
            session, imported, f"Reported: {issue.kind.label.lower()}"
        )

    book.downloaded = False
    book.download_status = None
    book.search_attempts = 0
    book.last_searched_at = None
    session.add(book)
    session.commit()

    # an admin asked for this, so it searches whoever requested the book and
    # whether or not automatic downloads are on
    try:
        _ = await query_sources(
            asin_or_uuid=book.asin,
            session=session,
            client_session=client_session,
            start_auto_download=True,
            force_refresh=True,
        )
    except Exception as e:
        logger.warning(
            "Searching for a replacement failed", asin=book.asin, error=str(e)
        )
    session.refresh(book)
    started = book.downloaded
    if started:
        outcome = "Replaced with another release"
        message = "Blocklisted the release and grabbed another one."
    elif imported is None:
        outcome = "Searching for another release"
        message = "Earr didn't import this book itself, so there was nothing to blocklist. It's back on the wishlist to search for again."
    else:
        outcome = "Blocklisted the release"
        message = "Blocklisted the release. Nothing else cleared the bar yet, so it's back on the wishlist and will be searched for again."
    _ = await resolve(session, issue.id, admin, outcome)
    return message
