"""Fills a fresh database with a believable household for the README screenshots.

Run by capture.py in a child process, with EARR_APP__CONFIG_DIR pointing at a
throwaway folder, because the app builds its database engine at import time.

Reads:
    EARR_DEMO_PASSWORD  password of the admin "alex", who takes the screenshots
    EARR_DEMO_PROWLARR  URL of the stub, which stands in for Prowlarr and qBittorrent
    EARR_DEMO_OUT       file to write a JSON summary to, for the stub and capture

Books come from the real Audible catalogue, so covers and metadata are real.
"""

import asyncio
import hashlib
import json
import os
import re
import secrets
from datetime import datetime, timedelta
from pathlib import Path

import aiohttp
from sqlmodel import Session

from app.internal import blocklist
from app.internal.audible.catalog import (
    get_author_products,
    get_book_details,
    is_main_edition,
)
from app.internal.audible.search import search_audible_books
from app.internal.audible.types import AudibleContributor
from app.internal.auth.authentication import create_user
from app.internal.auth.config import auth_config
from app.internal.auth.login_types import LoginTypeEnum
from app.internal.library.config import library_config
from app.internal.library.naming import build_tokens, render_template
from app.internal.models import (
    Audiobook,
    AudiobookRequest,
    DownloadStatusEnum,
    Follow,
    FollowKindEnum,
    GroupEnum,
    Issue,
    IssueKindEnum,
    LibraryImport,
    LibraryImportStatusEnum,
    ManualBookRequest,
    OrganizeModeEnum,
)
from app.internal.download_clients.config import download_client_config
from app.internal.prowlarr.util import prowlarr_config
from app.internal.ranking.quality import IndexerFlag, quality_config
from app.util.db import engine

INDEXERS = ["MyAnonamouse", "AudioBookBay", "NZBgeek"]
"""Named on grabs, for the Stats page."""

LIBRARY_ROOT = "/data/media/audiobooks"
TORRENTS = "/data/torrents/audiobooks"
USENET = "/data/usenet/audiobooks"
TEMPLATE = "{author}/{series}/[{series_position} - ]{title}"

USERS = {
    "alex": GroupEnum.admin,
    "sam": GroupEnum.trusted,
    "jordan": GroupEnum.untrusted,
    "priya": GroupEnum.untrusted,
}


class Wanted:
    def __init__(
        self,
        key: str,
        title: str,
        author: str,
        requested_by: list[str],
        status: DownloadStatusEnum | None = None,
        narrator: str | None = None,
        days_ago: int = 1,
    ):
        self.key: str = key
        self.title: str = title
        self.author: str = author
        self.narrator: str | None = narrator
        self.requested_by: list[str] = requested_by
        self.status: DownloadStatusEnum | None = status
        self.days_ago: int = days_ago


BOOKS = [
    # the book the Sources page screenshot is taken of
    Wanted("dcc", "Dungeon Crawler Carl", "Matt Dinniman", ["priya", "sam", "alex"]),
    Wanted(
        "phm",
        "Project Hail Mary",
        "Andy Weir",
        ["sam"],
        DownloadStatusEnum.grabbed,
        days_ago=2,
    ),
    Wanted(
        "leviathan",
        "Leviathan Wakes",
        "James S. A. Corey",
        ["jordan"],
        DownloadStatusEnum.failed,
        days_ago=20,
    ),
    Wanted("red-rising", "Red Rising", "Pierce Brown", ["jordan", "priya"], days_ago=3),
    Wanted(
        "hobbit",
        "The Hobbit",
        "J.R.R. Tolkien",
        ["priya"],
        DownloadStatusEnum.grabbed,
        narrator="Andy Serkis",
        days_ago=1,
    ),
    Wanted(
        "wok",
        "The Way of Kings",
        "Brandon Sanderson",
        ["alex", "sam"],
        DownloadStatusEnum.downloaded,
        days_ago=9,
    ),
    Wanted(
        "notw",
        "The Name of the Wind",
        "Patrick Rothfuss",
        ["jordan"],
        DownloadStatusEnum.downloaded,
        days_ago=14,
    ),
]


def _norm(text: str) -> str:
    return re.sub(r"[^a-z0-9]", "", text.lower())


