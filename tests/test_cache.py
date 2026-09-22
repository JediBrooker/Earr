"""The caches are module level singletons, so instance isolation matters.

`_cache` used to be a class attribute, which meant every cache and every config
in the application shared one dict. For the configs that was survivable because
their keys do not overlap. For SimpleCache it was not: the prowlarr source cache
and the prowlarr indexer cache are both keyed by a single string, so cached
search results were readable through the indexer cache and Settings>Indexers
failed with "'list' object has no attribute 'id'".
"""

from sqlmodel import Session

from app.internal.audiobookshelf.config import abs_config
from app.internal.library.config import library_config
from app.internal.prowlarr.util import (
    prowlarr_config,
    prowlarr_indexer_cache,
    prowlarr_source_cache,
)
from app.util.cache import SimpleCache, StringConfigCache


def test_simple_cache_instances_do_not_share_entries():
    a = SimpleCache[str, str]()
    b = SimpleCache[str, str]()

    a.set("only-in-a", "shared-key")

    assert a.get(9999, "shared-key") == "only-in-a"
    assert b.get(9999, "shared-key") is None


def test_prowlarr_caches_are_separate():
    """A cached book search must not be visible as a cached indexer."""
    prowlarr_source_cache.set([], "Dune")

    assert prowlarr_indexer_cache.get_all(9999) == {}


def test_flush_only_affects_one_cache():
    a = SimpleCache[str, str]()
    b = SimpleCache[str, str]()
    a.set("a", "k")
    b.set("b", "k")

    a.flush()

    assert a.get(9999, "k") is None
    assert b.get(9999, "k") == "b"


def test_string_config_instances_do_not_share_entries():
    a = StringConfigCache[str]()
    b = StringConfigCache[str]()
    a._cache["some_key"] = "from-a"  # pyright: ignore[reportPrivateUsage]

    assert b._cache.get("some_key") is None  # pyright: ignore[reportPrivateUsage]


def test_configs_keep_their_own_values(session: Session):
    """Two configs writing different keys must not read each other's values."""
    prowlarr_config.set_base_url(session, "http://prowlarr.example")
    abs_config.set_base_url(session, "http://abs.example")

    assert prowlarr_config.get_base_url(session) == "http://prowlarr.example"
    assert abs_config.get_base_url(session) == "http://abs.example"


def test_config_reads_fall_through_to_the_database(session: Session):
    """A value written by one instance is visible to a fresh one via the db."""
    library_config.set_folder_template(session, "{author}/{title}")
    library_config._cache.clear()  # pyright: ignore[reportPrivateUsage]

    assert library_config.get_folder_template(session) == "{author}/{title}"
