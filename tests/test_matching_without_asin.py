"""Books with no ASIN are recognised by title and author, and found on Audible."""

from datetime import datetime
from unittest import mock

from aiohttp import ClientSession
from sqlmodel import Session

from app.internal import library_follows
from app.internal.audible.catalog import BookDetails
from app.internal.audible.matching import book_key, find_on_audible
from app.internal.audible.types import AudibleContributor, AudibleProduct, AudibleSeries
from app.internal.audiobookshelf.catalog import (
    AbsListenedBook,
    AbsSeries,
    AbsSeriesBook,
    LibraryIndex,
)
from app.internal.models import GroupEnum, User
from app.internal.recommendations import listening
from tests.conftest import standalone_book


def test_the_key_ignores_subtitles_punctuation_and_first_names():
    assert book_key("Fugitive Telemetry", ["Martha Wells"]) == book_key(
        "Fugitive Telemetry: The Murderbot Diaries, Book 6", ["M. Wells"]
    )
    assert book_key("Dune", ["Frank Herbert"]) != book_key("Dune", ["Someone Else"])


def test_the_index_knows_books_by_asin_or_by_title():
    index = LibraryIndex(
        asins={"B1"}, keys={book_key("Exit Strategy", ["Martha Wells"])}
    )
    assert index.has("B1", "anything", [])
    assert index.has("OTHER", "Exit Strategy", ["Martha Wells"])
    assert not index.has("OTHER", "Artificial Condition", ["Martha Wells"])


async def test_audible_lookups_are_matched_and_remembered(session: Session):
    hits = [
        standalone_book("WRONG", title="Fugitive Telemetry", authors=["Someone Else"]),
        standalone_book("RIGHT", title="Fugitive Telemetry", authors=["Martha Wells"]),
    ]
    search = mock.AsyncMock(return_value=hits)
    with mock.patch("app.internal.audible.matching.search_audible_books", new=search):
        async with ClientSession() as cs:
            first = await find_on_audible(
                session, cs, "Fugitive Telemetry", "Martha Wells"
            )
            again = await find_on_audible(
                session, cs, "Fugitive Telemetry", "Martha Wells"
            )
    assert first is not None and first.asin == "RIGHT"
    assert again is first
    assert search.await_count == 1


async def test_a_series_without_asins_is_found_through_its_books(session: Session):
    series = AbsSeries(
        name="The Murderbot Diaries",
        books=[
            AbsSeriesBook(
                asin=None, title="All Systems Red", sequence="1", author="Martha Wells"
            )
        ],
    )
    details = BookDetails(
        book=standalone_book("ASR"),
        description=None,
        authors=[],
        series=[AudibleSeries(asin="MBD", title="The Murderbot Diaries", sequence="1")],
        format_type=None,
    )
    with (
        mock.patch.object(
            library_follows,
            "find_on_audible",
            new=mock.AsyncMock(return_value=standalone_book("ASR")),
        ) as lookup,
        mock.patch.object(
            library_follows,
            "get_book_details",
            new=mock.AsyncMock(return_value=details),
        ),
    ):
        async with ClientSession() as cs:
            import asyncio

            found = await library_follows._resolve_series(  # pyright: ignore[reportPrivateUsage]
                session, cs, series, asyncio.Semaphore(2)
            )
    assert found == ("MBD", "The Murderbot Diaries")
    lookup.assert_awaited_once_with(
        session, mock.ANY, "All Systems Red", "Martha Wells"
    )


async def test_up_next_works_without_asins_and_skips_what_is_owned_by_title(
    session: Session,
):
    user = User(username="reader", password="x", group=GroupEnum.trusted)
    session.add(user)
    session.commit()

    def product(asin: str, title: str, sequence: str) -> AudibleProduct:
        return AudibleProduct.model_validate(
            {
                "asin": asin,
                "title": title,
                "release_date": "2020-01-01",
                "language": "english",
                "authors": [{"name": "Martha Wells"}],
                "series": [
                    {
                        "asin": "MBD",
                        "title": "The Murderbot Diaries",
                        "sequence": sequence,
                    }
                ],
            }
        )

    editions = [
        product("ASR", "All Systems Red", "1"),
        product("AC", "Artificial Condition", "2"),
        product("RP", "Rogue Protocol", "3"),
    ]
    listened = AbsListenedBook(
        asin=None,
        title="All Systems Red",
        authors=["Martha Wells"],
        progress=1.0,
        finished=True,
        last_update=datetime.now(),
    )
    # book 2 is on the shelf, but with no ASIN
    owned = LibraryIndex(keys={book_key("Artificial Condition", ["Martha Wells"])})
    details = BookDetails(
        book=standalone_book("ASR"),
        description=None,
        authors=[AudibleContributor(name="Martha Wells")],
        series=[AudibleSeries(asin="MBD", title="The Murderbot Diaries", sequence="1")],
        format_type=None,
    )
    with (
        mock.patch.object(
            listening, "listening_history", new=mock.AsyncMock(return_value=[listened])
        ),
        mock.patch.object(
            listening, "library_index", new=mock.AsyncMock(return_value=owned)
        ),
        mock.patch.object(
            listening,
            "find_on_audible",
            new=mock.AsyncMock(return_value=standalone_book("ASR")),
        ),
        mock.patch.object(
            listening, "get_book_details", new=mock.AsyncMock(return_value=details)
        ),
        mock.patch.object(
            listening, "get_series_products", new=mock.AsyncMock(return_value=editions)
        ),
    ):
        async with ClientSession() as cs:
            picks = await listening.up_next(session, cs, user)
            seeds = await listening.listening_seeds(session, cs, user)

    assert [p.book.book.asin for p in picks] == ["RP"]
    assert seeds == ["ASR"]
