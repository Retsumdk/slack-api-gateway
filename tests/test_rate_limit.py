"""Token-bucket pacing, with time fully under the test's control."""

from __future__ import annotations

import pytest

from slack_api_gateway.rate_limit import Pacer, TokenBucket


def test_burst_is_free_then_pacing_bites(clock) -> None:
    bucket = TokenBucket(60.0, burst=2, clock=clock, sleep=clock.sleep)  # 1 token/second
    assert bucket.acquire() == 0.0
    assert bucket.acquire() == 0.0
    waited = bucket.acquire()
    assert waited == pytest.approx(1.0)
    assert clock.sleeps == [pytest.approx(1.0)]


def test_tokens_refill_with_elapsed_time(clock) -> None:
    bucket = TokenBucket(60.0, burst=1, clock=clock, sleep=clock.sleep)
    bucket.acquire()
    clock.now += 0.5
    assert bucket.available == pytest.approx(0.5)
    clock.now += 10
    assert bucket.available == pytest.approx(1.0)  # capped at burst


def test_sustained_rate_is_enforced_over_many_calls(clock) -> None:
    bucket = TokenBucket(20.0, burst=1, clock=clock, sleep=clock.sleep)  # 3s per call
    for _ in range(4):
        bucket.acquire()
    assert clock.total_slept == pytest.approx(9.0)


def test_multiple_tokens_per_call(clock) -> None:
    bucket = TokenBucket(120.0, burst=10, clock=clock, sleep=clock.sleep)
    bucket.acquire(10)
    assert bucket.acquire(5) == pytest.approx(2.5)


def test_impossible_requests_are_rejected(clock) -> None:
    with pytest.raises(ValueError, match="per_minute must be positive"):
        TokenBucket(0, clock=clock, sleep=clock.sleep)
    with pytest.raises(ValueError, match="burst must be positive"):
        TokenBucket(1, burst=0, clock=clock, sleep=clock.sleep)
    bucket = TokenBucket(1, burst=1, clock=clock, sleep=clock.sleep)
    with pytest.raises(ValueError, match="cannot acquire 5 tokens"):
        bucket.acquire(5)


def test_families_get_independent_budgets(clock) -> None:
    pacer = Pacer(
        {"chat": 60.0, "users": 60.0, "other": 60.0}, burst=1, clock=clock, sleep=clock.sleep
    )
    assert pacer.acquire("chat") == 0.0
    assert pacer.acquire("users") == 0.0  # separate bucket, no wait
    assert pacer.acquire("chat") == pytest.approx(1.0)
    assert pacer.total_wait == pytest.approx(1.0)


def test_unknown_families_fall_back_to_other(clock) -> None:
    pacer = Pacer({"other": 60.0}, burst=1, clock=clock, sleep=clock.sleep)
    pacer.acquire("never.heard.of.it")
    assert pacer.acquire("never.heard.of.it") == pytest.approx(1.0)


def test_disabled_pacer_never_waits(clock) -> None:
    pacer = Pacer(enabled=False, burst=1, clock=clock, sleep=clock.sleep)
    for _ in range(50):
        assert pacer.acquire("chat") == 0.0
    assert clock.sleeps == []
