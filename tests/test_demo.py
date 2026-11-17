"""The offline demo is a contract: deterministic, network-free, byte-stable.

``demo-output.txt`` is checked in and compared character for character, so any
behaviour change in pacing, retries, pagination or error mapping shows up as a
reviewable diff instead of a surprise in production.
"""

from __future__ import annotations

from pathlib import Path

from slack_api_gateway.demo import run_demo

GOLDEN = Path(__file__).resolve().parents[1] / "demo-output.txt"


def test_demo_covers_every_documented_failure_mode() -> None:
    transcript = run_demo()

    for step in (
        "auth.test",
        "conversations.list",
        "conversations.info",
        "chat.postMessage",
        "identical repost",
        "NotFoundError[channel_not_found]",
        "NotFoundError[users_not_found]",
        "upload 3-step flow",
        "PaginationError[pagination_loop]",
    ):
        assert step in transcript
    assert transcript.rstrip().endswith("RESULT: PASS")


def test_demo_is_deterministic() -> None:
    assert run_demo() == run_demo()


def test_demo_matches_checked_in_transcript() -> None:
    assert GOLDEN.exists(), f"missing golden transcript: {GOLDEN}"
    assert run_demo() == GOLDEN.read_text(encoding="utf-8")


def test_demo_reports_real_counters() -> None:
    transcript = run_demo()

    assert '"rate_limited": 1' in transcript
    assert '"idempotent_replays": 1' in transcript
    assert '"retries": 1' in transcript
    assert '"transport_errors": 0' in transcript
