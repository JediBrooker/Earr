"""The Audiobookshelf sidecar. ABS prefers this file over the folder layout,
so a wrong value here is more visible than a wrong folder name."""

import json
from pathlib import Path

from app.internal.library.metadata import (
    METADATA_FILENAME,
    build_metadata,
    write_metadata,
)
from tests.conftest import manual_request, series_book, standalone_book


class TestPayload:
    def test_series_is_formatted_the_way_abs_expects(self):
        assert build_metadata(series_book())["series"] == ["The Stormlight Archive #1"]

    def test_series_without_a_position_omits_the_number(self):
        book = series_book(series_position=None)
        assert build_metadata(book)["series"] == ["The Stormlight Archive"]

    def test_standalone_books_have_no_series_key_at_all(self):
        assert "series" not in build_metadata(standalone_book())

    def test_unknown_fields_are_left_out_rather_than_written_empty(self):
        """ABS should keep what it finds elsewhere instead of seeing a blank."""
        payload = build_metadata(series_book())
        for absent in ("description", "publisher", "genres", "isbn", "language"):
            assert absent not in payload

    def test_core_fields(self):
        payload = build_metadata(series_book())
        assert payload["title"] == "The Way of Kings"
        assert payload["authors"] == ["Brandon Sanderson"]
        assert payload["narrators"] == ["Michael Kramer", "Kate Reading"]
        assert payload["publishedYear"] == "2010"
        assert payload["asin"] == "B1"

    def test_a_manual_request_has_no_asin_or_series(self):
        payload = build_metadata(manual_request())
        assert payload["title"] == "An Indie Book"
        assert "asin" not in payload
        assert "series" not in payload


class TestWriting:
    def test_writes_valid_json(self, tmp_path: Path):
        written = write_metadata(tmp_path, series_book())

        assert written == tmp_path / METADATA_FILENAME
        assert json.loads((tmp_path / METADATA_FILENAME).read_text())["title"] == (
            "The Way of Kings"
        )

    def test_an_existing_sidecar_is_left_alone(self, tmp_path: Path):
        """Anything corrected by hand in ABS must survive a re-import."""
        target = tmp_path / METADATA_FILENAME
        _ = target.write_text('{"title": "HAND EDITED"}')

        assert write_metadata(tmp_path, series_book()) is None
        assert json.loads(target.read_text())["title"] == "HAND EDITED"

    def test_overwrite_replaces_it(self, tmp_path: Path):
        target = tmp_path / METADATA_FILENAME
        _ = target.write_text('{"title": "HAND EDITED"}')

        assert write_metadata(tmp_path, series_book(), overwrite=True) is not None
        assert json.loads(target.read_text())["title"] == "The Way of Kings"

    def test_an_unwritable_folder_does_not_raise(self, tmp_path: Path):
        """A book on disk without its sidecar is still a good outcome."""
        assert write_metadata(tmp_path / "does-not-exist", series_book()) is None

    def test_non_ascii_is_preserved(self, tmp_path: Path):
        _ = write_metadata(tmp_path, standalone_book(authors=["Émile Zola"]))
        assert "Émile Zola" in (tmp_path / METADATA_FILENAME).read_text()
