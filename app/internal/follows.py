"""Following authors and series, and requesting their new books.

When someone follows, every book already out is remembered as seen, so only
what comes after is requested. Books that have been announced but are not out
yet count as new: following is how you would hear about them.

A new book is skipped when it is in a language nobody wants, when it is an old
book that only now appeared in Audible's listing, or when the same title is
already requested or in the library under another edition.
"""

import uuid
from datetime import datetime, timedelta

import aiohttp
from aiohttp import ClientSession
from pydantic import BaseModel
from rapidfuzz import utils
from sqlmodel import Session, col, func, select

from app.internal.audible.catalog import (
    get_author_products,
    get_series_products,
    is_main_edition,
    release_date,
)
from app.internal.audible.types import AudibleContributor, AudibleProduct
from app.internal.language import is_preferred
from app.internal.models import (
    Audiobook,
    AudiobookRequest,
    Follow,
    FollowKindEnum,
    User,
)
from app.internal.requesting import request_book
from app.util.db import get_session
from app.util.log import logger

CHECK_INTERVAL = 12 * 60 * 60
"""Audible adds books a few times a week at most."""
BACKLIST_GRACE = timedelta(days=30)
"""A book released this long before the follow started is not news."""


class FollowSummary(BaseModel):
    follow: Follow
    requested: int
    """Books this follow has requested so far."""
    next_release: Audiobook | None


async def fetch_products(
    client_session: ClientSession, follow: Follow
) -> list[AudibleProduct]:
    if follow.kind == FollowKindEnum.series:
        return await get_series_products(client_session, follow.audible_id)
    author = AudibleContributor(
        name=follow.name,
        asin=None if follow.audible_id == follow.name else follow.audible_id,
    )
    return await get_author_products(client_session, author)


def find_follow(
    session: Session, username: str, kind: FollowKindEnum, audible_id: str
) -> Follow | None:
    return session.exec(
        select(Follow).where(
            Follow.user_username == username,
            Follow.kind == kind,
            Follow.audible_id == audible_id,
        )
    ).first()


def list_follows(session: Session, user: User) -> list[Follow]:
    """Admins see everyone's."""
    query = select(Follow).order_by(col(Follow.name))
    if not user.is_admin():
        query = query.where(Follow.user_username == user.username)
    return list(session.exec(query).all())


def summaries(session: Session, follows: list[Follow]) -> list[FollowSummary]:
    now = datetime.now()
    result: list[FollowSummary] = []
    for follow in follows:
        requested = session.exec(
            select(func.count()).where(
                AudiobookRequest.user_username == follow.user_username,
                AudiobookRequest.via_follow == follow.name,
            )
        ).one()
        upcoming = session.exec(
            select(Audiobook)
            .join(AudiobookRequest)
            .where(
                AudiobookRequest.user_username == follow.user_username,
                AudiobookRequest.via_follow == follow.name,
                col(Audiobook.release_date) > now,
            )
            .order_by(col(Audiobook.release_date))
        ).first()
        result.append(
            FollowSummary(follow=follow, requested=requested, next_release=upcoming)
        )
    return result


def _already_have(session: Session, user: User, book: Audiobook) -> bool:
    """The same book under another edition is already on its way or in the library."""
    title = utils.default_process(book.title)
    authors = {utils.default_process(a) for a in book.authors}
    candidates = session.exec(
        select(Audiobook).where(func.lower(Audiobook.title) == book.title.lower())
    ).all()
    for other in candidates:
        if utils.default_process(other.title) != title:
            continue
        if authors and not authors & {utils.default_process(a) for a in other.authors}:
            continue
        if other.downloaded or any(
            r.user_username == user.username for r in other.requests
        ):
            return True
    return False


async def check_follow(
    session: Session, client_session: ClientSession, follow: Follow
) -> list[Audiobook]:
    """Requests the books that appeared since the last check. Returns them."""
    user = session.get(User, follow.user_username)
    if user is None:
        return []
    products = await fetch_products(client_session, follow)
    follow.last_checked_at = datetime.now()
    if not products:
        follow.last_error = "Audible returned no books"
        session.add(follow)
        session.commit()
        return []

    known = set(follow.known_asins)
    requested: list[Audiobook] = []
    for product in products:
        if product.asin in known:
            continue
        known.add(product.asin)
        if not is_preferred(session, product.language) or not is_main_edition(product):
            continue
        if release_date(product) < follow.created_at - BACKLIST_GRACE:
            continue
        book = product.to_audiobook()
        if _already_have(session, user, book):
            continue
        if await request_book(
            session, client_session, user, book, via_follow=follow.name
        ):
            requested.append(book)

    follow.known_asins = sorted(known)
    follow.last_error = None
    session.add(follow)
    session.commit()
    if requested:
        logger.info(
            "Requested new books from a follow",
            follow=follow.name,
            count=len(requested),
        )
    return requested


async def start_following(
    session: Session,
    client_session: ClientSession,
    user: User,
    kind: FollowKindEnum,
    audible_id: str,
    name: str,
) -> Follow:
    existing = find_follow(session, user.username, kind, audible_id)
    if existing:
        return existing

    follow = Follow(
        user_username=user.username, kind=kind, audible_id=audible_id, name=name
    )
    products = await fetch_products(client_session, follow)
    now = datetime.now()
    # what is out already is the backlist; what is announced is what to wait for
    follow.known_asins = sorted(p.asin for p in products if release_date(p) <= now)
    session.add(follow)
    session.commit()
    _ = await check_follow(session, client_session, follow)
    return follow


def stop_following(session: Session, user: User, follow_id: uuid.UUID) -> bool:
    follow = session.get(Follow, follow_id)
    if follow is None:
        return False
    if follow.user_username != user.username and not user.is_admin():
        return False
    session.delete(follow)
    session.commit()
    return True


async def check_all_follows() -> None:
    """Scheduled entry point. Never raises so the scheduler keeps running."""
    try:
        with next(get_session()) as session:
            async with ClientSession(
                timeout=aiohttp.ClientTimeout(total=60)
            ) as client_session:
                total = 0
                for follow in session.exec(select(Follow)).all():
                    try:
                        total += len(
                            await check_follow(session, client_session, follow)
                        )
                    except Exception as e:
                        logger.warning(
                            "Checking a follow failed", follow=follow.name, error=str(e)
                        )
        if total:
            logger.info("Follows requested new books", count=total)
    except Exception as e:
        logger.error("Checking follows failed", error=str(e))
