"""A small TTL cache for idempotent reads.

Slack's read endpoints (``conversations.info``, ``users.info``) are cheap but
rate-limited, and agent workflows tend to look up the same channel or user on
every turn. A 60-second cache turns a 50-request loop into one request.

The cache is deliberately dumb: no eviction policy beyond expiry, no
persistence, and it never caches a failure. For a gateway living inside a
single process that is the right amount of machinery.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from typing import Generic, TypeVar

T = TypeVar("T")


class TTLCache(Generic[T]):
    """Thread-safe mapping with per-entry expiry.

    Args:
        default_ttl: Seconds each entry lives. ``0`` disables storage.
        clock: Monotonic time source, injectable for tests.
        max_entries: Soft ceiling; the oldest-expiring entries are dropped
            when exceeded so a long-lived process cannot grow unbounded.
    """

    def __init__(
        self,
        default_ttl: float = 60.0,
        *,
        clock: Callable[[], float] = time.monotonic,
        max_entries: int = 512,
    ) -> None:
        self.default_ttl = default_ttl
        self._clock = clock
        self._max_entries = max(1, max_entries)
        self._entries: dict[str, tuple[T, float]] = {}
        self._lock = threading.Lock()
        self.hits = 0
        self.misses = 0
        self.evictions = 0

    def get(self, key: str) -> T | None:
        with self._lock:
            entry = self._entries.get(key)
            if entry is None:
                self.misses += 1
                return None
            value, expires_at = entry
            if self._clock() >= expires_at:
                del self._entries[key]
                self.misses += 1
                return None
            self.hits += 1
            return value

    def set(self, key: str, value: T, ttl: float | None = None) -> None:
        lifetime = self.default_ttl if ttl is None else ttl
        if lifetime <= 0:
            return
        with self._lock:
            self._entries[key] = (value, self._clock() + lifetime)
            if len(self._entries) > self._max_entries:
                self._evict_locked()

    def _evict_locked(self) -> None:
        # Drop the entries closest to expiry first; they are the least useful.
        overflow = len(self._entries) - self._max_entries
        if overflow <= 0:
            return
        ordered = sorted(self._entries.items(), key=lambda item: item[1][1])
        for key, _ in ordered[:overflow]:
            self._entries.pop(key, None)
            self.evictions += 1

    def delete(self, key: str) -> bool:
        with self._lock:
            return self._entries.pop(key, None) is not None

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()

    def prune(self) -> int:
        """Delete expired entries; returns how many were removed."""
        now = self._clock()
        with self._lock:
            expired = [key for key, (_, expires_at) in self._entries.items() if now >= expires_at]
            for key in expired:
                del self._entries[key]
            return len(expired)

    def stats(self) -> dict[str, float]:
        with self._lock:
            total = self.hits + self.misses
            return {
                "hits": float(self.hits),
                "misses": float(self.misses),
                "evictions": float(self.evictions),
                "entries": float(len(self._entries)),
                "hit_rate": round(self.hits / total, 3) if total else 0.0,
            }

    def __len__(self) -> int:
        with self._lock:
            return len(self._entries)

    def __contains__(self, key: str) -> bool:
        return self.get(key) is not None


class IdempotencyCache:
    """Remembers successful writes so a retried call cannot double-post.

    Slack has no idempotency-key parameter. The pragmatic workaround used by
    production integrations is a local ledger: hash the semantic request, and
    if the exact same request already succeeded inside the TTL, replay the
    stored response instead of posting twice.
    """

    def __init__(
        self,
        default_ttl: float = 600.0,
        *,
        clock: Callable[[], float] = time.monotonic,
        max_entries: int = 512,
    ) -> None:
        self._cache: TTLCache[dict[str, object]] = TTLCache(
            default_ttl, clock=clock, max_entries=max_entries
        )

    def replay(self, key: str) -> dict[str, object] | None:
        return self._cache.get(key)

    def remember(self, key: str, payload: dict[str, object], ttl: float | None = None) -> None:
        self._cache.set(key, payload, ttl)

    def forget(self, key: str) -> bool:
        return self._cache.delete(key)

    def stats(self) -> dict[str, float]:
        return self._cache.stats()


def stable_key(*parts: object) -> str:
    """Build a deterministic cache key from arbitrary scalars.

    Uses ``repr`` for nested containers so dict ordering cannot change the key.
    """

    def render(value: object) -> str:
        if isinstance(value, dict):
            items = sorted((str(k), render(v)) for k, v in value.items())
            return "{" + ",".join(f"{k}:{v}" for k, v in items) + "}"
        if isinstance(value, (list, tuple)):
            return "[" + ",".join(render(v) for v in value) + "]"
        if isinstance(value, float):
            return f"{value:.6f}"
        return repr(value)

    return "|".join(render(part) for part in parts)


__all__ = ["IdempotencyCache", "TTLCache", "stable_key"]