def pick(results: list[Audiobook], wanted: Wanted) -> Audiobook:
    """The search hit that is really the wanted book, not a summary or a box set."""

    def score(book: Audiobook) -> tuple[bool, bool, bool]:
        return (
            _norm(book.title) == _norm(wanted.title),
            any(_norm(a) == _norm(wanted.author) for a in book.authors),
            wanted.narrator is None
            or any(_norm(n) == _norm(wanted.narrator) for n in book.narrators),
        )

    if not results:
        raise RuntimeError(f"Audible returned nothing for {wanted.title!r}")
    return max(results, key=score)  # max keeps the first of equals, i.e. relevance


FOLLOWED_AUTHORS = {
    "alex": ["Brandon Sanderson", "Will Wight", "Adrian Tchaikovsky"],
    "sam": ["Andy Weir", "John Scalzi", "Joe Abercrombie"],
    "priya": ["Travis Baldree", "Rebecca Yarros", "Matt Dinniman"],
}
"""Who follows whom. Their announced books fill the Calendar."""


class Followed:
    def __init__(
        self, username: str, author: AudibleContributor, upcoming: list[Audiobook]
    ):
        self.username: str = username
        self.author: AudibleContributor = author
        self.upcoming: list[Audiobook] = upcoming


async def fetch_books() -> tuple[dict[str, Audiobook], list[Followed], str | None]:
    """The wanted books, each followed author's announced books, and the
    Stormlight Archive's series ASIN for a series follow."""
    books: dict[str, Audiobook] = {}
    followed: list[Followed] = []
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(30)) as client:
        for wanted in BOOKS:
            query = " ".join(
                filter(None, [wanted.title, wanted.author, wanted.narrator])
            )
            results = await search_audible_books(client, query, audible_region="us")
            books[wanted.key] = pick(results, wanted)

        now = datetime.now()
        for username, authors in FOLLOWED_AUTHORS.items():
            for name in authors:
                # the author's ASIN, from one of their books
                hits = await search_audible_books(client, name, audible_region="us")
                details = (
                    await get_book_details(client, hits[0].asin, "us") if hits else None
                )
                author = next(
                    (
                        a
                        for a in (details.authors if details else [])
                        if _norm(a.name) == _norm(name)
                    ),
                    AudibleContributor(name=name),
                )
                products = await get_author_products(client, author, "us")
                upcoming = [
                    p.to_audiobook()
                    for p in products
                    if p.language == "english"
                    and is_main_edition(p)
                    and datetime.fromisoformat(p.release_date) > now
                ][:2]
                followed.append(Followed(username, author, upcoming))

        details = await get_book_details(client, books["wok"].asin, "us")
        series = details.primary_series if details else None
    return books, followed, series.asin if series else None


def target_folder(book: Audiobook) -> str:
    return "/".join([LIBRARY_ROOT, *render_template(TEMPLATE, build_tokens(book))])


def release_name(book: Audiobook) -> str:
    return f"{book.authors[0]} - {book.title} [M4B 64kbps]"


def seed_follows(
    session: Session, followed: list[Followed], series_asin: str | None
) -> None:
    now = datetime.now()
    for entry in followed:
        session.add(
            Follow(
                user_username=entry.username,
                kind=FollowKindEnum.author,
                audible_id=entry.author.asin or entry.author.name,
                name=entry.author.name,
                known_asins=[b.asin for b in entry.upcoming],
                last_checked_at=now - timedelta(hours=3),
                created_at=now - timedelta(days=30),
            )
        )
        for book in entry.upcoming:
            if session.get(Audiobook, book.asin) is None:
                session.add(book)
            session.add(
                AudiobookRequest(
                    asin=book.asin,
                    user_username=entry.username,
                    via_follow=entry.author.name,
                    created_at=now - timedelta(days=2),
                )
            )
    if series_asin:
        session.add(
            Follow(
                user_username="alex",
                kind=FollowKindEnum.series,
                audible_id=series_asin,
                name="The Stormlight Archive",
                last_checked_at=now - timedelta(hours=3),
            )
        )


