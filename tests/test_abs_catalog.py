"""Reading the Audiobookshelf library, and following and recommending from it."""

from datetime import datetime
from typing import Any
from unittest import mock

import pytest
from aiohttp import ClientSession, web
from sqlmodel import Session

from app.internal.audible.catalog import BookDetails
from app.internal.audible.types import AudibleContributor, AudibleProduct, AudibleSeries
from app.internal.audiobookshelf import catalog
from app.internal.audiobookshelf.config import abs_config
from app.internal.library_follows import suggest_from_library
from app.internal.models import (
    AudiobookRequest,
    Follow,
    FollowKindEnum,
    GroupEnum,
    User,
)
from app.internal.recommendations import listening
from tests.conftest import standalone_book

NOW_MS = int(datetime.now().timestamp() * 1000)


def item(item_id: str, asin: str | None, title: str, **metadata: Any) -> dict[str, Any]:
    return {
        "id": item_id,
        "media": {"metadata": {"asin": asin, "title": title, **metadata}},
    }


def abs_routes() -> list[Any]:
    async def items(request: web.Request) -> web.Response:
        return web.json_response(
            {
                "results": [
                    item("li1", "WOK", "The Way of Kings"),
                    item("li2", "WOR", "Words of Radiance"),
                    item("li3", None, "A Book With No ASIN"),
                ]
            }
        )

    async def series(request: web.Request) -> web.Response:
        return web.json_response(
            {
                "results": [
                    {
                        "name": "The Stormlight Archive",
                        "books": [
                            item(
                                "li1",
                                "WOK",
                                "The Way of Kings",
                                series={
                                    "name": "The Stormlight Archive",
                                    "sequence": "1",
                                },
                            ),
                            item(
                                "li2",
                                "WOR",
                                "Words of Radiance",
                                series={
                                    "name": "The Stormlight Archive",
                                    "sequence": "2",
                                },
                            ),
                        ],
                    },
                    {
                        "name": "Nothing Audible Knows",
                        "books": [item("li3", None, "x")],
                    },
                ]
            }
        )

    async def authors(request: web.Request) -> web.Response:
        return web.json_response(
            {
                "authors": [
                    {"name": "Brandon Sanderson", "asin": "B001IGFHW6", "numBooks": 2},
                    {"name": "Jane Doe", "asin": None, "numBooks": 1},
                ]
            }
        )

    async def users(request: web.Request) -> web.Response:
        return web.json_response({"users": [{"id": "usr_1", "username": "Reader"}]})

    async def user(request: web.Request) -> web.Response:
        return web.json_response(
            {
                "id": "usr_1",
                "mediaProgress": [
                    {
                        "libraryItemId": "li2",
                        "progress": 1.0,
                        "isFinished": True,
                        "lastUpdate": NOW_MS,
                    },
                    {
                        "libraryItemId": "li1",
                        "progress": 0.3,
                        "isFinished": False,
                        "lastUpdate": NOW_MS - 10_000,
                    },
                    {
                        "libraryItemId": "pod",
                        "episodeId": "ep1",
                        "progress": 0.5,
                        "lastUpdate": NOW_MS,
                    },
                    {"libraryItemId": "li3", "progress": 0.0, "lastUpdate": NOW_MS},
                ],
            }
        )

    async def batch(request: web.Request) -> web.Response:
        wanted = (await request.json())["libraryItemIds"]
        known = {
            "li1": item(
                "li1",
                "WOK",
                "The Way of Kings",
                authors=[{"name": "Brandon Sanderson"}],
            ),
            "li2": item(
                "li2", "WOR", "Words of Radiance", authorName="Brandon Sanderson"
            ),
        }
        return web.json_response(
            {"libraryItems": [known[i] for i in wanted if i in known]}
        )

    return [
        web.get("/api/libraries/lib1/items", items),
        web.get("/api/libraries/lib1/series", series),
        web.get("/api/libraries/lib1/authors", authors),
        web.get("/api/users", users),
        web.get("/api/users/usr_1", user),
        web.post("/api/items/batch/get", batch),
    ]


@pytest.fixture
async def abs_server(session: Session, serve: Any) -> None:
    catalog.forget_cache()
    url = await serve(abs_routes())
    abs_config.set_base_url(session, url)
    abs_config.set_api_token(session, "token")
    abs_config.set_library_id(session, "lib1")


