"""Error-code mapping: Slack strings become typed exceptions."""

from __future__ import annotations

import pytest

from slack_api_gateway.errors import (
    AuthenticationError,
    AuthorizationError,
    NotFoundError,
    RateLimitError,
    SlackApiError,
    TransportError,
    ValidationError,
    error_from_payload,
    retry_after_seconds,
)


@pytest.mark.parametrize(
    "code, expected",
    [
        ("invalid_auth", AuthenticationError),
        ("token_revoked", AuthenticationError),
        ("missing_scope", AuthorizationError),
        ("not_in_channel", AuthorizationError),
        ("channel_not_found", NotFoundError),
        ("users_not_found", NotFoundError),
        ("msg_too_long", ValidationError),
        ("invalid_blocks", ValidationError),
        ("something_brand_new", SlackApiError),
    ],
)
def test_codes_map_to_specific_classes(code, expected) -> None:
    error = error_from_payload({"ok": False, "error": code})
    assert isinstance(error, expected)
    assert error.code == code
    assert str(error) == f"Slack API error: {code}"


def test_missing_scope_carries_the_needed_scope() -> None:
    error = error_from_payload(
        {"ok": False, "error": "missing_scope", "needed": "chat:write"}, status=200
    )
    assert isinstance(error, AuthorizationError)
    assert error.needed == "chat:write"


def test_warning_is_used_when_error_is_absent() -> None:
    error = error_from_payload({"ok": False, "warning": "deprecated_argument"})
    assert error.code == "deprecated_argument"


def test_hints_are_actionable() -> None:
    assert "SLACK_BOT_TOKEN" in error_from_payload({"error": "invalid_auth"}).hint()
    assert "OAuth scope" in error_from_payload({"error": "missing_scope"}).hint()
    assert "workspace-specific" in error_from_payload({"error": "channel_not_found"}).hint()
    assert "api.slack.com" in error_from_payload({"error": "weird"}).hint()


def test_transport_error_keeps_status() -> None:
    error = TransportError("boom", status=502)
    assert error.status == 502
    assert error.code == "transport_error"


def test_rate_limit_error_is_a_429_slack_error() -> None:
    error = RateLimitError("slow down", retry_after=3.5)
    assert error.status == 429
    assert error.code == "ratelimited"
    assert error.retry_after == 3.5
    assert isinstance(error, SlackApiError)


@pytest.mark.parametrize(
    "value, expected",
    [(None, 1.0), ("", 1.0), ("2", 2.0), ("0.25", 0.25), ("-4", 0.0), ("soon", 1.0)],
)
def test_retry_after_parsing(value, expected) -> None:
    assert retry_after_seconds(value) == expected
