"""Renders library folder paths from a user defined template.

A template is a ``/`` separated path where tokens such as ``{author}`` are
replaced with the metadata of a book. Two rules keep templates readable without
needing conditionals:

- A path segment that renders to nothing is dropped, so ``{author}/{series}/{title}``
  collapses to ``Author/Title`` for a book that is not part of a series.
- Text wrapped in square brackets is optional and disappears as a whole as soon
  as one of the tokens inside it is empty, which makes
  ``[{series_position} - ]{title}`` render as just the title for standalone books.
"""

import re
from typing import Mapping, cast

from pydantic import BaseModel

from app.internal.models import Audiobook, ManualBookRequest

MAX_SEGMENT_LENGTH = 180
"""Most filesystems cap a single path component at 255 bytes. Stay well below
that so multi byte characters and an appended file name still fit."""

DEFAULT_FOLDER_TEMPLATE = "{author}/{series}/{title}"

TOKEN_DESCRIPTIONS: dict[str, str] = {
    "author": "First author",
    "authors": "All authors, comma separated",
    "title": "Book title",
    "subtitle": "Book subtitle",
    "series": "Series name, empty if the book is standalone",
    "series_position": "Position within the series, e.g. 1 or 2.5",
    "narrator": "First narrator",
    "narrators": "All narrators, comma separated",
    "year": "Release year",
    "asin": "Audible ASIN, or the request id for manual requests",
}

TEMPLATE_EXAMPLES: list[str] = [
    "{author}/{title}",
    "{author}/{series}/{title}",
    "{author}/{series}/[{series_position} - ]{title}",
    "{author}/{title} ({year})",
    "{series}/{author} - {title}",
]

_TOKEN_PATTERN = re.compile(r"\{([a-z_]+)\}")
_OPTIONAL_PATTERN = re.compile(r"\[([^\[\]]*)\]")
_ILLEGAL_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_WHITESPACE = re.compile(r"\s+")
_WINDOWS_RESERVED = {
    "con",
    "prn",
    "aux",
    "nul",
    *(f"com{i}" for i in range(1, 10)),
    *(f"lpt{i}" for i in range(1, 10)),
}


def _tokens_in(template: str) -> list[str]:
    return cast(list[str], _TOKEN_PATTERN.findall(template))


class TemplateError(ValueError):
    """Raised for a template that can never produce a usable path."""


class SampleBook(BaseModel):
    """A made up book used to show what a template renders to."""

    label: str
    tokens: dict[str, str]


SAMPLE_BOOKS: list[SampleBook] = [
    SampleBook(
        label="Part of a series",
        tokens={
            "author": "Brandon Sanderson",
            "authors": "Brandon Sanderson",
            "title": "The Way of Kings",
            "subtitle": "Book One of the Stormlight Archive",
            "series": "The Stormlight Archive",
            "series_position": "1",
            "narrator": "Michael Kramer",
            "narrators": "Michael Kramer, Kate Reading",
            "year": "2010",
            "asin": "B003ZWFO1X",
        },
    ),
    SampleBook(
        label="Standalone book",
        tokens={
            "author": "Andy Weir",
            "authors": "Andy Weir",
            "title": "The Martian",
            "subtitle": "",
            "series": "",
            "series_position": "",
            "narrator": "R. C. Bray",
            "narrators": "R. C. Bray",
            "year": "2013",
            "asin": "B00B5HZGUG",
        },
    ),
]


def sanitize_segment(segment: str) -> str:
    """Turns arbitrary text into something safe to use as a single folder name.

    Also strips characters that only Windows rejects, because libraries are
    regularly shared over SMB.
    """
    cleaned = _ILLEGAL_CHARS.sub(" ", segment)
    cleaned = _WHITESPACE.sub(" ", cleaned).strip()
    # Windows silently drops trailing dots and spaces from folder names, and a
    # leading dot would hide the folder on unix
    cleaned = cleaned.strip(". ")
    if cleaned.lower() in _WINDOWS_RESERVED:
        cleaned = f"_{cleaned}"
    if len(cleaned) > MAX_SEGMENT_LENGTH:
        cleaned = cleaned[:MAX_SEGMENT_LENGTH].strip(". ")
    return cleaned


