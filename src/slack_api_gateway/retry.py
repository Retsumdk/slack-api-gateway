"""Retry decisions: what to retry, how long to wait, and when to stop.

Rules encoded here:

* Retry transport failures, HTTP 408/429/5xx, and Slack's own
  ``ratelimited``/``service_unavailable`` envelope codes.
* Never retry an auth, permission, validation, or not-found error — those are
  answers, not hiccups. Retrying ``channel_not_found`` four times just wastes
  five seconds of the caller's life.
* Honour ``Retry-After`` exactly; otherwise exponential backoff with *full
  jitter* (``random.uniform(0, capped)``). Full jitter matters when a fleet of
  workers is 429'd at the same instant — without it they retry in lockstep and
  get 429'd together, forever.
"""

from __future__ import annotations

import random
from collections.abc import Callable
from dataclasses import dataclass

# HTTP statuses worth another attempt.
RETRYABLE_STATUSES: frozenset[int] = frozenset({408, 429, 500, 502, 503, 504})

# Slack error codes that describe a transient condition rather than a bug.
RETRYABLE_CODES: frozenset[str] = frozenset(
    {
        "ratelimited",
        "rate_limited",
        "service_unavailable",
        "internal_error",
        "fatal_error",
        "request_timeout",
        "team_added_to_org",
    }
)


@dataclass(frozen=True)
class Decision:
    """Outcome of a retry evaluation."""

    retry: bool
    delay: float = 0.0
    reason: str = ""


@dataclass(frozen=True)
class RetryPolicy:
    """Bounded exponential backoff with jitter.

    Args:
        max_attempts: Total attempts, including the first.
        base_delay: Delay before the second attempt, before jitter.
        max_delay: Ceiling for any single wait.
        jitter: ``random.uniform``-style callable; inject a seed for tests.
    """

    max_attempts: int = 5
    base_delay: float = 0.5
    max_delay: float = 30.0
    jitter: Callable[[float, float], float] = random.uniform

    def __post_init__(self) -> None:
        if self.max_attempts < 1:
            raise ValueError("max_attempts must be at least 1")
        if self.base_delay < 0 or self.max_delay < 0:
            raise ValueError("delays cannot be negative")
        if self.max_delay < self.base_delay:
            raise ValueError("max_delay must be >= base_delay")

    def backoff(self, attempt: int, *, retry_after: float | None = None) -> float:
        """Seconds to wait before attempt ``attempt + 1``.

        ``attempt`` is 1-based: after the first failed attempt pass ``1``.
        A server-supplied ``retry_after`` always wins; it is not jittered,
        because the server said exactly what it wanted.
        """
        if retry_after is not None:
            # The server asked for a specific wait; honour it even above max_delay.
            return max(0.0, float(retry_after))
        exponent = max(0, attempt - 1)
        capped = min(self.max_delay, self.base_delay * (2.0**exponent))
        if capped <= 0:
            return 0.0
        return float(self.jitter(0.0, capped))

    def decide(
        self,
        *,
        attempt: int,
        status: int = 0,
        code: str = "",
        retry_after: float | None = None,
        transport_error: bool = False,
    ) -> Decision:
        """Evaluate whether to retry after a failed attempt."""
        if attempt >= self.max_attempts:
            return Decision(False, 0.0, f"attempt {attempt} of {self.max_attempts}")
        retryable = transport_error or status in RETRYABLE_STATUSES or code in RETRYABLE_CODES
        if not retryable:
            return Decision(False, 0.0, f"not retryable (status={status}, code={code or '-'})")
        return Decision(
            True,
            self.backoff(attempt, retry_after=retry_after),
            f"status={status} code={code or '-'}",
        )


__all__ = ["RETRYABLE_CODES", "RETRYABLE_STATUSES", "Decision", "RetryPolicy"]
