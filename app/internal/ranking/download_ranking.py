import asyncio
from functools import cmp_to_key
from typing import Callable, final

import pydantic
from aiohttp import ClientSession
from rapidfuzz import fuzz, utils
from sqlmodel import Session

from app.internal import blocklist
from app.internal.language import (
    LANGUAGES,
    detect_release_languages,
    language_label,
    wanted_languages,
)
from app.internal.models import Audiobook, ManualBookRequest, ProwlarrSource
from app.internal.ranking.quality import (
    FileFormat,
    QualityFormatKey,
    QualityRange,
    quality_config,
)
from app.internal.ranking.quality_extract import Quality, extract_qualities
from app.util.log import logger

_RANGE_KEYS: dict[FileFormat, QualityFormatKey] = {
    "flac": "quality_flac",
    "m4b": "quality_m4b",
    "mp3": "quality_mp3",
    "unknown-audio": "quality_unknown_audio",
    "unknown": "quality_unknown",
}

_FORMAT_LABELS: dict[FileFormat, str] = {
    "flac": "FLAC",
    "m4b": "M4B",
    "mp3": "MP3",
    "unknown-audio": "audio, format unknown",
    "unknown": "format unknown",
}

_MATCH_HINT = "A title, author or narrator match is enough."


class RankSource(pydantic.BaseModel):
    source: ProwlarrSource
    quality: Quality


class SourceCheck(pydantic.BaseModel):
    """One thing the ranking looked at, as shown next to a source."""

    label: str
    passed: bool | None = None
    """None for information that ranks a source without passing or failing it."""
    blocking: bool = False
    """Whether this failure keeps the source from being downloaded automatically."""
    hint: str | None = None


class SourceVerdict(pydantic.BaseModel):
    """Why a source ranked where it did and whether it clears the configured bar."""

    meets_bar: bool
    checks: list[SourceCheck]


class RankedSources(pydantic.BaseModel):
    """Every source, best first, plus the subset worth downloading unattended.

    The full list is what the sources page shows, so an admin can still pick a
    source the heuristics rejected. `valid` is what automatic downloading is
    allowed to touch.
    """

    all: list[ProwlarrSource]
    valid: list[ProwlarrSource]
    verdicts: list[SourceVerdict]
    """One per entry of `all`, in the same order."""

    @property
    def best_valid(self) -> ProwlarrSource | None:
        return self.valid[0] if self.valid else None


async def rank_sources(
    session: Session,
    client_session: ClientSession,
    sources: list[ProwlarrSource],
    book: Audiobook | ManualBookRequest,
    is_manual: bool = False,
) -> RankedSources:
    async def get_qualities(source: ProwlarrSource):
        qualities = await extract_qualities(session, client_session, source, book)
        return [RankSource(source=source, quality=q) for q in qualities]

    coros = [get_qualities(source) for source in sources]
    rank_sources = [x for y in await asyncio.gather(*coros) for x in y]

    compare = CompareSource(session, book, is_manual)
    rank_sources.sort(key=cmp_to_key(compare))

    verdicts = [compare.explain(rs) for rs in rank_sources]
    return RankedSources(
        all=[rs.source for rs in rank_sources],
        valid=[
            rs.source
            for rs, verdict in zip(rank_sources, verdicts, strict=True)
            if verdict.meets_bar
        ],
        verdicts=verdicts,
    )


