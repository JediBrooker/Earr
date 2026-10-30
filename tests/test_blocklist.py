"""Blocklisting releases that failed, so the next search picks another."""

import uuid
from datetime import datetime
from unittest import mock

import pytest
from aiohttp import ClientSession
from sqlmodel import Session, select

from app.internal import blocklist
from app.internal.activity import remove_download
from app.internal.library.watcher import fail_download
from app.internal.models import (
    BlocklistedRelease,
    DownloadStatusEnum,
    LibraryImport,
    LibraryImportStatusEnum,
    TorrentSource,
)
from app.internal.ranking.download_ranking import rank_sources
from app.internal.ranking.quality_extract import Quality
from tests.conftest import series_book


def source(title: str, guid: str) -> TorrentSource:
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


def grabbed(session: Session) -> LibraryImport:
    book = series_book()
    book.downloaded = True
    book.download_status = DownloadStatusEnum.grabbed
    book.last_searched_at = datetime(2026, 1, 1)
    entry = LibraryImport(
        asin_or_uuid=book.asin,
        book_title=book.title,
        release_title="Brandon Sanderson - The Way of Kings [M4B]",
        client_id="abc",
        protocol="torrent",
        guid="guid-1",
        indexer="Books",
    )
    session.add_all([book, entry])
    session.commit()
    return entry


@pytest.fixture(autouse=True)
def _stub_quality():
    """Quality extraction reaches out to prowlarr, which tests must not do."""
    with mock.patch(
        "app.internal.ranking.download_ranking.extract_qualities",
        new=mock.AsyncMock(return_value=[Quality(file_format="m4b", kbits=100)]),
    ):
        yield


class TestBlocklist:
    def test_matches_by_guid_or_by_name(self, session: Session):
        _ = blocklist.add(session, title="Some Release [M4B]", guid="g1")
        loaded = blocklist.load(session)

        assert loaded.blocks(source("Different name", "g1"))
        assert loaded.blocks(source("some release (m4b)", "other-guid"))
        assert not loaded.blocks(source("Another Release", "g2"))

    def test_adding_the_same_release_twice_keeps_one_entry(self, session: Session):
        first = blocklist.add(session, title="Some Release", guid="g1")
        again = blocklist.add(session, title="SOME RELEASE", guid="g9")

        assert again.id == first.id
        assert len(blocklist.entries(session)) == 1

    def test_removing_allows_it_again(self, session: Session):
        entry = blocklist.add(session, title="Some Release")
        assert blocklist.remove(session, entry.id)
        assert not blocklist.remove(session, uuid.uuid4())
        assert blocklist.entries(session) == []

    async def test_ranking_skips_a_blocklisted_release(self, session: Session):
        bad = source("Brandon Sanderson - The Way of Kings m4b", "bad")
        good = source("The Way of Kings - Brandon Sanderson [M4B]", "good")
        _ = blocklist.add(session, title=bad.title, guid=bad.guid)

        async with ClientSession() as cs:
            ranked = await rank_sources(session, cs, [bad, good], series_book())

        assert [s.guid for s in ranked.valid] == ["good"]
        assert ranked.all[-1].guid == "bad"


class TestFailedDownloads:
    def test_a_failed_download_is_blocklisted_and_the_book_freed(
        self, session: Session
    ):
        entry = grabbed(session)

        fail_download(session, entry, "qBittorrent: missingFiles")

        assert entry.status == LibraryImportStatusEnum.failed
        [blocked] = session.exec(select(BlocklistedRelease)).all()
        assert blocked.guid == "guid-1"
        assert blocked.reason == "qBittorrent: missingFiles"
        book = series_book()
        stored = session.get(type(book), book.asin)
        assert stored is not None
        assert stored.downloaded is False
        assert stored.download_status == DownloadStatusEnum.failed
        assert stored.last_searched_at is None

    async def test_remove_and_blocklist_takes_it_out_of_the_client(
        self, session: Session
    ):
        entry = grabbed(session)
        client = mock.Mock()
        client.name = "qBittorrent"
        client.remove = mock.AsyncMock(return_value=True)

        with (
            mock.patch(
                "app.internal.activity.download_client_config.clients_for",
                return_value=[client],
            ),
            mock.patch(
                "app.internal.research.search_again",
                new=mock.AsyncMock(return_value=0),
            ) as search_again,
        ):
            async with ClientSession() as cs:
                message = await remove_download(session, cs, entry.id, blocklist=True)

        client.remove.assert_awaited_once()
        search_again.assert_awaited_once()
        assert "from qBittorrent" in message
        assert entry.status == LibraryImportStatusEnum.failed
        assert len(blocklist.entries(session)) == 1

    async def test_plain_remove_does_not_blocklist(self, session: Session):
        entry = grabbed(session)
        with mock.patch(
            "app.internal.activity.download_client_config.clients_for",
            return_value=[],
        ):
            async with ClientSession() as cs:
                message = await remove_download(session, cs, entry.id, blocklist=False)

        assert "no download client had it" in message
        assert blocklist.entries(session) == []
        stored = session.get(type(series_book()), entry.asin_or_uuid)
        assert stored is not None and stored.download_status is None
