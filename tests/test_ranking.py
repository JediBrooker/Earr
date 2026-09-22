"""The bar an automatic download has to clear.

Ranking only sorts. Before the validity guard, query_sources took the top
result unconditionally, so three results that matched nothing and had no
seeders still produced a grab of the least bad one.
"""

from datetime import datetime
from unittest import mock

import pytest
from aiohttp import ClientSession
from sqlmodel import Session

from app.internal.models import Audiobook, TorrentSource, UsenetSource
from app.internal.prowlarr.util import prowlarr_config
from app.internal.ranking.download_ranking import rank_sources
from app.internal.ranking.quality import quality_config
from app.internal.ranking.quality_extract import Quality

GOOD_RELEASE = "Brandon Sanderson - The Way of Kings [M4B 64kbps]"


def torrent(title: str, seeders: int) -> TorrentSource:
    return TorrentSource(
        guid=title,
        indexer_id=1,
        indexer="MAM",
        title=title,
        size=10**9,
        publish_date=datetime.now(),
        info_url=None,
        indexer_flags=[],
        seeders=seeders,
        leechers=0,
    )


def usenet(title: str) -> UsenetSource:
    return UsenetSource(
        guid=title,
        indexer_id=1,
        indexer="NZB",
        title=title,
        size=10**9,
        publish_date=datetime.now(),
        info_url=None,
        indexer_flags=[],
        grabs=5,
    )


@pytest.fixture
def book() -> Audiobook:
    return Audiobook(
        asin="B1",
        title="The Way of Kings",
        subtitle=None,
        authors=["Brandon Sanderson"],
        narrators=["Michael Kramer"],
        cover_image=None,
        release_date=datetime(2010, 8, 31),
        runtime_length_min=2734,
    )


@pytest.fixture(autouse=True)
def _stub_quality():
    """Quality extraction reaches out to prowlarr, which tests must not do."""
    with mock.patch(
        "app.internal.ranking.download_ranking.extract_qualities",
        new=mock.AsyncMock(return_value=[Quality(file_format="unknown", kbits=100)]),
    ):
        yield


@pytest.fixture
def configured(session: Session) -> Session:
    prowlarr_config.set_base_url(session, "http://prowlarr.example")
    prowlarr_config.set_api_key(session, "key")
    return session


async def rank(session: Session, sources: list[object], book: Audiobook):
    async with ClientSession() as cs:
        return await rank_sources(session, cs, sources, book)  # pyright: ignore[reportArgumentType]


class TestValidity:
    async def test_junk_produces_nothing_to_download(
        self, configured: Session, book: Audiobook
    ):
        junk = [
            torrent("Totally Unrelated Cooking Show S01", 0),
            torrent("Linux.ISO.2024", 0),
            torrent("Some Random Podcast Episode 44", 1),
        ]

        ranked = await rank(configured, junk, book)

        assert len(ranked.all) == 3, "the sources page still shows everything"
        assert ranked.valid == []
        assert ranked.best_valid is None

    async def test_the_matching_release_is_picked_out_of_the_junk(
        self, configured: Session, book: Audiobook
    ):
        sources = [
            torrent("Totally Unrelated Cooking Show S01", 0),
            torrent(GOOD_RELEASE, 25),
            torrent("Linux.ISO.2024", 0),
        ]

        ranked = await rank(configured, sources, book)

        assert ranked.best_valid is not None
        assert ranked.best_valid.title == GOOD_RELEASE

    async def test_the_right_book_below_the_seeder_floor_is_refused(
        self, configured: Session, book: Audiobook
    ):
        quality_config.set_min_seeders(configured, 5)

        ranked = await rank(configured, [torrent(GOOD_RELEASE, 1)], book)

        assert ranked.best_valid is None

    async def test_lowering_the_floor_admits_it(
        self, configured: Session, book: Audiobook
    ):
        quality_config.set_min_seeders(configured, 0)

        ranked = await rank(configured, [torrent(GOOD_RELEASE, 1)], book)

        assert ranked.best_valid is not None

    async def test_usenet_is_not_judged_on_seeders(
        self, configured: Session, book: Audiobook
    ):
        quality_config.set_min_seeders(configured, 50)

        ranked = await rank(configured, [usenet(GOOD_RELEASE)], book)

        assert ranked.best_valid is not None

    async def test_an_author_match_is_enough_when_the_title_is_mangled(
        self, configured: Session, book: Audiobook
    ):
        ranked = await rank(
            configured, [torrent("Brandon Sanderson - Stormlight 01 [M4B]", 20)], book
        )

        assert ranked.best_valid is not None

    async def test_no_sources_at_all(self, configured: Session, book: Audiobook):
        ranked = await rank(configured, [], book)

        assert ranked.all == []
        assert ranked.best_valid is None


class TestOrdering:
    async def test_valid_sources_sort_above_invalid_ones(
        self, configured: Session, book: Audiobook
    ):
        sources = [torrent("Linux.ISO.2024", 0), torrent(GOOD_RELEASE, 25)]

        ranked = await rank(configured, sources, book)

        assert ranked.all[0].title == GOOD_RELEASE

    async def test_more_seeders_wins_between_two_valid_sources(
        self, configured: Session, book: Audiobook
    ):
        sources = [torrent(GOOD_RELEASE, 3), torrent(GOOD_RELEASE + " v2", 99)]

        ranked = await rank(configured, sources, book)

        assert ranked.valid[0].seeders == 99  # pyright: ignore[reportAttributeAccessIssue]
