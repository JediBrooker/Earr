"""Which languages the books should be in.

Audible lists every edition it sells, so an author search in the US store
returns the Spanish and German editions too, and a release on an indexer can
be a translation that happens to match the title. The preference decides what
following an author requests, and which releases may be grabbed unattended.
"""

import json
import re
from typing import Literal, cast

from sqlmodel import Session

from app.internal.audible.types import get_region_from_settings
from app.internal.models import Audiobook, ManualBookRequest
from app.util.cache import StringConfigCache

LANGUAGES: dict[str, str] = {
    "english": "English",
    "german": "German",
    "french": "French",
    "spanish": "Spanish",
    "italian": "Italian",
    "portuguese": "Portuguese",
    "dutch": "Dutch",
    "swedish": "Swedish",
    "danish": "Danish",
    "norwegian": "Norwegian",
    "finnish": "Finnish",
    "polish": "Polish",
    "russian": "Russian",
    "japanese": "Japanese",
    "chinese": "Chinese",
    "hindi": "Hindi",
}
"""Keyed by the lower case name Audible uses."""

_REGION_LANGUAGE: dict[str, str] = {
    "us": "english",
    "ca": "english",
    "uk": "english",
    "au": "english",
    "in": "english",
    "de": "german",
    "fr": "french",
    "it": "italian",
    "es": "spanish",
    "jp": "japanese",
    "br": "portuguese",
}


# Words and tags that say what language a release is in. Short codes only count
# in brackets, since "ger" or "spa" on their own turn up inside ordinary titles.
def _codes(alternatives: str) -> str:
    """A short code inside brackets, alone or among other tags: "[GER]", "[ENG / M4B]"."""
    return r"(?<=[\[\(\{/|,])\s*(?:" + alternatives + r")\s*(?=[\]\)\}/|,])"


_MARKERS: dict[str, list[str]] = {
    "english": [r"english", _codes("eng|en")],
    "german": [r"german", r"deutsch", _codes("ger|deu|de")],
    "french": [r"french", r"fran[cç]ais", r"vff?", _codes("fre|fra|fr")],
    "spanish": [r"spanish", r"espa[nñ]ol", r"castellano", _codes("spa|esp|es")],
    "italian": [r"italian", r"italiano", _codes("ita|it")],
    "portuguese": [r"portuguese", r"portugu[eê]s", _codes("por|pt|pt-br")],
    "dutch": [r"dutch", r"nederlands", _codes("dut|nld|nl")],
    "swedish": [r"swedish", r"svenska", _codes("swe|sv")],
    "danish": [r"danish", r"dansk", _codes("dan|da")],
    "norwegian": [r"norwegian", r"norsk", _codes("nor|no")],
    "finnish": [r"finnish", r"suomi", _codes("fin|fi")],
    "polish": [_codes("pol|pl|polish"), r"polski"],
    "russian": [r"russian", _codes("rus|ru")],
    "japanese": [r"japanese", _codes("jpn|jp|ja")],
    "chinese": [r"chinese", r"mandarin", _codes("chi|zho|zh")],
    "hindi": [r"hindi", _codes("hin|hi")],
}
_PATTERNS: dict[str, re.Pattern[str]] = {
    language: re.compile(
        "|".join(rf"(?<!\w){marker}(?!\w)" for marker in markers), re.IGNORECASE
    )
    for language, markers in _MARKERS.items()
}


def detect_release_languages(release_title: str, book_title: str = "") -> set[str]:
    """The languages a release name says it is in. Empty when it doesn't say.

    A marker that is also part of the book's own title is ignored, so "The
    German Genius" is not taken for a German release.
    """
    found: set[str] = set()
    for language, pattern in _PATTERNS.items():
        for match in pattern.finditer(release_title):
            if match.group(0).strip("[](){} ").lower() in book_title.lower():
                continue
            found.add(language)
            break
    return found


def _json_list(raw: str) -> list[object]:
    try:
        loaded = cast(object, json.loads(raw))
    except ValueError:
        return []
    return cast(list[object], loaded) if isinstance(loaded, list) else []


class LanguageConfig(StringConfigCache[Literal["preferred_languages"]]):
    def get_preferred(self, session: Session) -> list[str]:
        """Defaults to the language of the Audible store Earr searches."""
        raw = self.get(session, "preferred_languages")
        if raw:
            languages = [
                str(language)
                for language in _json_list(raw)
                if str(language) in LANGUAGES
            ]
            if languages:
                return languages
        return [_REGION_LANGUAGE.get(get_region_from_settings(), "english")]

    def set_preferred(self, session: Session, languages: list[str]):
        wanted = [language for language in languages if language in LANGUAGES]
        if not wanted:
            self.delete(session, "preferred_languages")
            return
        self.set(session, "preferred_languages", json.dumps(wanted))


language_config = LanguageConfig()


def wanted_languages(session: Session, book: Audiobook | ManualBookRequest) -> set[str]:
    """What a release of this book should be in.

    Someone who requested the German edition wants German, whatever the
    preference says, so a book's own language wins when it is known.
    """
    if isinstance(book, Audiobook) and book.language:
        return {book.language.lower()}
    return set(language_config.get_preferred(session))


def is_preferred(session: Session, language: str | None) -> bool:
    """Whether a book in this language should be requested automatically.

    An unknown language is let through, since refusing it would skip books
    Audible simply didn't label.
    """
    if not language:
        return True
    return language.lower() in language_config.get_preferred(session)


def language_label(language: str | None) -> str:
    if not language:
        return ""
    return LANGUAGES.get(language.lower(), language.title())