def seed(session: Session, books: dict[str, Audiobook], password: str) -> None:
    for username, group in USERS.items():
        session.add(
            create_user(
                username,
                password if username == "alex" else secrets.token_urlsafe(16),
                group,
                root=username == "alex",
            )
        )
    auth_config.set_login_type(session, LoginTypeEnum.forms)

    prowlarr_config.set_base_url(session, os.environ["EARR_DEMO_PROWLARR"])
    prowlarr_config.set_api_key(session, "demo")
    quality_config.set_auto_download(session, True)
    quality_config.set_indexer_flags(session, [IndexerFlag(flag="freeleech", score=5)])

    # the Prowlarr stand-in answers for qBittorrent too, so Activity shows progress
    download_client_config.set_enabled(session, "qbit", True)
    download_client_config.set_field(
        session, "qbit", "url", os.environ["EARR_DEMO_PROWLARR"]
    )

    library_config.set_enabled(session, True)
    library_config.set_mode(session, OrganizeModeEnum.hardlink)
    library_config.set_download_dirs(session, f"{TORRENTS}\n{USENET}")
    library_config.set_root_dir(session, LIBRARY_ROOT)
    library_config.set_folder_template(session, TEMPLATE)

    now = datetime.now()
    for wanted in BOOKS:
        book = books[wanted.key]
        when = now - timedelta(days=wanted.days_ago)
        book.download_status = wanted.status
        # as the app does it: a grab counts as downloaded until the files fail to arrive
        book.downloaded = wanted.status in (
            DownloadStatusEnum.grabbed,
            DownloadStatusEnum.downloaded,
        )
        session.add(book)
        for username in wanted.requested_by:
            session.add(
                AudiobookRequest(
                    asin=book.asin,
                    user_username=username,
                    updated_at=when,
                    created_at=when,
                )
            )
        if wanted.status is None:
            continue

        entry = LibraryImport(
            asin_or_uuid=book.asin,
            book_title=book.title,
            release_title=release_name(book),
            # the stand-in qBittorrent reports progress for any hash it's asked about
            client_id=hashlib.sha1(book.asin.encode()).hexdigest(),
            protocol="torrent",
            indexer=INDEXERS[len(wanted.key) % len(INDEXERS)],
            created_at=when,
            updated_at=when,
        )
        if wanted.status == DownloadStatusEnum.downloaded:
            entry.status = LibraryImportStatusEnum.imported
            entry.source_path = f"{TORRENTS}/{release_name(book)}"
            entry.target_path = target_folder(book)
            # most land within a couple of hours
            entry.updated_at = when + timedelta(hours=2, minutes=len(wanted.key) * 7)
            book.quality_format = "m4b"
        elif wanted.status == DownloadStatusEnum.failed:
            entry.status = LibraryImportStatusEnum.failed
            entry.error = "qBittorrent: files missing"
        session.add(entry)
        if wanted.status == DownloadStatusEnum.failed:
            session.commit()
            _ = blocklist.add_import(session, entry, entry.error)

    session.add(
        ManualBookRequest(
            user_username="jordan",
            title="The Wind in the Willows",
            authors=["Kenneth Grahame"],
            narrators=["LibriVox volunteers"],
            publish_date="1908",
            additional_info="The free LibriVox recording, for the kids",
        )
    )
    session.commit()

    # someone noticed a problem with a book that came in
    session.add(
        Issue(
            asin=books["notw"].asin,
            book_title=books["notw"].title,
            user_username="jordan",
            kind=IssueKindEnum.bad_audio,
            message="Chapter 12 cuts off half way through and jumps to chapter 14.",
            created_at=now - timedelta(hours=5),
        )
    )
    session.commit()


def main() -> None:
    books, followed, series_asin = asyncio.run(fetch_books())
    with Session(engine) as session:
        seed(session, books, os.environ["EARR_DEMO_PASSWORD"])
        seed_follows(session, followed, series_asin)
        session.commit()
        summary = {
            "books": {
                key: {
                    "asin": book.asin,
                    "title": book.title,
                    "authors": book.authors,
                    "narrators": book.narrators,
                    "runtime_length_min": book.runtime_length_min,
                }
                for key, book in books.items()
            },
            "sources_asin": books["dcc"].asin,
            "book_asin": books["wok"].asin,
        }
    _ = Path(os.environ["EARR_DEMO_OUT"]).write_text(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
