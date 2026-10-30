"""Numbers for the admin: who asks for what, how the indexers do, how long books take."""

from collections import Counter
from datetime import datetime, timedelta
from statistics import median

from pydantic import BaseModel
from sqlmodel import Session, col, select

from app.internal.models import (
    Audiobook,
    AudiobookRequest,
    Issue,
    IssueStatusEnum,
    LibraryImport,
    LibraryImportStatusEnum,
    ManualBookRequest,
    User,
)

MONTHS = 12


class PersonStats(BaseModel):
    username: str
    group: str
    requests: int
    last_30_days: int
    in_library: int
    issues: int


class IndexerStats(BaseModel):
    indexer: str
    grabs: int
    imported: int
    failed: int

    @property
    def success_rate(self) -> int | None:
        settled = self.imported + self.failed
        return round(100 * self.imported / settled) if settled else None


class Bar(BaseModel):
    label: str
    count: int
    percent: int
    """Of the largest bar, for drawing."""


class Stats(BaseModel):
    total_requests: int
    requests_last_30_days: int
    books_in_library: int
    success_rate: int | None
    """Grabs that reached the library, of those that settled, in percent."""
    median_hours_to_library: float | None
    open_issues: int
    people: list[PersonStats]
    indexers: list[IndexerStats]
    months: list[Bar]
    authors: list[Bar]


def _bars(counts: list[tuple[str, int]]) -> list[Bar]:
    top = max((c for _, c in counts), default=0)
    return [
        Bar(label=label, count=count, percent=round(100 * count / top) if top else 0)
        for label, count in counts
    ]


def _month_labels(now: datetime) -> list[tuple[str, str]]:
    """(key, label) for the last MONTHS months, oldest first."""
    labels: list[tuple[str, str]] = []
    year, month = now.year, now.month
    for _ in range(MONTHS):
        labels.append((f"{year}-{month:02d}", datetime(year, month, 1).strftime("%b")))
        month -= 1
        if month == 0:
            year, month = year - 1, 12
    return list(reversed(labels))


def build(session: Session) -> Stats:
    now = datetime.now()
    month_ago = now - timedelta(days=30)
    requests = list(session.exec(select(AudiobookRequest)).all())
    manual = list(session.exec(select(ManualBookRequest)).all())
    books = {b.asin: b for b in session.exec(select(Audiobook)).all()}
    issues = list(session.exec(select(Issue)).all())
    imports = list(
        session.exec(
            select(LibraryImport).where(
                col(LibraryImport.status) != LibraryImportStatusEnum.pending
            )
        ).all()
    )

    people: list[PersonStats] = []
    for user in session.exec(select(User).order_by(col(User.username))).all():
        mine = [r for r in requests if r.user_username == user.username]
        my_manual = [m for m in manual if m.user_username == user.username]
        people.append(
            PersonStats(
                username=user.username,
                group=user.group.value,
                requests=len(mine) + len(my_manual),
                last_30_days=sum(
                    1 for r in [*mine, *my_manual] if r.created_at >= month_ago
                ),
                in_library=sum(
                    1
                    for r in mine
                    if (b := books.get(r.asin)) is not None and b.downloaded
                )
                + sum(1 for m in my_manual if m.downloaded),
                issues=sum(1 for i in issues if i.user_username == user.username),
            )
        )
    people.sort(key=lambda p: p.requests, reverse=True)

    by_indexer: dict[str, IndexerStats] = {}
    for entry in imports:
        name = entry.indexer or "Not recorded"
        stats = by_indexer.setdefault(
            name, IndexerStats(indexer=name, grabs=0, imported=0, failed=0)
        )
        stats.grabs += 1
        if entry.status == LibraryImportStatusEnum.imported:
            stats.imported += 1
        else:
            stats.failed += 1
    indexers = sorted(by_indexer.values(), key=lambda s: s.grabs, reverse=True)

    imported = [e for e in imports if e.status == LibraryImportStatusEnum.imported]
    settled = len(imports)
    durations = [
        (e.updated_at - e.created_at).total_seconds() / 3600
        for e in imported
        if e.updated_at >= e.created_at
    ]

    per_month = Counter(r.created_at.strftime("%Y-%m") for r in [*requests, *manual])
    months = _bars(
        [(label, per_month.get(key, 0)) for key, label in _month_labels(now)]
    )

    author_counts = Counter[str]()
    for r in requests:
        book = books.get(r.asin)
        if book is not None:
            author_counts.update(book.authors[:1])

    return Stats(
        total_requests=len(requests) + len(manual),
        requests_last_30_days=sum(
            1 for r in [*requests, *manual] if r.created_at >= month_ago
        ),
        books_in_library=sum(1 for b in books.values() if b.downloaded and b.requests)
        + sum(1 for m in manual if m.downloaded),
        success_rate=round(100 * len(imported) / settled) if settled else None,
        median_hours_to_library=round(median(durations), 1) if durations else None,
        open_issues=sum(1 for i in issues if i.status == IssueStatusEnum.open),
        people=people,
        indexers=indexers,
        months=months,
        authors=_bars(author_counts.most_common(10)),
    )
