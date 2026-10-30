"""An author's books, a series' books, and everything Audible says about one book.

The search endpoint only ever returns a page of loosely matching results. These
ask the catalog more precisely: by author, newest first, or by walking a
series' own list of books.
"""

import html
import re
import time
from datetime import datetime

from aiohttp import ClientSession
from pydantic import BaseModel
from rapidfuzz import utils

from app.internal.audible.types import (
    RESPONSE_GROUPS,
    AudibleContributor,
    AudibleProduct,
    AudibleSearchResponse,
    AudibleSeries,
    AudibleSingleResponse,
    audible_region_type,
    audible_regions,
    get_region_from_settings,
)
from app.internal.models import Audiobook
from app.util.log import logger

CACHE_SECONDS = 6 * 60 * 60
"""New releases appear rarely, and the follow checker runs a few times a day."""
BATCH_SIZE = 50
"""The most ASINs Audible returns for in one lookup."""
AUTHOR_RESULTS = 50


class BookDetails(BaseModel):
    book: Audiobook
    description: str | None
    authors: list[AudibleContributor]
    series: list[AudibleSeries]
    format_type: str | None

    @property
    def primary_series(self) -> AudibleSeries | None:
        numbered = [s for s in self.series if s.sequence]
        return (numbered or self.series or [None])[0]


class _Relationship(BaseModel):
    asin: str
    relationship_to_product: str | None = None
    relationship_type: str | None = None
    sequence: str | None = None
    sort: str | None = None


class _SeriesProduct(BaseModel):
    asin: str
    title: str | None = None
    relationships: list[_Relationship] = []


class _SeriesResponse(BaseModel):
    product: _SeriesProduct


_details_cache: dict[str, tuple[float, BookDetails]] = {}
_list_cache: dict[str, tuple[float, list[AudibleProduct]]] = {}


def _fresh[T](cache: dict[str, tuple[float, T]], key: str) -> T | None:
    hit = cache.get(key)
    if hit and time.time() - hit[0] < CACHE_SECONDS:
        return hit[1]
    return None


def _remember[T](cache: dict[str, tuple[float, T]], key: str, value: T) -> T:
    cache[key] = (time.time(), value)
    return value


def forget_cache() -> None:
    _details_cache.clear()
    _list_cache.clear()


def _base(region: audible_region_type) -> str:
    return f"https://api.audible{audible_regions[region]}/1.0/catalog/products"


_TAGS = re.compile(r"<[^>]+>")
_BREAKS = re.compile(r"</p>|<br\s*/?>", re.IGNORECASE)


def plain_text(summary: str | None) -> str | None:
    """Audible's description is HTML. Only the text is kept, so nothing from
    it can run in the page; paragraph breaks survive as blank lines."""
    if not summary:
        return None
    text = _BREAKS.sub("\n\n", summary)
    text = html.unescape(_TAGS.sub("", text))
    paragraphs = [" ".join(p.split()) for p in text.split("\n\n")]
    return "\n\n".join(p for p in paragraphs if p) or None


async def get_book_details(
    client_session: ClientSession,
    asin: str,
    region: audible_region_type | None = None,
) -> BookDetails | None:
    region = region or get_region_from_settings()
    key = f"{region}:{asin}"
    if (hit := _fresh(_details_cache, key)) is not None:
        return hit
    try:
        async with client_session.get(
            f"{_base(region)}/{asin}", params={"response_groups": RESPONSE_GROUPS}
        ) as response:
            if response.status == 404:
                return None
            response.raise_for_status()
            product = AudibleSingleResponse.model_validate(
                await response.json()
            ).product
    except Exception as e:
        logger.warning("Audible: could not fetch book", asin=asin, error=str(e))
        return None
    details = BookDetails(
        book=product.to_audiobook(),
        description=plain_text(product.merchandising_summary),
        authors=product.authors,
        series=product.series or [],
        format_type=product.format_type,
    )
    return _remember(_details_cache, key, details)


async def get_products(
    client_session: ClientSession,
    asins: list[str],
    region: audible_region_type,
) -> list[AudibleProduct]:
    """Looks up many books at once, in batches Audible accepts."""
    found: list[AudibleProduct] = []
    for start in range(0, len(asins), BATCH_SIZE):
        batch = asins[start : start + BATCH_SIZE]
        async with client_session.get(
            _base(region),
            params={
                "asins": ",".join(batch),
                "num_results": str(len(batch)),
                "response_groups": RESPONSE_GROUPS,
            },
        ) as response:
            response.raise_for_status()
            found.extend(
                AudibleSearchResponse.model_validate(await response.json()).products
            )
    return found


