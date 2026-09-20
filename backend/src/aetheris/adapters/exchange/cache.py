"""Bounded in-process TTL cache.

Deliberately not Redis. Phase 2 caches a handful of public market-data
responses on a machine with roughly 1 GB of free RAM; adding a network cache
service would cost more memory than it saves and add a failure mode to a
read-only feature.

Two properties matter more than speed:

* **Bounded.** A hard entry ceiling with least-recently-used eviction, so a
  symbol-per-key cache cannot grow with the size of the traded universe.
* **Never stale-as-live.** An expired entry is a miss and is evicted. The cache
  has no "serve stale while revalidating" mode, because a cached price
  presented as current is exactly the fabrication this system forbids.
"""

from __future__ import annotations

import time
from collections import OrderedDict
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class _Entry[V]:
    value: V
    expires_at: float


class TTLCache[V]:
    """LRU cache with a per-cache TTL.

    Uses a monotonic clock: a wall-clock adjustment (NTP step, DST) must not
    make a cached entry appear fresh for hours.
    """

    def __init__(self, *, ttl_seconds: float, max_entries: int) -> None:
        if ttl_seconds <= 0:
            raise ValueError("ttl_seconds must be positive")
        if max_entries < 1:
            raise ValueError("max_entries must be at least 1")
        self._ttl = ttl_seconds
        self._max_entries = max_entries
        self._entries: OrderedDict[str, _Entry[V]] = OrderedDict()
        self._hits = 0
        self._misses = 0

    def get(self, key: str) -> V | None:
        entry = self._entries.get(key)
        if entry is None:
            self._misses += 1
            return None
        if entry.expires_at <= time.monotonic():
            # Expired entries are dropped, never served.
            del self._entries[key]
            self._misses += 1
            return None
        self._entries.move_to_end(key)
        self._hits += 1
        return entry.value

    def set(self, key: str, value: V) -> None:
        self._entries[key] = _Entry(value=value, expires_at=time.monotonic() + self._ttl)
        self._entries.move_to_end(key)
        while len(self._entries) > self._max_entries:
            self._entries.popitem(last=False)

    def invalidate(self, key: str) -> None:
        self._entries.pop(key, None)

    def clear(self) -> None:
        self._entries.clear()

    def __len__(self) -> int:
        return len(self._entries)

    @property
    def stats(self) -> dict[str, int]:
        return {
            "entries": len(self._entries),
            "max_entries": self._max_entries,
            "hits": self._hits,
            "misses": self._misses,
        }