def _join(values: list[str]) -> str:
    return ", ".join(v.strip() for v in values if v.strip())


def build_tokens(book: Audiobook | ManualBookRequest) -> dict[str, str]:
    """Collects the metadata a template can reference for a single book."""
    if isinstance(book, Audiobook):
        year = str(book.release_date.year)
        series = book.series or ""
        series_position = book.series_position or ""
        asin = book.asin
    else:
        # manual requests only store a free form publish date
        match = re.search(r"\d{4}", book.publish_date or "")
        year = match.group(0) if match else ""
        series = ""
        series_position = ""
        asin = str(book.id)

    return {
        "author": book.authors[0] if book.authors else "",
        "authors": _join(book.authors),
        "title": book.title,
        "subtitle": book.subtitle or "",
        "series": series,
        "series_position": series_position,
        "narrator": book.narrators[0] if book.narrators else "",
        "narrators": _join(book.narrators),
        "year": year,
        "asin": asin,
    }


def validate_template(template: str) -> None:
    """Raises a TemplateError describing why a template is unusable."""
    if not template.strip():
        raise TemplateError("Folder structure cannot be empty")

    if template.count("[") != template.count("]"):
        raise TemplateError("Unbalanced square brackets in the folder structure")

    unknown = sorted(
        {name for name in _tokens_in(template) if name not in TOKEN_DESCRIPTIONS}
    )
    if unknown:
        known = ", ".join(f"{{{name}}}" for name in sorted(TOKEN_DESCRIPTIONS))
        raise TemplateError(
            f"Unknown placeholder{'s' if len(unknown) > 1 else ''} "
            + ", ".join(f"{{{name}}}" for name in unknown)
            + f". Available placeholders: {known}"
        )

    if not _TOKEN_PATTERN.search(template):
        raise TemplateError(
            "Folder structure needs at least one placeholder, e.g. {title}"
        )

    if template.startswith("/"):
        raise TemplateError(
            "Folder structure is relative to the library folder and cannot start with '/'"
        )

    for sample in SAMPLE_BOOKS:
        if not render_template(template, sample.tokens, validate=False):
            raise TemplateError(
                f"Folder structure renders to an empty path for a {sample.label.lower()}"
            )


def render_template(
    template: str,
    tokens: Mapping[str, str],
    validate: bool = True,
) -> list[str]:
    """Renders a template into sanitized path segments.

    Returns an empty list when nothing is left after dropping empty segments.
    """
    if validate:
        validate_template(template)

    safe = {name: sanitize_segment(value) for name, value in tokens.items()}

    def replace_token(match: re.Match[str]) -> str:
        return safe.get(match.group(1), "")

    def replace_optional(match: re.Match[str]) -> str:
        group = match.group(1)
        if any(not safe.get(name) for name in _tokens_in(group)):
            return ""
        return _TOKEN_PATTERN.sub(replace_token, group)

    rendered = _OPTIONAL_PATTERN.sub(replace_optional, template)
    rendered = _TOKEN_PATTERN.sub(replace_token, rendered)

    segments: list[str] = []
    for raw in rendered.split("/"):
        segment = sanitize_segment(raw)
        if segment:
            segments.append(segment)
    return segments


def render_preview(template: str) -> list[tuple[str, str]]:
    """Renders the sample books so the settings page can show what a template does."""
    preview: list[tuple[str, str]] = []
    for sample in SAMPLE_BOOKS:
        segments = render_template(template, sample.tokens, validate=False)
        preview.append((sample.label, "/".join(segments) or "—"))
    return preview
