"""Following authors and series."""

from datetime import datetime, timedelta
from unittest import mock

import pytest
from aiohttp import ClientSession
from sqlmodel import Session, select

from app.internal import follows
from app.internal.audible.catalog import dedupe_editions, plain_text
from app.internal.audible.types import AudibleProduct
from app.internal.language import language_config
from app.internal.models import (
    Audiobook,
    AudiobookRequest,
    FollowKindEnum,
    GroupEnum,
    User,
)
from app.internal.request_limits import allowance, request_limit_config

NOW = datetime.now()


def product(
    asin: str,
    title: str,
    released: datetime,
    *,
    language: str = "english",
    sequence: str = "",
    series_asin: str = "S1",
) -> AudibleProduct:
    return AudibleProduct.model_validate(
        {
            "asin": asin,
            "title": title,
            "release_date": released.date().isoformat(),
            "authors": [{"name": "Brandon Sanderson", "asin": "A1"}],
            "language": language,
            "series": [
                {"asin": series_asin, "title": "Stormlight", "sequence": sequence}
            ]
            if sequence
            else None,
        }
    )


@pytest.fixture
def reader(session: Session) -> User:
    user = User(username="reader", password="x", group=GroupEnum.untrusted)
    session.add(user)
    session.commit()
    return user


@pytest.fixture(autouse=True)
def _configs():
    for c in (language_config, request_limit_config):
        c._cache.clear()  # pyright: ignore[reportPrivateUsage]
    yield


def audible_lists(products: list[AudibleProduct]):
    return mock.patch.object(
        follows, "fetch_products", new=mock.AsyncMock(return_value=products)
    )


def requested(session: Session) -> list[str]:
    return sorted(r.asin for r in session.exec(select(AudiobookRequest)).all())


class TestFollowing:
    async def test_only_books_not_out_yet_are_requested_at_first(
        self, session: Session, reader: User
    ):
        listing = [
            product("OLD", "Old Book", NOW - timedelta(days=900)),
            product("SOON", "Next Book", NOW + timedelta(days=40)),
        ]
        with audible_lists(listing):
            async with ClientSession() as cs:
                follow = await follows.start_following(
                    session,
                    cs,
                    reader,
                    FollowKindEnum.author,
                    "A1",
                    "Brandon Sanderson",
                )

        assert requested(session) == ["SOON"]
        assert set(follow.known_asins) == {"OLD", "SOON"}
        [request] = session.exec(select(AudiobookRequest)).all()
        assert request.via_follow == "Brandon Sanderson"

    async def test_a_new_release_is_requested_on_the_next_check(
        self, session: Session, reader: User
    ):
        with audible_lists([]):
            async with ClientSession() as cs:
                follow = await follows.start_following(
                    session,
                    cs,
                    reader,
                    FollowKindEnum.author,
                    "A1",
                    "Brandon Sanderson",
                )
        listing = [
            product("NEW", "Brand New", NOW - timedelta(days=1)),
            product("DE", "Ganz Neu", NOW - timedelta(days=1), language="german"),
            product("ANCIENT", "Newly Listed Backlist", NOW - timedelta(days=2000)),
        ]
        with audible_lists(listing):
            async with ClientSession() as cs:
                added = await follows.check_follow(session, cs, follow)

        assert [b.asin for b in added] == ["NEW"]
        assert requested(session) == ["NEW"]
        # all three are remembered, so none is looked at again
        assert {"NEW", "DE", "ANCIENT"} <= set(follow.known_asins)
        assert follow.last_error is None

    async def test_another_edition_of_a_book_already_requested_is_skipped(
        self, session: Session, reader: User
    ):
        existing = Audiobook(
            asin="US",
            title="Wind and Truth",
            subtitle=None,
            authors=["Brandon Sanderson"],
            narrators=[],
            cover_image=None,
            release_date=NOW - timedelta(days=3),
            runtime_length_min=100,
        )
        session.add(existing)
        session.add(AudiobookRequest(asin="US", user_username=reader.username))
        session.commit()
        with audible_lists([]):
            async with ClientSession() as cs:
                follow = await follows.start_following(
                    session,
                    cs,
                    reader,
                    FollowKindEnum.author,
                    "A1",
                    "Brandon Sanderson",
                )
        with audible_lists([product("UK", "Wind and Truth", NOW - timedelta(days=2))]):
            async with ClientSession() as cs:
                added = await follows.check_follow(session, cs, follow)

        assert added == []
        assert requested(session) == ["US"]

    async def test_following_twice_returns_the_same_follow(
        self, session: Session, reader: User
    ):
        with audible_lists([]):
            async with ClientSession() as cs:
                a = await follows.start_following(
                    session, cs, reader, FollowKindEnum.series, "S1", "Stormlight"
                )
                b = await follows.start_following(
                    session, cs, reader, FollowKindEnum.series, "S1", "Stormlight"
                )
        assert a.id == b.id

    async def test_audible_returning_nothing_is_recorded(
        self, session: Session, reader: User
    ):
        with audible_lists([]):
            async with ClientSession() as cs:
                follow = await follows.start_following(
                    session, cs, reader, FollowKindEnum.series, "S1", "Stormlight"
                )
        assert follow.last_error == "Audible returned no books"

    async def test_requests_from_a_follow_do_not_use_up_the_limit(
        self, session: Session, reader: User
    ):
        request_limit_config.set_group_limit(session, GroupEnum.untrusted, 1)
        listing = [
            product("SOON1", "One", NOW + timedelta(days=10)),
            product("SOON2", "Two", NOW + timedelta(days=20)),
        ]
        with audible_lists(listing):
            async with ClientSession() as cs:
                _ = await follows.start_following(
                    session,
                    cs,
                    reader,
                    FollowKindEnum.author,
                    "A1",
                    "Brandon Sanderson",
                )

        assert requested(session) == ["SOON1", "SOON2"]
        found = allowance(session, reader)
        assert found is not None and found.used == 0

    def test_unfollowing_someone_elses_follow_is_refused(
        self, session: Session, reader: User
    ):
        from app.internal.models import Follow

        other = User(username="other", password="x", group=GroupEnum.untrusted)
        follow = Follow(
            user_username=reader.username,
            kind=FollowKindEnum.author,
            audible_id="A1",
            name="Brandon Sanderson",
        )
        session.add_all([other, follow])
        session.commit()

        assert not follows.stop_following(session, other, follow.id)
        assert follows.stop_following(session, reader, follow.id)


