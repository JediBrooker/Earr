"""Folder templates decide where files land, so getting them wrong writes to
the wrong place on a real library."""

import pytest

from app.internal.library.naming import (
    MAX_SEGMENT_LENGTH,
    TEMPLATE_EXAMPLES,
    TemplateError,
    build_tokens,
    render_template,
    sanitize_segment,
    validate_template,
)
from tests.conftest import manual_request, series_book, standalone_book


def render(template: str, book) -> str:
    return "/".join(render_template(template, build_tokens(book), validate=False))


class TestRendering:
    @pytest.mark.parametrize(
        "template,expected",
        [
            ("{author}/{title}", "Brandon Sanderson/The Way of Kings"),
            (
                "{author}/{series}/{title}",
                "Brandon Sanderson/The Stormlight Archive/The Way of Kings",
            ),
            (
                "{author}/{series}/[{series_position} - ]{title}",
                "Brandon Sanderson/The Stormlight Archive/1 - The Way of Kings",
            ),
            (
                "{author}/[{series} #{series_position}|{title}]",
                "Brandon Sanderson/The Stormlight Archive #1",
            ),
            ("{author}/{title} ({year})", "Brandon Sanderson/The Way of Kings (2010)"),
            ("{narrator}/{title}", "Michael Kramer/The Way of Kings"),
            (
                "{authors}/{narrators}",
                "Brandon Sanderson/Michael Kramer, Kate Reading",
            ),
        ],
    )
    def test_series_book(self, template: str, expected: str):
        assert render(template, series_book()) == expected

    @pytest.mark.parametrize(
        "template,expected",
        [
            ("{author}/{title}", "Andy Weir/The Martian"),
            # the empty series segment is dropped rather than leaving a gap
            ("{author}/{series}/{title}", "Andy Weir/The Martian"),
            ("{author}/{series}/[{series_position} - ]{title}", "Andy Weir/The Martian"),
            # the fallback branch is used instead
            ("{author}/[{series} #{series_position}|{title}]", "Andy Weir/The Martian"),
            ("{author}/{title} ({year})", "Andy Weir/The Martian (2013)"),
        ],
    )
    def test_standalone_book(self, template: str, expected: str):
        assert render(template, standalone_book()) == expected

    def test_manual_request_has_no_series(self):
        assert render("{author}/{series}/{title}", manual_request()) == (
            "Jane Doe/An Indie Book"
        )

    def test_manual_request_year_is_parsed_from_a_free_text_date(self):
        assert render("{title} ({year})", manual_request()) == "An Indie Book (2021)"

    def test_every_shipped_example_renders_for_both_kinds_of_book(self):
        for template in TEMPLATE_EXAMPLES:
            assert render(template, series_book())
            assert render(template, standalone_book())


class TestSanitizing:
    @pytest.mark.parametrize(
        "raw,expected",
        [
            ('A/B: "The Book"?', "A B The Book"),
            ("Mr. Mercedes", "Mr. Mercedes"),
            # windows drops trailing dots and spaces, unix hides leading ones
            ("  ...trailing dots...  ", "trailing dots"),
            (".hidden", "hidden"),
            # nothing usable is left, so the segment disappears
            ("..", ""),
            ("???", ""),
            # reserved device names on windows
            ("CON", "_CON"),
            ("nul", "_nul"),
            ("Æon: Flüx | vol*2", "Æon Flüx vol 2"),
        ],
    )
    def test_segments(self, raw: str, expected: str):
        assert sanitize_segment(raw) == expected

    def test_long_titles_are_truncated(self):
        assert len(sanitize_segment("x" * 500)) == MAX_SEGMENT_LENGTH

    def test_a_title_of_only_dots_cannot_escape_the_library(self):
        book = standalone_book(title="..", authors=[".."])
        assert render_template("{author}/{title}", build_tokens(book), validate=False) == []


class TestValidation:
    @pytest.mark.parametrize(
        "template",
        [
            "",
            "   ",
            "no placeholders at all",
            "{author}/{nope}",
            "/{author}/{title}",
            "{author}/[{title}",
            "{author}/[{a}|{b}|{c}]",
            # renders to nothing for a standalone book
            "{series}",
        ],
    )
    def test_rejected(self, template: str):
        with pytest.raises(TemplateError):
            validate_template(template)

    @pytest.mark.parametrize("template", TEMPLATE_EXAMPLES)
    def test_shipped_examples_are_accepted(self, template: str):
        validate_template(template)

    def test_unknown_placeholder_is_named_in_the_error(self):
        with pytest.raises(TemplateError, match=r"\{nope\}"):
            validate_template("{author}/{nope}")
