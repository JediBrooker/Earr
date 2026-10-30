import re
from abc import ABCMeta, abstractmethod
from datetime import datetime
from typing import Literal

from pydantic import BaseModel
from typing_extensions import override

from app.internal.env_settings import Settings
from app.internal.models import Audiobook

REFETCH_TTL = 60 * 60 * 24 * 7  # 1 week

RESPONSE_GROUPS = "media,series"
"""What every catalog lookup asks for. `series` adds the series ASIN and the
book's number within it. Audible only reads a comma separated value; repeating
the parameter silently keeps just one group."""

audible_region_type = Literal[
    "us",
    "ca",
    "uk",
    "au",
    "fr",
    "de",
    "jp",
    "it",
    "in",
    "es",
    "br",
]
audible_regions: dict[audible_region_type, str] = {
    "us": ".com",
    "ca": ".ca",
    "uk": ".co.uk",
    "au": ".com.au",
    "fr": ".fr",
    "de": ".de",
    "jp": ".co.jp",
    "it": ".it",
    "in": ".in",
    "es": ".es",
    "br": ".com.br",
}


def get_region_from_settings() -> audible_region_type:
    region = Settings().app.default_region
    if region not in audible_regions:
        return "us"
    return region


_SERIES_POSITION = re.compile(
    r"\b(?:book|volume|vol\.?|part|#)\s*(\d+(?:\.\d+)?)\b", re.IGNORECASE
)


def _extract_series_position(subtitle: str | None) -> str | None:
    """Best effort read of the position within a series.

    The unauthenticated Audible API does not expose it, but subtitles are
    usually formatted like "The Stormlight Archive, Book 1".
    """
    if not subtitle:
        return None
    match = _SERIES_POSITION.search(subtitle)
    return match.group(1) if match else None


class AudibleContributor(BaseModel):
    name: str
    asin: str | None = None
    """Authors usually have one, narrators never do."""


class AudibleSeries(BaseModel):
    asin: str
    title: str
    sequence: str = ""
    """The book's number in the series, e.g. "2" or "2.5". Empty for umbrella
    series such as "The Cosmere" that do not number their books."""


class AudibleProduct(BaseModel):
    asin: str
    authors: list[AudibleContributor] = []
    narrators: list[AudibleContributor] = []
    product_images: dict[str, str] = {}
    runtime_length_min: int = 0
    release_date: str
    title: str
    subtitle: str | None = None
    publication_name: str | None = None
    """Audible puts the series name here for books that belong to one."""
    series: list[AudibleSeries] | None = None
    language: str | None = None
    """Lower case English name, e.g. "english" or "german"."""
    format_type: str | None = None
    """"unabridged" or "abridged"."""
    merchandising_summary: str | None = None
    """A short description, as HTML."""

    @property
    def primary_series(self) -> AudibleSeries | None:
        """The series that numbers this book, over an umbrella one that doesn't."""
        series = self.series or []
        numbered = [s for s in series if s.sequence]
        return (numbered or series or [None])[0]

    def to_audiobook(self) -> Audiobook:
        cover_image = self.product_images.get("500")
        if not cover_image:
            covers = list(self.product_images.values())
            if covers:  # default to first cover other than the one keyed by "500"
                cover_image = covers[0]

        series = self.primary_series
        return Audiobook(
            asin=self.asin,
            title=self.title,
            subtitle=self.subtitle,
            authors=[author.name for author in self.authors],
            narrators=[narrator.name for narrator in self.narrators],
            cover_image=cover_image,
            release_date=datetime.fromisoformat(self.release_date),
            runtime_length_min=self.runtime_length_min,
            series=series.title if series else self.publication_name,
            series_position=(
                series.sequence
                if series and series.sequence
                else _extract_series_position(self.subtitle)
            ),
            language=self.language,
        )


class _Response(BaseModel, metaclass=ABCMeta):
    @abstractmethod
    def audiobooks(self) -> list[Audiobook]:
        pass


class AudibleSearchResponse(_Response):
    products: list[AudibleProduct]

    @override
    def audiobooks(self) -> list[Audiobook]:
        return [product.to_audiobook() for product in self.products]


class AudibleSimilarResponse(_Response):
    similar_products: list[AudibleProduct]

    @override
    def audiobooks(self) -> list[Audiobook]:
        return [sim.to_audiobook() for sim in self.similar_products]


class AudibleSingleResponse(_Response):
    product: AudibleProduct

    @override
    def audiobooks(self) -> list[Audiobook]:
        return [self.product.to_audiobook()]
