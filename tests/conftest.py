"""Shared fixtures: a virtual clock and a client factory wired to a stub transport."""

from __future__ import annotations

import pytest

from slack_api_gateway.client import SlackGatewayClient
from slack_api_gateway.config import SlackConfig
from slack_api_gateway.rate_limit import Pacer
from slack_api_gateway.retry import RetryPolicy

TEST_TOKEN = "xoxb-test-token-not-real"


class FakeClock:
    """Monotonic clock whose ``sleep`` only advances itself."""

    def __init__(self, start: float = 1_000.0) -> None:
        self.now = start
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds

    @property
    def total_slept(self) -> float:
        return sum(self.sleeps)


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def make_client(clock):
    """Build a client with deterministic time, pacing and backoff."""

    def factory(
        transport,
        *,
        max_attempts: int = 3,
        cache_ttl: float = 60.0,
        rate_limit: bool = True,
        base_url: str = "https://slack.test/api/",
        **kwargs,
    ) -> SlackGatewayClient:
        config = SlackConfig(base_url=base_url, cache_ttl=cache_ttl, rate_limit_enabled=rate_limit)
        return SlackGatewayClient(
            TEST_TOKEN,
            config=config,
            transport=transport,
            retry_policy=RetryPolicy(
                max_attempts=max_attempts,
                base_delay=0.5,
                max_delay=4.0,
                jitter=lambda low, high: high,
            ),
            pacer=Pacer(enabled=rate_limit, burst=100.0, clock=clock, sleep=clock.sleep),
            clock=clock,
            sleep=clock.sleep,
            **kwargs,
        )

    return factory
