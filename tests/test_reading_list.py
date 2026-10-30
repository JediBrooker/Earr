"""Importing a Goodreads or StoryGraph want-to-read list."""

from datetime import datetime
from unittest import mock

import pytest
from aiohttp import ClientSession
from sqlmodel import Session

from app.internal import reading_list
from app.internal.models import Audiobook, AudiobookRequest, GroupEnum, User
from app.internal.reading_list import (
    ListEntry,
    ReadingListError,
    best_match,
    clean_title,
    parse_export,
)

GOODREADS = """Book Id,Title,Author,Author l-f,Additional Authors,ISBN,ISBN13,My Rating,Exclusive Shelf
1,"Words of Radiance (The Stormlight Archive, #2)",Brandon Sanderson,"Sanderson, Brandon",,,,0,to-read
2,Project Hail Mary,Andy Weir,"Weir, Andy",,,,5,read
3,Dune,Frank Herbert,"Herbert, Frank",,,,0,to-read
4,Dune,Frank Herbert,"Herbert, Frank",,,,0,to-read
"""

STORYGRAPH = """Title,Authors,Contributors,ISBN/UID,Format,Read Status
The Martian,"Andy Weir, Someone Else",,123,audio,to-read
Klara and the Sun,Kazuo Ishiguro,,456,digital,read
"""


def book(
    asin: str, title: str, authors: list[str], language: str = "english"
) -> Audiobook:
    return Audiobook(
        asin=asin,
        title=title,
        subtitle=None,
        authors=authors,
        narrators=[],
        cover_image=None,
        release_date=datetime(2020, 1, 1),
        runtime_length_min=100,
        language=language,
    )


class TestParsing:
    def test_goodreads_takes_only_the_to_read_shelf(self):
        parsed = parse_export("﻿" + GOODREADS)
        assert parsed.source == "Goodreads"
        assert [(e.title, e.author) for e in parsed.entries] == [
            ("Words of Radiance", "Brandon Sanderson"),
            ("Dune", "Frank Herbert"),
        ]

    def test_storygraph_uses_the_first_author(self):
        parsed = parse_export(STORYGRAPH)
        assert parsed.source == "The StoryGraph"
        assert [(e.title, e.author) for e in parsed.entries] == [
            ("The Martian", "Andy Weir")
        ]

    def test_anything_else_is_refused(self):
        with pytest.raises(ReadingListError):
            _ = parse_export("name,price\nwidget,3\n")

    def test_long_lists_are_cut_off(self):
        rows = "".join(f"{i},Book {i},Author,,,,,0,to-read\n" for i in range(200))
        parsed = parse_export(GOODREADS.splitlines()[0] + "\n" + rows)
        assert len(parsed.entries) == reading_list.MAX_ENTRIES
        assert parsed.skipped == 200 - reading_list.MAX_ENTRIES

    def test_series_suffixes_are_removed(self):
        assert clean_title("Dune (Dune, #1)") == "Dune"
        assert clean_title("Leviathan Wakes (The Expanse #1)") == "Leviathan Wakes"
        assert (
            clean_title("Catch-22 (Anniversary Edition)")
            == "Catch-22 (Anniversary Edition)"
        )


class TestMatching:
    def test_title_and_author_both_have_to_match(self, session: Session):
        entry = ListEntry(title="Dune", author="Frank Herbert")
        results = [
            book("X", "Dune: The Graphic Novel", ["Brian Herbert"]),
            book("Y", "Dune Messiah", ["Frank Herbert"]),
            book("Z", "Dune", ["Frank Herbert"]),
        ]
        found = best_match(session, entry, results)
        assert found is not None and found.asin == "Z"

    def test_the_preferred_language_wins(self, session: Session):
        entry = ListEntry(title="Dune", author="Frank Herbert")
        results = [
            book("DE", "Dune", ["Frank Herbert"], language="german"),
            book("EN", "Dune", ["Frank Herbert"]),
        ]
        found = best_match(session, entry, results)
        assert found is not None and found.asin == "EN"

    def test_author_names_are_compared_sensibly(self):
        from app.internal.reading_list import same_author

        assert same_author("J.R.R. Tolkien", "J. R. R. Tolkien")
        assert same_author("Andy Weir", "Andy Weir")
        assert not same_author("Frank Herbert", "Brian Herbert")

    def test_no_match_is_none(self, session: Session):
        entry = ListEntry(title="An Obscure Novel", author="Nobody")
        assert (
            best_match(session, entry, [book("Z", "Dune", ["Frank Herbert"])]) is None
        )

    async def test_statuses(self, session: Session):
        user = User(username="reader", password="x", group=GroupEnum.trusted)
        owned = book("OWNED", "Dune", ["Frank Herbert"])
        owned.downloaded = True
        asked = book("ASKED", "The Martian", ["Andy Weir"])
        session.add_all([user, owned, asked])
        session.add(AudiobookRequest(asin="ASKED", user_username="reader"))
        session.commit()
        entries = [
            ListEntry(title="Dune", author="Frank Herbert"),
            ListEntry(title="The Martian", author="Andy Weir"),
            ListEntry(title="Hyperion", author="Dan Simmons"),
            ListEntry(title="Unfindable", author="Nobody"),
        ]
        catalogue = {
            "Dune Frank Herbert": [book("OWNED", "Dune", ["Frank Herbert"])],
            "The Martian Andy Weir": [book("ASKED", "The Martian", ["Andy Weir"])],
            "Hyperion Dan Simmons": [book("NEW", "Hyperion", ["Dan Simmons"])],
            "Unfindable Nobody": [],
        }

        async def search(
            _cs: object, query: str, num_results: int = 10
        ) -> list[Audiobook]:
            _ = num_results
            return catalogue[query]

        with mock.patch.object(reading_list, "search_audible_books", new=search):
            async with ClientSession() as cs:
                matches = await reading_list.match_entries(session, cs, user, entries)

        assert [m.status for m in matches] == ["owned", "requested", "new", "missing"]
