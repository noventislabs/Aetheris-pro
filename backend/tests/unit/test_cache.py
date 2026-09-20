"""The cache must be bounded, and must never serve an expired entry."""

from __future__ import annotations

import pytest

from aetheris.adapters.exchange.cache import TTLCache


def test_stores_and_returns() -> None:
    cache: TTLCache[str] = TTLCache(ttl_seconds=60, max_entries=4)
    cache.set("a", "alpha")
    assert cache.get("a") == "alpha"


def test_missing_key_is_a_miss() -> None:
    cache: TTLCache[str] = TTLCache(ttl_seconds=60, max_entries=4)
    assert cache.get("nope") is None


def test_expired_entry_is_a_miss_and_is_evicted(monkeypatch: pytest.MonkeyPatch) -> None:
    """An expired entry must never be served -- not even once, not even stale.

    A cached price presented as current is precisely the fabrication the
    system forbids, so expiry is a hard miss rather than a revalidation hint.
    """
    clock = {"now": 1000.0}
    monkeypatch.setattr("aetheris.adapters.exchange.cache.time.monotonic", lambda: clock["now"])

    cache: TTLCache[str] = TTLCache(ttl_seconds=5, max_entries=4)
    cache.set("a", "alpha")
    clock["now"] = 1004.9
    assert cache.get("a") == "alpha"

    clock["now"] = 1005.1
    assert cache.get("a") is None
    assert len(cache) == 0


def test_bounded_by_max_entries_with_lru_eviction() -> None:
    cache: TTLCache[int] = TTLCache(ttl_seconds=60, max_entries=3)
    for index, key in enumerate("abc"):
        cache.set(key, index)
    cache.get("a")  # 'a' becomes most recently used, so 'b' is the victim
    cache.set("d", 3)

    assert len(cache) == 3
    assert cache.get("b") is None
    assert cache.get("a") == 0
    assert cache.get("d") == 3


def test_cannot_grow_without_limit() -> None:
    cache: TTLCache[int] = TTLCache(ttl_seconds=60, max_entries=8)
    for index in range(500):
        cache.set(f"key-{index}", index)
    assert len(cache) == 8


def test_invalidate_and_clear() -> None:
    cache: TTLCache[str] = TTLCache(ttl_seconds=60, max_entries=4)
    cache.set("a", "alpha")
    cache.invalidate("a")
    assert cache.get("a") is None

    cache.set("b", "beta")
    cache.clear()
    assert len(cache) == 0


def test_rejects_nonsense_configuration() -> None:
    with pytest.raises(ValueError, match="ttl_seconds must be positive"):
        TTLCache[str](ttl_seconds=0, max_entries=4)
    with pytest.raises(ValueError, match="max_entries must be at least 1"):
        TTLCache[str](ttl_seconds=5, max_entries=0)


def test_stats_report_bounds() -> None:
    cache: TTLCache[str] = TTLCache(ttl_seconds=60, max_entries=2)
    cache.set("a", "alpha")
    cache.get("a")
    cache.get("missing")
    stats = cache.stats
    assert stats["hits"] == 1
    assert stats["misses"] == 1
    assert stats["max_entries"] == 2