class TestEditions:
    def test_adaptations_and_abridged_editions_are_not_main_editions(self):
        from app.internal.audible.catalog import is_main_edition

        assert is_main_edition(product("A", "Warbreaker", NOW))
        assert not is_main_edition(
            product("B", "Warbreaker (1 of 3) [Dramatized Adaptation]", NOW)
        )
        abridged = product("C", "Warbreaker", NOW)
        abridged.format_type = "abridged"
        assert not is_main_edition(abridged)

    async def test_a_follow_skips_adaptations(self, session: Session, reader: User):
        listing = [
            product(
                "ADAPT",
                "Light Bringer (1 of 3) [Dramatized Adaptation]",
                NOW + timedelta(days=5),
            ),
            product("BOOK", "Light Bringer", NOW + timedelta(days=9)),
        ]
        with audible_lists(listing):
            async with ClientSession() as cs:
                _ = await follows.start_following(
                    session, cs, reader, FollowKindEnum.author, "A1", "Pierce Brown"
                )
        assert requested(session) == ["BOOK"]

    def test_the_book_page_prefers_the_edition_you_have(self, session: Session):
        from app.internal.book_page import _pick_editions  # pyright: ignore[reportPrivateUsage]

        mine = Audiobook(
            asin="UK",
            title="The Way of Kings",
            subtitle=None,
            authors=["Brandon Sanderson"],
            narrators=[],
            cover_image=None,
            release_date=NOW,
            runtime_length_min=1,
            downloaded=True,
        )
        session.add(mine)
        session.commit()
        editions = [
            product("US", "The Way of Kings", NOW, sequence="1"),
            product("UK", "The Way of Kings", NOW, sequence="1"),
            product("US2", "Words of Radiance", NOW, sequence="2"),
            product("UK2", "Words of Radiance", NOW, sequence="2"),
        ]

        picked = _pick_editions(session, editions, "S1", current_asin="UK2")

        assert [p.asin for p in picked] == ["UK", "UK2"]


class TestCatalog:
    def test_series_editions_keep_one_per_number_and_language(self):
        listing = [
            product("A", "Wind and Truth", NOW, sequence="5"),
            product("B", "Wind and Truth", NOW, sequence="5"),
            product("C", "Viento y verdad", NOW, sequence="5", language="spanish"),
            product("D", "Dawnshard", NOW, sequence="3.5"),
        ]
        assert [p.asin for p in dedupe_editions(listing, "S1")] == ["A", "C", "D"]

    def test_the_numbered_series_wins_over_an_umbrella_one(self):
        book = AudibleProduct.model_validate(
            {
                "asin": "B0052EFJLE",
                "title": "The Way of Kings",
                "release_date": "2011-05-26",
                "language": "english",
                "series": [
                    {"asin": "COS", "title": "The Cosmere", "sequence": ""},
                    {"asin": "SL", "title": "The Stormlight Archive", "sequence": "1"},
                ],
            }
        ).to_audiobook()
        assert book.series == "The Stormlight Archive"
        assert book.series_position == "1"
        assert book.language == "english"

    def test_descriptions_lose_their_markup(self):
        assert (
            plain_text("<p>One <b>bold</b> line.</p><p>Two &amp; more<br/>lines</p>")
            == "One bold line.\n\nTwo & more\n\nlines"
        )
        assert plain_text("<script>alert(1)</script>") == "alert(1)"
        assert plain_text(None) is None
