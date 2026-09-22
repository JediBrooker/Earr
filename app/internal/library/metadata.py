"""Writes an Audiobookshelf metadata sidecar next to an organized book.

Audiobookshelf reads a ``metadata.json`` sitting in a book folder and prefers it
over whatever it can infer from the file and folder names. Earr
already knows all of this from the Audible request, so writing it out means ABS
does not have to guess, and a release with messy file names still lands with the
right title, author, narrator and series.

Only fields that are actually known are written. Anything Earr has no source for
(description, publisher, genres, isbn, language) is left out so ABS keeps
whatever it finds elsewhere instead of seeing a blank value.
"""

import json
from pathlib import Path
from typing import Any

from app.internal.models import Audiobook, ManualBookRequest
from app.util.log import logger

METADATA_FILENAME = "metadata.json"


def build_metadata(book: Audiobook | ManualBookRequest) -> dict[str, Any]:  # pyright: ignore[reportExplicitAny]
    """Builds the Audiobookshelf metadata payload for a book."""
    metadata: dict[str, Any] = {  # pyright: ignore[reportExplicitAny]
        "title": book.title,
        "authors": list(book.authors),
        "narrators": list(book.narrators),
    }

    if book.subtitle:
        metadata["subtitle"] = book.subtitle

    if isinstance(book, Audiobook):
        metadata["publishedYear"] = str(book.release_date.year)
        metadata["asin"] = book.asin
        if book.series:
            # ABS expects "Series Name #1", and just the name when there is no
            # position to go with it
            if book.series_position:
                metadata["series"] = [f"{book.series} #{book.series_position}"]
            else:
                metadata["series"] = [book.series]
    elif book.publish_date:
        metadata["publishedDate"] = book.publish_date

    return metadata


def write_metadata(
    target_dir: Path,
    book: Audiobook | ManualBookRequest,
    overwrite: bool = False,
) -> Path | None:
    """Writes the sidecar into an organized book folder.

    An existing file is left alone unless `overwrite` is set, so a sidecar that
    was hand corrected in Audiobookshelf survives a re-import. Returns the path
    that was written, or None if nothing was.
    """
    target = target_dir / METADATA_FILENAME
    if target.exists() and not overwrite:
        logger.debug("Library: metadata sidecar already exists", path=str(target))
        return None

    try:
        _ = target.write_text(
            json.dumps(build_metadata(book), indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
    except OSError as e:
        # a book that is on disk but missing its sidecar is still a good outcome
        logger.warning(
            "Library: failed to write metadata sidecar",
            path=str(target),
            error=str(e),
        )
        return None

    logger.debug("Library: wrote metadata sidecar", path=str(target))
    return target