@final
class CompareSource:
    def __init__(
        self,
        session: Session,
        book: Audiobook | ManualBookRequest,
        is_manual: bool = False,
    ):
        self.session = session
        self.book = book
        self.is_manual = is_manual
        self.blocklist = blocklist.load(session)
        self.languages = wanted_languages(session, book)
        self.compare_order = [
            self._compare_valid,
            self._compare_title,
            self._compare_authors,
            self._compare_narrators,
            self._compare_format,
            self._compare_flags,
            self._compare_indexer,
            self._compare_subtitle,
            self._compare_seeders,
            self._compare_age,
        ]

    def __call__(self, a: RankSource, b: RankSource):
        return self.compare(a, b)

    def compare(self, a: RankSource, b: RankSource) -> int:
        return self._get_next_compare(0)(a, b, 1)

    def _get_next_compare(
        self, index: int
    ) -> Callable[[RankSource, RankSource, int], int]:
        def default_compare(a: RankSource, b: RankSource, next_compare: int) -> int:
            _, _, _ = a, b, next_compare
            return 0

        if index < len(self.compare_order):
            return self.compare_order[index]
        return default_compare

    def _quality_range(self, file_format: FileFormat) -> QualityRange:
        return quality_config.get_range(self.session, _RANGE_KEYS[file_format])

    def _is_valid_quality(self, a: RankSource) -> bool:
        if self.is_manual:
            return True

        quality_range = self._quality_range(a.quality.file_format)
        return quality_range.from_kbits < a.quality.kbits < quality_range.to_kbits

    def _author_matches(self, source: ProwlarrSource) -> int:
        """How many of the book's authors the source names, in its title or metadata."""
        name_ratio = quality_config.get_name_exists_ratio(self.session)
        return max(
            vaguely_exist_in_title(self.book.authors, source.title, name_ratio),
            fuzzy_author_narrator_match(
                source.book_metadata.authors, self.book.authors, name_ratio
            ),
        )

    def _narrator_matches(self, source: ProwlarrSource) -> int:
        """How many of the book's narrators the source names, in its title or metadata."""
        name_ratio = quality_config.get_name_exists_ratio(self.session)
        return max(
            vaguely_exist_in_title(self.book.narrators, source.title, name_ratio),
            fuzzy_author_narrator_match(
                source.book_metadata.narrators, self.book.narrators, name_ratio
            ),
        )

    def _has_enough_seeders(self, source: ProwlarrSource) -> bool:
        """Usenet has no seeders, so only torrents are held to the minimum."""
        if source.protocol != "torrent":
            return True
        return source.seeders >= quality_config.get_min_seeders(self.session)

    def _has_minimum_match(self, a: RankSource) -> bool:
        """Check if source has at least a minimal match with the book.

        Requires either:
        - Title match, OR
        - At least one author/narrator match
        """
        title_ratio = quality_config.get_title_exists_ratio(self.session)

        # Check title match
        title_match = exists_in_title(
            self.book.title,
            a.source.title,
            title_ratio,
        )

        if title_match:
            logger.debug(
                "Title matches",
                book_title=self.book.title,
                source_title=a.source.title,
                ratio=title_ratio,
            )
            return True

        # Require at least one author OR one narrator match
        return (
            self._author_matches(a.source) > 0 or self._narrator_matches(a.source) > 0
        )

    def _release_languages(self, source: ProwlarrSource) -> set[str]:
        return detect_release_languages(source.title, self.book.title)

    def _language_ok(self, source: ProwlarrSource) -> bool:
        """A release that doesn't say its language is given the benefit of the doubt."""
        found = self._release_languages(source)
        return not found or bool(found & self.languages)

    def is_valid(self, a: RankSource) -> bool:
        """Whether a source clears the configured bar at all.

        Sorting alone is not enough for automatic downloads: if every result is
        junk, the best of them is still junk, so this is what decides whether a
        source may be grabbed without a human looking at it.
        """
        if self.blocklist.blocks(a.source) or not self._language_ok(a.source):
            return False
        valid = self._is_valid_quality(a) and self._has_enough_seeders(a.source)
        # a reasonable match with the book: title, or author/narrator
        return valid and self._has_minimum_match(a)

    def explain(self, a: RankSource) -> SourceVerdict:
        """Spells out what is_valid checked, plus what else moved the source up or down."""
        source = a.source
        checks: list[SourceCheck] = []

        if self.blocklist.blocks(source):
            checks.append(
                SourceCheck(
                    label="blocklisted",
                    passed=False,
                    blocking=True,
                    hint=(
                        "This release failed or was rejected before. Remove it from "
                        "the blocklist under Activity to allow it again."
                    ),
                )
            )

        found = self._release_languages(source)
        if found:
            ok = bool(found & self.languages)
            wanted = ", ".join(
                LANGUAGES.get(language, language) for language in sorted(self.languages)
            )
            checks.append(
                SourceCheck(
                    label=" / ".join(language_label(f) for f in sorted(found)),
                    passed=ok,
                    blocking=not ok,
                    hint=(
                        f"The release name says what language it is in. Wanted: {wanted}. "
                        "Set preferred languages under Settings > Download."
                    ),
                )
            )

        # any one of title, author or narrator is enough (see _has_minimum_match),
        # so a miss only blocks the source when all of them miss
        title_ratio = quality_config.get_title_exists_ratio(self.session)
        title_score = title_match_score(self.book.title, source.title)
        authors = self._author_matches(source)
        narrators = self._narrator_matches(source)
        matched = title_score > title_ratio or authors > 0 or narrators > 0

        checks.append(
            SourceCheck(
                label=f"title {title_score:.0f}%",
                passed=title_score > title_ratio,
                blocking=not matched,
                hint=(
                    f"How closely the release name contains the book title. Above "
                    f"{title_ratio}% counts as a match. {_MATCH_HINT}"
                ),
            )
        )
        for role, found, people in (
            ("author", authors, self.book.authors),
            ("narrator", narrators, self.book.narrators),
        ):
            if not people:
                continue
            checks.append(
                SourceCheck(
                    label=role
                    if len(people) == 1
                    else f"{role}s {found}/{len(people)}",
                    passed=found > 0,
                    blocking=not matched,
                    hint=(
                        f"Looked for {', '.join(people)} in the release name and "
                        f"its metadata. {_MATCH_HINT}"
                    ),
                )
            )

        if not self.is_manual:
            quality_range = self._quality_range(a.quality.file_format)
            quality_ok = self._is_valid_quality(a)
            checks.append(
                SourceCheck(
                    label=(
                        f"{_FORMAT_LABELS[a.quality.file_format]} "
                        f"· {a.quality.kbits:.0f} kbps"
                    ),
                    passed=quality_ok,
                    blocking=not quality_ok,
                    hint=(
                        "Estimated from the size and the book's runtime. Allowed for "
                        f"this format: {quality_range.from_kbits:.0f} to "
                        f"{quality_range.to_kbits:.0f} kbps."
                    ),
                )
            )

        if source.protocol == "torrent":
            enough = self._has_enough_seeders(source)
            checks.append(
                SourceCheck(
                    label=f"{source.seeders} seeder{'' if source.seeders == 1 else 's'}",
                    passed=enough,
                    blocking=not enough,
                    hint=(
                        f"At least {quality_config.get_min_seeders(self.session)} "
                        "needed for an automatic download."
                    ),
                )
            )

        for flag in quality_config.get_indexer_flags(self.session):
            if flag.score and flag.flag.lower() in source.indexer_flags:
                checks.append(
                    SourceCheck(
                        label=f"{flag.flag} {flag.score:+d}",
                        hint=(
                            "Indexer flag priority from the download settings. It "
                            "moves a source up or down but never blocks it."
                        ),
                    )
                )

        return SourceVerdict(meets_bar=self.is_valid(a), checks=checks)

    def _compare_valid(self, a: RankSource, b: RankSource, next_compare: int) -> int:
        """Sorts sources that fail the configured bar to the bottom"""
        a_valid = self.is_valid(a)
        b_valid = self.is_valid(b)

        if a_valid == b_valid:
            return self._get_next_compare(next_compare)(a, b, next_compare + 1)
        return int(b_valid) - int(a_valid)

    def _compare_format(self, a: RankSource, b: RankSource, next_compare: int) -> int:
        if a.quality.file_format == b.quality.file_format:
            return self._get_next_compare(next_compare)(a, b, next_compare + 1)
        a_index = quality_config.calculate_quality_rank(
            self.session, a.quality.file_format
        )
        b_index = quality_config.calculate_quality_rank(
            self.session, b.quality.file_format
        )
        return a_index - b_index

    def _compare_flags(self, a: RankSource, b: RankSource, next_compare: int) -> int:
        a_score = sum(
            f.score
            for f in quality_config.get_indexer_flags(self.session)
            if f.flag.lower() in a.source.indexer_flags
        )
        b_score = sum(
            f.score
            for f in quality_config.get_indexer_flags(self.session)
            if f.flag.lower() in b.source.indexer_flags
        )
        if a_score == b_score:
            return self._get_next_compare(next_compare)(a, b, next_compare + 1)
        return b_score - a_score

    def _compare_indexer(self, a: RankSource, b: RankSource, next_compare: int) -> int:
        a_index = quality_config.calculate_indexer_rank(
            self.session, a.source.indexer_id
        )
        b_index = quality_config.calculate_indexer_rank(
            self.session, b.source.indexer_id
        )
        if a_index == b_index:
            return self._get_next_compare(next_compare)(a, b, next_compare + 1)
        return a_index - b_index

    def _compare_title(self, a: RankSource, b: RankSource, next_compare: int) -> int:
        a_title = exists_in_title(
            self.book.title,
            a.source.title,
            quality_config.get_title_exists_ratio(self.session),
        )
        b_title = exists_in_title(
            self.book.title,
            b.source.title,
            quality_config.get_title_exists_ratio(self.session),
        )
        if a_title == b_title:
            return self._get_next_compare(next_compare)(a, b, next_compare + 1)
        return int(b_title) - int(a_title)

    def _compare_subtitle(self, a: RankSource, b: RankSource, next_compare: int) -> int:
        if not self.book.subtitle:
            return self._get_next_compare(next_compare)(a, b, next_compare + 1)
        a_title = exists_in_title(
            self.book.subtitle,
            a.source.title,
            quality_config.get_title_exists_ratio(self.session),
        )
        b_title = exists_in_title(
            self.book.subtitle,
            b.source.title,
            quality_config.get_title_exists_ratio(self.session),
        )
        if a_title == b_title:
            return self._get_next_compare(next_compare)(a, b, next_compare + 1)
        return int(b_title) - int(a_title)

    def _compare_authors(self, a: RankSource, b: RankSource, next_compare: int) -> int:
        a_score = self._author_matches(a.source)
        b_score = self._author_matches(b.source)
        if a_score == b_score:
            return self._get_next_compare(next_compare)(a, b, next_compare + 1)
        return b_score - a_score

    def _compare_narrators(
        self, a: RankSource, b: RankSource, next_compare: int
    ) -> int:
        a_score = self._narrator_matches(a.source)
        b_score = self._narrator_matches(b.source)
        if a_score == b_score:
            return self._get_next_compare(next_compare)(a, b, next_compare + 1)
        return b_score - a_score

    def _compare_seeders(self, a: RankSource, b: RankSource, next_compare: int) -> int:
        if a.source.protocol == "usenet" or b.source.protocol == "usenet":
            return self._get_next_compare(next_compare)(a, b, next_compare + 1)
        if a.source.seeders == b.source.seeders:
            return self._get_next_compare(next_compare)(a, b, next_compare + 1)
        return b.source.seeders - a.source.seeders

    def _compare_age(self, a: RankSource, b: RankSource, next_compare: int) -> int:
        if a.source.protocol != b.source.protocol:
            return self._get_next_compare(next_compare)(a, b, next_compare + 1)
        if a.source.protocol == "usenet":
            # With usenets: newer => better
            return int((a.source.publish_date - b.source.publish_date).total_seconds())
        # With torrents: older => better
        return int((b.source.publish_date - a.source.publish_date).total_seconds())


def fuzzy_author_narrator_match(
    source_people: list[str], book_people: list[str], name_exists_ratio: int
) -> int:
    """Calculate a fuzzy matching score between two lists of author/narrator names."""
    if not source_people or not book_people:
        return 0
    score = 0
    for book_person in book_people:
        best_match = 0.0
        for source_person in source_people:
            match_score = fuzz.token_set_ratio(
                book_person, source_person, processor=utils.default_process
            )
            best_match = max(best_match, match_score)

        # Only count matches above threshold
        if best_match > name_exists_ratio:
            score += 1

    return score


def vaguely_exist_in_title(words: list[str], title: str, name_exists_ratio: int) -> int:
    return sum(
        1
        for w in words
        if fuzz.token_set_ratio(w, title, processor=utils.default_process)
        > name_exists_ratio
    )


def title_match_score(word: str, title: str) -> float:
    """How closely `word` appears somewhere in `title`, from 0 to 100."""
    return fuzz.partial_ratio(word, title, processor=utils.default_process)


def exists_in_title(word: str, title: str, title_exists_ratio: int) -> bool:
    return title_match_score(word, title) > title_exists_ratio
