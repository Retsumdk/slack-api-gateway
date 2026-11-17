"""Client-side pacing, so a busy loop cannot exhaust Slack's quota.

Slack's documented limits are per-method, per-workspace, and expressed in
requests per minute (1, 20, 50, 100). This module turns those budgets into a
token bucket: tokens refill continuously at ``per_minute / 60`` per second, and
:meth:`TokenBucket.acquire` blocks just long enough to stay inside the budget.

Two deliberate choices:

* **The bucket is a guard, never an authority.** Slack's own ``429`` +
  ``Retry-After`` is the real limit; when it disagrees with the local budget
  the client yields to the server.
* **Clock and sleep are injected.** Tests assert exact wait times without
  actually sleeping, which is the difference between a 40 ms suite and a
  40 second one.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable

# Conservative per-minute budgets by call family. Slack's published tiers are
# per method; these families group methods that share a budget in practice.
DEFAULT_PACING: dict[str, float] = {
    "chat": 60.0,
    "reactions": 60.0,
    "conversations.read": 50.0,
    "conversations.list": 20.0,
    "users": 50.0,
    "files": 20.0,
    "auth": 100.0,
    "other": 30.0,
}

# Burst allowance: how many calls can go out back-to-back before pacing bites.
DEFAULT_BURST: float = 5.0


class TokenBucket:
    """Thread-safe token bucket with a continuous refill.

    Args:
        per_minute: Sustained budget.
        burst: Maximum tokens held at once.
        clock: Monotonic time source.
        sleep: Blocking sleep.
    """

    def __init__(
        self,
        per_minute: float,
        *,
        burst: float = DEFAULT_BURST,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if per_minute <= 0:
            raise ValueError("per_minute must be positive")
        if burst <= 0:
            raise ValueError("burst must be positive")
        self.per_minute = per_minute
        self.burst = burst
        self._rate_per_second = per_minute / 60.0
        self._clock = clock
        self._sleep = sleep
        self._tokens = burst
        self._updated = clock()
        self._lock = threading.Lock()
        self.total_wait = 0.0

    def _refill(self) -> None:
        now = self._clock()
        elapsed = max(0.0, now - self._updated)
        self._updated = now
        self._tokens = min(self.burst, self._tokens + elapsed * self._rate_per_second)

    def acquire(self, tokens: float = 1.0) -> float:
        """Take ``tokens``, blocking if needed. Returns seconds spent waiting."""
        if tokens > self.burst:
            raise ValueError(f"cannot acquire {tokens} tokens from a bucket of {self.burst}")
        waited = 0.0
        while True:
            with self._lock:
                self._refill()
                if self._tokens >= tokens:
                    self._tokens -= tokens
                    return waited
                deficit = tokens - self._tokens
                delay = deficit / self._rate_per_second
            self._sleep(delay)
            waited += delay
            with self._lock:
                self.total_wait += delay

    @property
    def available(self) -> float:
        with self._lock:
            self._refill()
            return self._tokens


class Pacer:
    """One :class:`TokenBucket` per call family, created lazily."""

    def __init__(
        self,
        budgets: dict[str, float] | None = None,
        *,
        enabled: bool = True,
        burst: float = DEFAULT_BURST,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.budgets = dict(budgets or DEFAULT_PACING)
        self.enabled = enabled
        self._burst = burst
        self._clock = clock
        self._sleep = sleep
        self._buckets: dict[str, TokenBucket] = {}
        self._lock = threading.Lock()

    def bucket(self, family: str) -> TokenBucket:
        with self._lock:
            bucket = self._buckets.get(family)
            if bucket is None:
                budget = self.budgets.get(family, self.budgets.get("other", 30.0))
                bucket = TokenBucket(
                    budget, burst=self._burst, clock=self._clock, sleep=self._sleep
                )
                self._buckets[family] = bucket
            return bucket

    def acquire(self, family: str, tokens: float = 1.0) -> float:
        """Pace one call in ``family``. Returns seconds waited."""
        if not self.enabled or tokens <= 0:
            return 0.0
        return self.bucket(family).acquire(tokens)

    @property
    def total_wait(self) -> float:
        return sum(b.total_wait for b in self._buckets.values())


__all__ = ["DEFAULT_BURST", "DEFAULT_PACING", "Pacer", "TokenBucket"]
