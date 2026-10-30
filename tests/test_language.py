"""Which language a release is in, and what that means for grabbing it."""

from datetime import datetime
from unittest import mock

import pytest
from aiohttp import ClientSession
from sqlmodel import Session

from app.internal.language import (
    detect_release_languages,
    is_preferred,
    language_config,
    wanted_languages,
)
from app.internal.models import TorrentSource
from app.internal.ranking.download_ranking import rank_sources
from app.internal.ranking.quality_extract import Quality
from tests.conftest import manual_request, series_book


@pytest.fixture(autouse=True)
def _fresh_language_config():
    language_config._cache.clear()  # pyright: ignore[reportPrivateUsage]
    yield
    language_config._cache.clear()  # pyright: ignore[reportPrivateUsage]


@pytest.fixture(autouse=True)
def _stub_quality():
    """Quality extraction reaches out to prowlarr, which tests must not do."""
    with mock.patch(
        "app.internal.ranking.download_ranking.extract_qualities",
        new=mock.AsyncMock(return_value=[Quality(file_format="m4b", kbits=100)]),
    ):
        yield


@pytest.mark.parametrize(
    ("title", "expected"),
    [
        ("Brandon Sanderson - The Way of Kings [M4B]", set[str]()),
        ("Der Weg der Könige [GER] Brandon Sanderson", {"german"}),
        ("El camino de los reyes (Spanish Edition) mp3", {"spanish"}),
        ("Le Seigneur des Anneaux - VF - 128kbps", {"french"}),
        ("Project Hail Mary [ENG / M4B]", {"english"}),
        ("Dune (Deutsch) Hörbuch", {"german"}),
        ("Something [ITA] audiolibro", {"italian"}),
        ("Dune [M4B / ger]", {"german"}),
        # short codes outside brackets are just letters in a title
        ("The Spa Weekend", set[str]()),
        ("De Profundis (Oscar Wilde)", set[str]()),
    ],
)
def test_detects_the_language_a_release_names(title: str, expected: set[str]):
    assert detect_release_languages(title) == expected


def test_a_marker_that_is_part_of_the_book_title_is_ignored():
    assert (
        detect_release_languages(
            "Peter Watson - The German Genius [M4B]", "The German Genius"
        )
        == set()
    )


class TestPreference:
    def test_defaults_to_the_language_of_the_audible_store(self, session: Session):
        assert language_config.get_preferred(session) == ["english"]

    def test_unknown_languages_are_dropped(self, session: Session):
        language_config.set_preferred(session, ["german", "klingon"])
        assert language_config.get_preferred(session) == ["german"]

    def test_an_empty_choice_falls_back_to_the_default(self, session: Session):
        language_config.set_preferred(session, ["klingon"])
        assert language_config.get_preferred(session) == ["english"]

    def test_unknown_book_languages_count_as_preferred(self, session: Session):
        assert is_preferred(session, None)
        assert is_preferred(session, "English")
        assert not is_preferred(session, "german")

    def test_a_book_in_another_language_wants_that_language(self, session: Session):
        assert wanted_languages(session, series_book(language="german")) == {"german"}
        assert wanted_languages(session, series_book()) == {"english"}
        assert wanted_languages(session, manual_request()) == {"english"}


def source(title: str, guid: str = "g1") -> TorrentSource:
    return TorrentSource(
        guid=guid,
        indexer_id=1,
        indexer="Books",
        title=title,
        size=500 * 1024 * 1024,
        publish_date=datetime(2024, 1, 1),
        info_url=None,
        indexer_flags=[],
        seeders=20,
        leechers=0,
    )


class TestRanking:
    async def test_a_release_in_another_language_is_never_auto_downloaded(
        self, session: Session
    ):
        book = series_book()
        german = source("Brandon Sanderson - The Way of Kings [GER] m4b", "de")
        plain = source("Brandon Sanderson - The Way of Kings m4b", "en")
        async with ClientSession() as cs:
            ranked = await rank_sources(session, cs, [german, plain], book)

        assert [s.guid for s in ranked.valid] == ["en"]
        verdict = ranked.verdicts[[s.guid for s in ranked.all].index("de")]
        language_check = next(c for c in verdict.checks if c.label == "German")
        assert language_check.blocking

    async def test_the_german_edition_takes_the_german_release(self, session: Session):
        book = series_book(language="german", title="Der Weg der Könige")
        german = source("Brandon Sanderson - Der Weg der Könige [GER] m4b", "de")
        async with ClientSession() as cs:
            ranked = await rank_sources(session, cs, [german], book)

        assert [s.guid for s in ranked.valid] == ["de"]
