"""The home page's For You and Popular rows.

An upstream refactor dropped the `reasons` parameter from PopularSection, so
both rows showed their "nothing here" text even when the routes passed books.
"""

from app.internal.auth.authentication import DetailedUser
from app.internal.auth.login_types import LoginTypeEnum
from app.internal.models import AudiobookWithRequests, GroupEnum, User
from app.internal.recommendations.local import AudiobookPopularity
from app.internal.recommendations.user_recommendations import AudiobookRecommendation
from app.util.templates import catalog
from tests.conftest import series_book, standalone_book

EMPTY = "Nothing to show yet"
CARD = 'class="book-card'


def viewer() -> DetailedUser:
    # built the way the auth layer builds it; constructing a DetailedUser
    # directly trips over the table model's column defaults
    user = User(username="reader", password="unused", group=GroupEnum.trusted)
    return DetailedUser.model_validate(user, update={"login_type": LoginTypeEnum.forms})


def with_requests(book: object) -> AudiobookWithRequests:
    return AudiobookWithRequests(book=book, requests=[], username="reader")  # pyright: ignore[reportArgumentType]


def render(reasons: list[object], **extra: object) -> str:
    return catalog.render(  # pyright: ignore[reportAny]
        "Index.PopularSection",
        title="For You",
        user=viewer(),
        region="us",
        reasons=reasons,
        empty=EMPTY,
        **extra,
    )


class TestRows:
    def test_recommendations_are_shown(self):
        html = render(
            [
                AudiobookRecommendation(
                    book=with_requests(series_book("B1")), reason="Because you asked"
                ),
                AudiobookRecommendation(book=with_requests(standalone_book("B2"))),
            ]
        )

        assert html.count(CARD) == 2
        assert "The Way of Kings" in html
        assert 'title="Because you asked"' in html
        assert EMPTY not in html

    def test_popular_books_carry_their_request_count(self):
        popular = AudiobookPopularity(
            book=with_requests(standalone_book()), request_count=3
        )

        html = render([popular])

        assert html.count(CARD) == 1
        assert f'title="{popular.reason}"' in html

    def test_nothing_to_show(self):
        html = render([])

        assert CARD not in html
        assert EMPTY in html

    def test_auto_download_from_the_routes_reaches_the_cards(self):
        """The /hx-* routes pass the flag as `auto_download`."""
        reasons = [AudiobookRecommendation(book=with_requests(standalone_book()))]

        plain = render(reasons)
        automatic = render(reasons, auto_download=True)

        assert plain != automatic, "a trusted user's button switches to download"
