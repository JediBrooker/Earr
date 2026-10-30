"""Importing a want-to-read list from Goodreads or The StoryGraph.

Both export a CSV of everything on your shelves. Only the books marked
"to-read" are taken, each matched to an Audible audiobook by title and
author, and shown for a final check before anything is requested.
"""

import asyncio
import csv
import io
import re
from typing import Literal

from aiohttp import ClientSession
from pydantic import BaseModel
from rapidfuzz import fuzz, utils
from sqlmodel import Session, select

from app.internal.audible.search import search_audible_books
from app.internal.audiobookshelf.catalog import library_asins
from app.internal.book_page import with_requests
from app.internal.language import is_preferred
from app.internal.models import Audiobook, AudiobookRequest, AudiobookWithRequests, User

MAX_ENTRIES = 150
"""Each one is an Audible search, so a huge shelf is cut off here."""
TITLE_MATCH = 85
AUTHOR_MATCH = 80
CONCURRENCY = 5


class ReadingListError(Exception):
    pass


class ListEntry(BaseModel):
    title: str
    author: str


class ListMatch(BaseModel):
    entry: ListEntry
    book: AudiobookWithRequests | None
    status: Literal["new", "requested", "owned", "missing"]


class ParsedList(BaseModel):
    source: str
    entries: list[ListEntry]
    skipped: int
    """To-read books beyond MAX_ENTRIES."""


# Goodreads puts the series in the title: "Words of Radiance (The Stormlight Archive, #2)"
_SERIES_SUFFIX = re.compile(r"\s*\([^()]*#\s*[\d.]+\)\s*$")


def clean_title(title: str) -> str:
    return _SERIES_SUFFIX.sub("", title).strip()


def parse_export(text: str) -> ParsedList:
    reader = csv.DictReader(io.StringIO(text.lstrip("﻿")))
    fields = set(reader.fieldnames or [])
    if {"Title", "Author", "Exclusive Shelf"} <= fields:
        source, author_field, shelf_field = "Goodreads", "Author", "Exclusive Shelf"
    elif {"Title", "Authors", "Read Status"} <= fields:
        source, author_field, shelf_field = "The StoryGraph", "Authors", "Read Status"
    else:
        raise ReadingListError(
            "That doesn't look like a Goodreads or StoryGraph export. Use the CSV"
            + " from Goodreads' Import and export page, or StoryGraph's Manage"
            + " account page."
        )

    entries: list[ListEntry] = []
    seen: set[tuple[str, str]] = set()
    for row in reader:
        if (row.get(shelf_field) or "").strip().lower() != "to-read":
            continue
        title = clean_title(row.get("Title") or "")
        # StoryGraph lists every author, comma separated; the first will do
        author = (row.get(author_field) or "").split(",")[0].strip()
        key = (utils.default_process(title), utils.default_process(author))
        if not title or key in seen:
            continue
        seen.add(key)
        entries.append(ListEntry(title=title, author=author))
    return ParsedList(
        source=source,
        entries=entries[:MAX_ENTRIES],
        skipped=max(0, len(entries) - MAX_ENTRIES),
    )


def same_author(a: str, b: str) -> bool:
    """Close spellings, or the same surname and first initial ("J.R.R. Tolkien"
    and "J. R. R. Tolkien"), but not Frank and Brian Herbert."""
    a_parts = utils.default_process(a).split()
    b_parts = utils.default_process(b).split()
    if not a_parts or not b_parts:
        return False
    if fuzz.token_sort_ratio(" ".join(a_parts), " ".join(b_parts)) >= AUTHOR_MATCH + 5:
        return True
    return a_parts[-1] == b_parts[-1] and a_parts[0][0] == b_parts[0][0]


def best_match(
    session: Session, entry: ListEntry, results: list[Audiobook]
) -> Audiobook | None:
    """The result that really is this book: the title and an author have to match."""

    def score(book: Audiobook) -> tuple[bool, bool, float]:
        return (
            is_preferred(session, book.language),
            "dramatized" not in book.title.lower(),
            # the closest title, so "Dune" beats "Dune Messiah"
            fuzz.ratio(entry.title, book.title, processor=utils.default_process),
        )

    candidates: list[Audiobook] = []
    for book in results:
        if (
            fuzz.token_set_ratio(
                entry.title, book.title, processor=utils.default_process
            )
            < TITLE_MATCH
        ):
            continue
        if entry.author and not any(same_author(entry.author, a) for a in book.authors):
            continue
        candidates.append(book)
    if not candidates:
        return None
    return max(candidates, key=score)


async def match_entries(
    session: Session,
    client_session: ClientSession,
    user: User,
    entries: list[ListEntry],
) -> list[ListMatch]:
    limit = asyncio.Semaphore(CONCURRENCY)

    async def search(entry: ListEntry) -> list[Audiobook]:
        async with limit:
            return await search_audible_books(
                client_session, f"{entry.title} {entry.author}".strip(), num_results=10
            )

    found = await asyncio.gather(*(search(e) for e in entries))
    matched = [best_match(session, e, r) for e, r in zip(entries, found, strict=True)]

    owned = await library_asins(session, client_session)
    mine = set(
        session.exec(
            select(AudiobookRequest.asin).where(
                AudiobookRequest.user_username == user.username
            )
        ).all()
    )
    books = with_requests(session, [b for b in matched if b], user.username)
    by_asin = {b.book.asin: b for b in books}

    results: list[ListMatch] = []
    for entry, book in zip(entries, matched, strict=True):
        if book is None:
            results.append(ListMatch(entry=entry, book=None, status="missing"))
            continue
        stored = by_asin[book.asin]
        if stored.book.downloaded or book.asin in owned:
            status = "owned"
        elif book.asin in mine:
            status = "requested"
        else:
            status = "new"
        results.append(ListMatch(entry=entry, book=stored, status=status))
    return results