def _sequence_key(sequence: str) -> tuple[float, str]:
    """Orders "2", "2.5" and "10" numerically, with unnumbered books last."""
    try:
        return float(sequence.split("-")[0]), sequence
    except ValueError:
        return float("inf"), sequence


def series_sequence(product: AudibleProduct, series_asin: str) -> str:
    for series in product.series or []:
        if series.asin == series_asin:
            return series.sequence
    return ""


_ADAPTATION = re.compile(r"dramati[sz]ed adaptation|\(\d+ of \d+\)", re.IGNORECASE)


def is_main_edition(product: AudibleProduct) -> bool:
    """Not abridged, and not a full-cast adaptation split into parts, which is
    rarely what someone following an author means."""
    if (product.format_type or "").lower() == "abridged":
        return False
    return not _ADAPTATION.search(product.title)


async def get_series_products(
    client_session: ClientSession,
    series_asin: str,
    region: audible_region_type | None = None,
    *,
    all_editions: bool = False,
) -> list[AudibleProduct]:
    """Every book Audible lists in a series, in reading order.

    Audible often lists the same book twice, once per publisher, so only the
    earliest edition of each number is kept unless `all_editions` is set.
    """
    region = region or get_region_from_settings()
    key = f"series:{region}:{series_asin}"
    if (hit := _fresh(_list_cache, key)) is not None:
        return hit if all_editions else dedupe_editions(hit, series_asin)
    try:
        async with client_session.get(
            f"{_base(region)}/{series_asin}",
            params={"response_groups": "relationships"},
        ) as response:
            response.raise_for_status()
            parent = _SeriesResponse.model_validate(await response.json()).product
        children = [
            r.asin
            for r in parent.relationships
            if r.relationship_to_product == "child" and r.relationship_type == "series"
        ]
        products = await get_products(client_session, children, region)
    except Exception as e:
        logger.warning(
            "Audible: could not list series", series_asin=series_asin, error=str(e)
        )
        return []

    products.sort(
        key=lambda p: (
            _sequence_key(series_sequence(p, series_asin)),
            p.release_date,
        )
    )
    _ = _remember(_list_cache, key, products)
    return products if all_editions else dedupe_editions(products, series_asin)


def _title_key(product: AudibleProduct) -> str:
    return utils.default_process(product.title)


def dedupe_editions(
    products: list[AudibleProduct], series_asin: str | None = None
) -> list[AudibleProduct]:
    """Keeps the first edition of each book, by series number or by title.

    Expects the list in the order editions should win, and keeps that order.
    Editions in different languages are different books here; a language filter
    is applied separately.
    """
    seen: set[tuple[str, str]] = set()
    kept: list[AudibleProduct] = []
    for product in products:
        sequence = series_sequence(product, series_asin) if series_asin else ""
        key = (
            product.language or "",
            f"#{sequence}" if sequence else _title_key(product),
        )
        if key in seen:
            continue
        seen.add(key)
        kept.append(product)
    return kept


def _same_person(a: str, b: str) -> bool:
    return utils.default_process(a) == utils.default_process(b)


async def get_author_products(
    client_session: ClientSession,
    author: AudibleContributor,
    region: audible_region_type | None = None,
) -> list[AudibleProduct]:
    """An author's books, newest first.

    Audible's author filter matches names loosely, so a result only counts when
    it really credits this author: by ASIN where Audible gives one, by name
    otherwise.
    """
    region = region or get_region_from_settings()
    key = f"author:{region}:{author.asin or author.name}"
    if (hit := _fresh(_list_cache, key)) is not None:
        return hit
    try:
        async with client_session.get(
            _base(region),
            params={
                "author": author.name,
                "products_sort_by": "-ReleaseDate",
                "num_results": str(AUTHOR_RESULTS),
                "response_groups": RESPONSE_GROUPS,
            },
        ) as response:
            response.raise_for_status()
            products = AudibleSearchResponse.model_validate(
                await response.json()
            ).products
    except Exception as e:
        logger.warning(
            "Audible: could not list author", author=author.name, error=str(e)
        )
        return []

    def credits(product: AudibleProduct) -> bool:
        for credited in product.authors:
            if author.asin and credited.asin:
                if credited.asin == author.asin:
                    return True
            elif _same_person(credited.name, author.name):
                return True
        return False

    mine = [p for p in products if credits(p)]
    # oldest edition first so it wins the dedupe, then back to newest first
    mine.sort(key=lambda p: p.release_date)
    kept = dedupe_editions(mine)
    kept.sort(key=lambda p: p.release_date, reverse=True)
    return _remember(_list_cache, key, kept)


def release_date(product: AudibleProduct) -> datetime:
    return datetime.fromisoformat(product.release_date)
