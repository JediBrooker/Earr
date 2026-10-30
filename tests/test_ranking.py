"""The bar an automatic download has to clear.

Ranking only sorts. Before the validity guard, query_sources took the top
result unconditionally, so three results that matched nothing and had no
seeders still produced a grab of the least bad one.
"""

from datetime import datetime
from unittest import mock

import pytest
from aiohttp import ClientSession
from fastapi.testclient import TestClient
from sqlmodel import Session

from app.internal.auth.authentication import create_user
from app.internal.models import Audiobook, GroupEnum, TorrentSource, UsenetSource
from app.internal.prowlarr.util import prowlarr_config
from app.internal.ranking.download_ranking import (
    RankedSources,
    SourceCheck,
    SourceVerdict,
    rank_sources,
)
from app.internal.ranking.quality import IndexerFlag, quality_config
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


async def rank(
    session: Session, sources: list[object], book: Audiobook
) -> RankedSources:
    async with ClientSession() as cs:
        return await rank_sources(session, cs, sources, book)  # pyright: ignore[reportArgumentType]


def check(verdict: SourceVerdict, prefix: str) -> SourceCheck:
    """The one check whose label starts with `prefix`."""
    found = [c for c in verdict.checks if c.label.startswith(prefix)]
    assert len(found) == 1, [c.label for c in verdict.checks]
    return found[0]


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


class TestExplanations:
    """The sources page shows why each source ranked where it did."""

    async def test_one_verdict_per_source_agreeing_with_valid(
        self, configured: Session, book: Audiobook
    ):
        sources = [
            torrent("Linux.ISO.2024", 0),
            torrent(GOOD_RELEASE, 25),
            usenet(GOOD_RELEASE + " NZB"),
        ]

        ranked = await rank(configured, sources, book)

        assert len(ranked.verdicts) == len(ranked.all)
        valid = {s.guid for s in ranked.valid}
        for source, verdict in zip(ranked.all, ranked.verdicts, strict=True):
            assert verdict.meets_bar == (source.guid in valid), source.title

    async def test_junk_is_blocked_on_the_match_and_the_seeders(
        self, configured: Session, book: Audiobook
    ):
        ranked = await rank(configured, [torrent("Linux.ISO.2024", 0)], book)
        verdict = ranked.verdicts[0]

        assert not verdict.meets_bar
        for prefix in ("title ", "author", "narrator", "0 seeders"):
            found = check(verdict, prefix)
            assert found.passed is False
            assert found.blocking, prefix

    async def test_an_author_match_excuses_a_mangled_title(
        self, configured: Session, book: Audiobook
    ):
        ranked = await rank(
            configured, [torrent("Brandon Sanderson - Stormlight 01 [M4B]", 20)], book
        )
        verdict = ranked.verdicts[0]

        assert verdict.meets_bar
        title = check(verdict, "title ")
        assert title.passed is False
        assert not title.blocking, "one match out of three is enough"
        assert check(verdict, "author").passed is True

    async def test_too_few_seeders_is_labelled_and_blocking(
        self, configured: Session, book: Audiobook
    ):
        quality_config.set_min_seeders(configured, 5)

        ranked = await rank(configured, [torrent(GOOD_RELEASE, 1)], book)
        seeders = check(ranked.verdicts[0], "1 seeder")

        assert seeders.label == "1 seeder"
        assert seeders.blocking
        assert seeders.hint is not None and "5" in seeders.hint
        assert not ranked.verdicts[0].meets_bar

    async def test_usenet_has_no_seeder_check(
        self, configured: Session, book: Audiobook
    ):
        ranked = await rank(configured, [usenet(GOOD_RELEASE)], book)

        assert not any("seeder" in c.label for c in ranked.verdicts[0].checks)

    async def test_a_bitrate_outside_the_range_is_blocking(
        self, configured: Session, book: Audiobook
    ):
        with mock.patch(
            "app.internal.ranking.download_ranking.extract_qualities",
            new=mock.AsyncMock(return_value=[Quality(file_format="mp3", kbits=900)]),
        ):
            ranked = await rank(configured, [torrent(GOOD_RELEASE, 25)], book)
        verdict = ranked.verdicts[0]

        quality = check(verdict, "MP3")
        assert quality.label == "MP3 · 900 kbps"
        assert quality.blocking
        assert not verdict.meets_bar

    async def test_scored_indexer_flags_are_shown(
        self, configured: Session, book: Audiobook
    ):
        quality_config.set_indexer_flags(
            configured,
            [IndexerFlag(flag="Freeleech", score=5), IndexerFlag(flag="VIP", score=0)],
        )
        source = torrent(GOOD_RELEASE, 25).model_copy(
            update={"indexer_flags": ["freeleech", "vip"]}
        )

        ranked = await rank(configured, [source], book)
        verdict = ranked.verdicts[0]

        flag = check(verdict, "Freeleech")
        assert flag.label == "Freeleech +5"
        assert flag.passed is None and not flag.blocking
        assert not any(c.label.startswith("VIP") for c in verdict.checks)

    async def test_meets_bar_exactly_when_nothing_blocks(
        self, configured: Session, book: Audiobook
    ):
        sources = [
            torrent("Totally Unrelated Cooking Show S01", 0),
            torrent(GOOD_RELEASE, 1),
            torrent(GOOD_RELEASE + " v2", 40),
            torrent("Brandon Sanderson - Stormlight 01 [M4B]", 20),
            torrent("Michael Kramer reads something", 3),
            usenet("Linux.ISO.2024"),
            usenet(GOOD_RELEASE),
        ]

        ranked = await rank(configured, sources, book)

        for source, verdict in zip(ranked.all, ranked.verdicts, strict=True):
            blocked = any(c.blocking for c in verdict.checks)
            assert verdict.meets_bar == (not blocked), source.title


class TestSourcesPage:
    def test_shows_why_each_source_ranked_where_it_did(
        self, client: TestClient, configured: Session, book: Audiobook
    ):
        configured.add(book)
        configured.add(create_user("page-admin", "page-admin-password", GroupEnum.admin))
        configured.commit()
        sources = [torrent(GOOD_RELEASE, 25), torrent("Linux.ISO.2024", 0)]

        with mock.patch(
            "app.internal.query.query_prowlarr",
            new=mock.AsyncMock(return_value=sources),
        ):
            response = client.get(
                "/wishlist/sources/B1",
                params={"only_body": "true"},
                auth=("page-admin", "page-admin-password"),
            )

        assert response.status_code == 200, response.text
        page = response.text
        assert "meets settings" in page
        assert "fails settings" in page
        assert "25 seeders" in page
        assert "0 seeders" in page
        assert "/settings/download" in page
        assert "bg-success/15" in page, "the best source clears the bar"