@pytest.fixture
def reader(session: Session) -> User:
    user = User(username="reader", password="x", group=GroupEnum.trusted)
    session.add(user)
    session.commit()
    return user


@pytest.mark.usefixtures("abs_server")
class TestLibrary:
    async def test_lists_the_asins_on_the_shelf(self, session: Session):
        async with ClientSession() as cs:
            assert await catalog.library_asins(session, cs) == {"WOK", "WOR"}

    async def test_reads_series_with_their_numbers(self, session: Session):
        async with ClientSession() as cs:
            [stormlight, other] = await catalog.library_series(session, cs)
        assert stormlight.name == "The Stormlight Archive"
        assert [(b.asin, b.sequence) for b in stormlight.books] == [
            ("WOK", "1"),
            ("WOR", "2"),
        ]
        assert other.books[0].asin is None

    async def test_listening_history_is_matched_by_username_and_skips_podcasts(
        self, session: Session, reader: User
    ):
        async with ClientSession() as cs:
            history = await catalog.listening_history(session, cs, reader)

        assert [(b.asin, b.finished) for b in history] == [
            ("WOR", True),
            ("WOK", False),
        ]
        assert history[0].authors == ["Brandon Sanderson"]
        assert history[1].authors == ["Brandon Sanderson"]

    async def test_suggests_series_and_authors_to_follow(
        self, session: Session, reader: User
    ):
        details = BookDetails(
            book=standalone_book("WOK"),
            description=None,
            authors=[],
            series=[
                AudibleSeries(asin="COSMERE", title="The Cosmere"),
                AudibleSeries(asin="SL", title="The Stormlight Archive", sequence="1"),
            ],
            format_type=None,
        )
        session.add(
            Follow(
                user_username="reader",
                kind=FollowKindEnum.author,
                audible_id="B001IGFHW6",
                name="Brandon Sanderson",
            )
        )
        session.commit()
        with mock.patch(
            "app.internal.library_follows.get_book_details",
            new=mock.AsyncMock(return_value=details),
        ):
            async with ClientSession() as cs:
                suggestions = await suggest_from_library(session, cs, reader)

        summary = [
            (s.kind.value, s.audible_id, s.owned, s.following) for s in suggestions
        ]
        assert summary == [
            ("series", "SL", 2, False),
            ("author", "B001IGFHW6", 2, True),
            ("author", "Jane Doe", 1, False),
        ]

    async def test_up_next_is_the_next_unowned_book(
        self, session: Session, reader: User
    ):
        def product(asin: str, sequence: str) -> AudibleProduct:
            return AudibleProduct.model_validate(
                {
                    "asin": asin,
                    "title": f"Book {sequence}",
                    "release_date": "2020-01-01",
                    "language": "english",
                    "series": [
                        {"asin": "SL", "title": "Stormlight", "sequence": sequence}
                    ],
                }
            )

        details = BookDetails(
            book=standalone_book("WOR"),
            description=None,
            authors=[AudibleContributor(name="Brandon Sanderson")],
            series=[AudibleSeries(asin="SL", title="Stormlight", sequence="2")],
            format_type=None,
        )
        editions = [
            product("WOK", "1"),
            product("WOR", "2"),
            product("EDGE", "2.5"),
            product("OATH", "3"),
        ]
        # Edgedancer is already asked for, so Oathbringer is next
        session.add(standalone_book("EDGE"))
        session.add(AudiobookRequest(asin="EDGE", user_username="reader"))
        session.commit()
        with (
            mock.patch.object(
                listening, "get_book_details", new=mock.AsyncMock(return_value=details)
            ),
            mock.patch.object(
                listening,
                "get_series_products",
                new=mock.AsyncMock(return_value=editions),
            ),
        ):
            async with ClientSession() as cs:
                picks = await listening.up_next(session, cs, reader)

        assert [p.book.book.asin for p in picks] == ["OATH"]
        assert picks[0].reason == "Next in Stormlight, after Words of Radiance"


async def test_nothing_is_asked_when_audiobookshelf_isnt_connected(
    session: Session, reader: User
):
    async with ClientSession() as cs:
        assert await catalog.library_asins(session, cs) == set()
        assert await catalog.listening_history(session, cs, reader) == []
