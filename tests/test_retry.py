"""Backoff arithmetic and retry classification."""

from __future__ import annotations

import pytest

from slack_api_gateway.retry import RETRYABLE_CODES, RETRYABLE_STATUSES, RetryPolicy


def policy(**kwargs) -> RetryPolicy:
    kwargs.setdefault("jitter", lambda low, high: high)
    return RetryPolicy(**kwargs)


def test_exponential_backoff_is_capped() -> None:
    p = policy(max_attempts=6, base_delay=1.0, max_delay=5.0)
    assert [p.backoff(attempt) for attempt in range(1, 6)] == [1.0, 2.0, 4.0, 5.0, 5.0]


def test_server_retry_after_wins_over_the_ceiling() -> None:
    p = policy(base_delay=1.0, max_delay=2.0)
    assert p.backoff(3, retry_after=90.0) == 90.0


def test_jitter_is_injected_and_respected() -> None:
    calls: list[tuple[float, float]] = []

    def jitter(low: float, high: float) -> float:
        calls.append((low, high))
        return low

    p = RetryPolicy(max_attempts=3, base_delay=1.0, max_delay=8.0, jitter=jitter)
    assert p.backoff(3) == 0.0
    assert calls == [(0.0, 4.0)]


def test_zero_delays_need_no_jitter_call() -> None:
    p = RetryPolicy(max_attempts=2, base_delay=0.0, max_delay=0.0)
    assert p.backoff(1) == 0.0


@pytest.mark.parametrize("status", sorted(RETRYABLE_STATUSES))
def test_retryable_statuses_are_retried(status) -> None:
    assert policy().decide(attempt=1, status=status).retry is True


@pytest.mark.parametrize("code", sorted(RETRYABLE_CODES))
def test_retryable_envelope_codes_are_retried(code) -> None:
    assert policy().decide(attempt=1, status=200, code=code).retry is True


@pytest.mark.parametrize("status", [400, 401, 403, 404, 422])
def test_client_errors_are_answers_not_hiccups(status) -> None:
    decision = policy().decide(attempt=1, status=status, code="channel_not_found")
    assert decision.retry is False
    assert "not retryable" in decision.reason


def test_transport_errors_are_retried() -> None:
    assert policy().decide(attempt=1, transport_error=True).retry is True


def test_attempt_cap_stops_the_loop() -> None:
    decision = policy(max_attempts=3).decide(attempt=3, status=503)
    assert decision.retry is False
    assert "attempt 3 of 3" in decision.reason


def test_envelope_rate_limit_passes_retry_after_through() -> None:
    decision = policy().decide(attempt=1, status=200, code="ratelimited", retry_after=7.0)
    assert decision.retry is True
    assert decision.delay == 7.0


@pytest.mark.parametrize(
    "kwargs, message",
    [
        ({"max_attempts": 0}, "max_attempts must be at least 1"),
        ({"base_delay": -1.0}, "delays cannot be negative"),
        ({"base_delay": 5.0, "max_delay": 1.0}, "max_delay must be >= base_delay"),
    ],
)
def test_policy_rejects_impossible_values(kwargs, message) -> None:
    with pytest.raises(ValueError, match=message):
        RetryPolicy(**kwargs)
