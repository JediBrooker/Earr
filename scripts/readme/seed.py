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

from app.internal.audible.search import search_audible_books
from app.internal.auth.authentication import create_user
from app.internal.auth.config import auth_config
from app.internal.auth.login_types import LoginTypeEnum
from app.internal.library.config import library_config
from app.internal.library.naming import build_tokens, render_template
from app.internal.models import (
    Audiobook,
    AudiobookRequest,
    DownloadStatusEnum,
    GroupEnum,
    LibraryImport,
    LibraryImportStatusEnum,
    ManualBookRequest,
    OrganizeModeEnum,
)
from app.internal.download_clients.config import download_client_config
from app.internal.prowlarr.util import prowlarr_config
from app.internal.ranking.quality import IndexerFlag, quality_config
from app.util.db import engine

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


async def fetch_books() -> dict[str, Audiobook]:
    books: dict[str, Audiobook] = {}
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(30)) as client:
        for wanted in BOOKS:
            query = " ".join(
                filter(None, [wanted.title, wanted.author, wanted.narrator])
            )
            results = await search_audible_books(client, query, audible_region="us")
            books[wanted.key] = pick(results, wanted)
    return books


def target_folder(book: Audiobook) -> str:
    return "/".join([LIBRARY_ROOT, *render_template(TEMPLATE, build_tokens(book))])


def release_name(book: Audiobook) -> str:
    return f"{book.authors[0]} - {book.title} [M4B 64kbps]"


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
    download_client_config.set_qbit_enabled(session, True)
    download_client_config.set_qbit_url(session, os.environ["EARR_DEMO_PROWLARR"])

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
                    asin=book.asin, user_username=username, updated_at=when
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
            created_at=when,
            updated_at=when,
        )
        if wanted.status == DownloadStatusEnum.downloaded:
            entry.status = LibraryImportStatusEnum.imported
            entry.source_path = f"{TORRENTS}/{release_name(book)}"
            entry.target_path = target_folder(book)
        elif wanted.status == DownloadStatusEnum.failed:
            entry.status = LibraryImportStatusEnum.expired
            entry.error = "The download never showed up in the completed folders"
        session.add(entry)

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


def main() -> None:
    books = asyncio.run(fetch_books())
    with Session(engine) as session:
        seed(session, books, os.environ["EARR_DEMO_PASSWORD"])
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
        }
    _ = Path(os.environ["EARR_DEMO_OUT"]).write_text(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
