"""How many books each person may request in a given period.

Admins are never limited. Everyone else gets the limit set for their group,
unless an admin gave them their own. Books requested automatically because
someone follows an author or series do not count: nobody chose them, and an
untrusted user's still wait for approval.
"""

from datetime import datetime, timedelta
from enum import Enum
from typing import Literal

from fastapi import HTTPException
from pydantic import BaseModel
from sqlmodel import Session, col, func, select

from app.internal.models import AudiobookRequest, GroupEnum, ManualBookRequest, User
from app.util.cache import StringConfigCache


class LimitPeriod(str, Enum):
    day = "day"
    week = "week"
    month = "month"


_PERIOD_DAYS = {LimitPeriod.day: 1, LimitPeriod.week: 7, LimitPeriod.month: 30}

LimitConfigKey = Literal[
    "request_limit_untrusted",
    "request_limit_trusted",
    "request_limit_period",
]


class RequestLimitConfig(StringConfigCache[LimitConfigKey]):
    def get_group_limit(self, session: Session, group: GroupEnum) -> int:
        """0 means unlimited, which is the default."""
        if group == GroupEnum.admin:
            return 0
        key: LimitConfigKey = (
            "request_limit_trusted"
            if group == GroupEnum.trusted
            else "request_limit_untrusted"
        )
        return max(0, self.get_int(session, key, 0))

    def set_group_limit(self, session: Session, group: GroupEnum, limit: int):
        if group == GroupEnum.admin:
            return
        key: LimitConfigKey = (
            "request_limit_trusted"
            if group == GroupEnum.trusted
            else "request_limit_untrusted"
        )
        self.set_int(session, key, max(0, limit))

    def get_period(self, session: Session) -> LimitPeriod:
        try:
            return LimitPeriod(self.get(session, "request_limit_period", "week"))
        except ValueError:
            return LimitPeriod.week

    def set_period(self, session: Session, period: LimitPeriod):
        self.set(session, "request_limit_period", period.value)


request_limit_config = RequestLimitConfig()


class Allowance(BaseModel):
    limit: int
    used: int
    period: LimitPeriod
    next_free_at: datetime | None
    """When the oldest counted request stops counting, if the limit is reached."""

    @property
    def remaining(self) -> int:
        return max(0, self.limit - self.used)

    @property
    def reached(self) -> bool:
        return self.used >= self.limit

    @property
    def period_label(self) -> str:
        return {"day": "today", "week": "this week", "month": "this month"}[
            self.period.value
        ]

    def message(self) -> str:
        when = (
            f" You can request again {self.next_free_at:%-d %b at %H:%M}."
            if self.next_free_at
            else ""
        )
        return (
            f"You have used all {self.limit} of your requests {self.period_label}."
            + when
        )


def limit_for(session: Session, user: User) -> int:
    if user.is_admin():
        return 0
    if user.request_limit is not None:
        return max(0, user.request_limit)
    return request_limit_config.get_group_limit(session, user.group)


def allowance(session: Session, user: User) -> Allowance | None:
    """None when the user may request as much as they like."""
    limit = limit_for(session, user)
    if limit <= 0:
        return None
    period = request_limit_config.get_period(session)
    days = _PERIOD_DAYS[period]
    since = datetime.now() - timedelta(days=days)

    times = list(
        session.exec(
            select(AudiobookRequest.created_at).where(
                AudiobookRequest.user_username == user.username,
                col(AudiobookRequest.created_at) >= since,
                col(AudiobookRequest.via_follow).is_(None),
            )
        ).all()
    ) + list(
        session.exec(
            select(ManualBookRequest.created_at).where(
                ManualBookRequest.user_username == user.username,
                col(ManualBookRequest.created_at) >= since,
            )
        ).all()
    )
    times.sort()
    next_free_at = None
    if len(times) >= limit:
        # the request that has to age out before one more fits
        next_free_at = times[len(times) - limit] + timedelta(days=days)
    return Allowance(
        limit=limit, used=len(times), period=period, next_free_at=next_free_at
    )


def raise_if_over(session: Session, user: User) -> None:
    found = allowance(session, user)
    if found is not None and found.reached:
        raise HTTPException(status_code=429, detail=found.message())


def count_requests(session: Session, username: str) -> int:
    """Every request a user has made, for the users list."""
    return (
        session.exec(
            select(func.count()).where(AudiobookRequest.user_username == username)
        ).one()
        + session.exec(
            select(func.count()).where(ManualBookRequest.user_username == username)
        ).one()
    )
