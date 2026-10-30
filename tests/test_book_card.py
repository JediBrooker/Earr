"""What a search result says about where a book stands."""

import pytest

from app.internal.auth.authentication import DetailedUser
from app.internal.auth.login_types import LoginTypeEnum
from app.internal.models import (
    AudiobookRequest,
    AudiobookWithRequests,
    DownloadStatusEnum,
    GroupEnum,
    User,
)
from app.util.templates import catalog
from tests.conftest import standalone_book


def viewer() -> DetailedUser:
    user = User(username="reader", password="unused", group=GroupEnum.trusted)
    return DetailedUser.model_validate(user, update={"login_type": LoginTypeEnum.forms})


def card(requested: bool = False, **book: object) -> str:
    audiobook = standalone_book(**book)
    requests = (
        [AudiobookRequest(asin=audiobook.asin, user_username="someone")]
        if requested
        else []
    )
    return catalog.render(  # pyright: ignore[reportAny]
        "BookCard",
        book_with_requests=AudiobookWithRequests(
            book=audiobook, requests=requests, username="reader"
        ),
        auto_start_download=False,
        region="us",
        user=viewer(),
    )


@pytest.mark.parametrize(
    "book, requested, label",
    [
        ({"downloaded": True, "download_status": DownloadStatusEnum.grabbed}, True, "Downloading"),
        ({"downloaded": True, "download_status": DownloadStatusEnum.downloaded}, True, "In library"),
        ({"downloaded": True}, False, "In library"),
        ({"download_status": DownloadStatusEnum.failed}, True, "Requested"),
        ({}, True, "Requested"),
    ],
)
def test_status(book: dict[str, object], requested: bool, label: str):
    html = card(requested, **book)

    assert "book-status" in html
    assert f">{label}</span>" in html


def test_a_book_nobody_asked_for_has_no_status():
    assert "book-status" not in card()
