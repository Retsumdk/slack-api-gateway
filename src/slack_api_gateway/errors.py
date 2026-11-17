"""Exception hierarchy and Slack error-code mapping.

Slack signals failure in two very different ways:

* HTTP-level (401, 429, 500) — transport concerns.
* Envelope-level (HTTP 200 with ``{"ok": false, "error": "channel_not_found"}``)
  — the API's actual answer, and by far the more common case.

Both funnel into :func:`error_from_payload`, so callers write
``except NotFoundError`` instead of string-matching ``"channel_not_found"``.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

# Slack error codes grouped by the exception they should raise.
AUTH_CODES: frozenset[str] = frozenset(
    {
        "invalid_auth",
        "not_authed",
        "account_inactive",
        "token_revoked",
        "token_expired",
        "org_login_required",
        "two_factor_setup_required",
        "sso_required",
        "ekm_access_denied",
    }
)

PERMISSION_CODES: frozenset[str] = frozenset(
    {
        "missing_scope",
        "no_permission",
        "not_allowed_token_type",
        "restricted_action",
        "restricted_action_read_only_channel",
        "restricted_action_thread_only_channel",
        "not_in_channel",
        "channel_is_archived",
        "cannot_reply_to_message",
        "cant_update_message",
        "cant_delete_message",
    }
)

NOT_FOUND_CODES: frozenset[str] = frozenset(
    {
        "channel_not_found",
        "user_not_found",
        "users_not_found",
        "message_not_found",
        "file_not_found",
        "team_not_found",
        "thread_not_found",
        "no_file_data",
        "file_deleted",
    }
)

VALIDATION_CODES: frozenset[str] = frozenset(
    {
        "invalid_arguments",
        "invalid_arg_name",
        "invalid_array_arg",
        "invalid_charset",
        "invalid_form_data",
        "invalid_json",
        "invalid_post_type",
        "missing_post_type",
        "invalid_blocks",
        "invalid_blocks_format",
        "invalid_name",
        "invalid_channel",
        "invalid_cursor",
        "no_text",
        "too_long",
        "too_many_attachments",
        "duplicate_message",
        "msg_too_long",
        "name_taken",
    }
)


class SlackGatewayError(Exception):
    """Base class for every error this package raises."""

    def __init__(self, message: str, *, code: str = "") -> None:
        super().__init__(message)
        self.message = message
        self.code = code

    def __str__(self) -> str:  # pragma: no cover - trivial
        return self.message


class ConfigError(SlackGatewayError):
    """Invalid or contradictory client configuration."""


class TransportError(SlackGatewayError):
    """Network-level failure: DNS, TLS, connection reset, timeout.

    ``status`` is set when an HTTP response arrived but was unusable
    (non-JSON body, or a 5xx that survived retries).
    """

    def __init__(self, message: str, *, code: str = "transport_error", status: int = 0) -> None:
        super().__init__(message, code=code)
        self.status = status


class SlackApiError(SlackGatewayError):
    """Slack answered ``{"ok": false}``."""

    def __init__(
        self,
        message: str,
        *,
        code: str,
        status: int = 200,
        payload: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__(message, code=code)
        self.status = status
        self.payload: dict[str, Any] = dict(payload or {})

    def hint(self) -> str:
        """One-line, actionable next step for this error code."""
        if self.code in AUTH_CODES:
            return "Check that SLACK_BOT_TOKEN is current and belongs to this workspace."
        if self.code == "missing_scope":
            return "Add the required OAuth scope to the app and reinstall it."
        if self.code in NOT_FOUND_CODES:
            return "Verify the ID: channel/user/message IDs are workspace-specific."
        return "See https://api.slack.com/methods for the method's error list."


class AuthenticationError(SlackApiError):
    """Token missing, revoked, or otherwise rejected."""


class AuthorizationError(SlackApiError):
    """Token is valid but lacks the scope or channel membership required."""

    def __init__(
        self,
        message: str,
        *,
        code: str,
        status: int = 200,
        payload: Mapping[str, Any] | None = None,
        needed: str | None = None,
    ) -> None:
        super().__init__(message, code=code, status=status, payload=payload)
        self.needed = needed or str((payload or {}).get("needed") or "")


class NotFoundError(SlackApiError):
    """The thing you asked for does not exist in this workspace."""


class ValidationError(SlackApiError):
    """The request was malformed. Raised before sending, or by Slack."""


class RateLimitError(SlackApiError):
    """HTTP 429 with the server's ``Retry-After``."""

    def __init__(
        self, message: str, *, retry_after: float = 1.0, payload: Mapping[str, Any] | None = None
    ) -> None:
        super().__init__(message, code="ratelimited", status=429, payload=payload)
        self.retry_after = retry_after


class PaginationError(SlackGatewayError):
    """A cursor repeated or the page cap was hit before the list ended."""


def error_from_payload(payload: Mapping[str, Any], *, status: int = 200) -> SlackApiError:
    """Map a Slack failure envelope to the most specific exception class."""
    code = str(payload.get("error") or payload.get("warning") or "unknown_error")
    message = f"Slack API error: {code}"
    needed = payload.get("needed")
    if code in AUTH_CODES:
        return AuthenticationError(message, code=code, status=status, payload=payload)
    if code in PERMISSION_CODES:
        return AuthorizationError(
            message,
            code=code,
            status=status,
            payload=payload,
            needed=needed if isinstance(needed, str) else None,
        )
    if code in NOT_FOUND_CODES:
        return NotFoundError(message, code=code, status=status, payload=payload)
    if code in VALIDATION_CODES:
        return ValidationError(message, code=code, status=status, payload=payload)
    return SlackApiError(message, code=code, status=status, payload=payload)


def retry_after_seconds(value: str | None, default: float = 1.0) -> float:
    """Parse a ``Retry-After`` header, tolerating junk and negative values."""
    if not value:
        return default
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        return default
    return max(0.0, seconds)


__all__ = [
    "AUTH_CODES",
    "NOT_FOUND_CODES",
    "PERMISSION_CODES",
    "VALIDATION_CODES",
    "AuthenticationError",
    "AuthorizationError",
    "ConfigError",
    "NotFoundError",
    "PaginationError",
    "RateLimitError",
    "SlackApiError",
    "SlackGatewayError",
    "TransportError",
    "ValidationError",
    "error_from_payload",
    "retry_after_seconds",
]
