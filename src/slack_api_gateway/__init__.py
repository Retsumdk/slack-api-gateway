"""slack-api-gateway: a typed, paced, retrying client for the Slack Web API.

Quick start::

    import os
    from slack_api_gateway import SlackGatewayClient

    with SlackGatewayClient(os.environ["SLACK_BOT_TOKEN"]) as slack:
        for channel in slack.list_conversations():
            print(channel.id, channel.name)

            slack.post_message(channel.id, f"Hello #{channel.name}")
"""

from __future__ import annotations

from ._version import __version__
from .cache import IdempotencyCache, TTLCache, stable_key
from .client import FAMILY_BY_METHOD, SlackGatewayClient, Stats
from .config import DEFAULT_BASE_URL, SlackConfig
from .errors import (
    AuthenticationError,
    AuthorizationError,
    ConfigError,
    NotFoundError,
    PaginationError,
    RateLimitError,
    SlackApiError,
    SlackGatewayError,
    TransportError,
    ValidationError,
    error_from_payload,
    retry_after_seconds,
)
from .models import AuthInfo, Channel, FileInfo, Message, Page, Reaction, User
from .pagination import paginate
from .rate_limit import DEFAULT_PACING, Pacer, TokenBucket
from .retry import RETRYABLE_CODES, RETRYABLE_STATUSES, Decision, RetryPolicy
from .transport import (
    CannedResponse,
    HttpRequest,
    HttpResponse,
    StubTransport,
    Transport,
    UrllibTransport,
    encode_multipart,
    form_encode,
    json_encode,
)

__all__ = [
    "DEFAULT_BASE_URL",
    "DEFAULT_PACING",
    "FAMILY_BY_METHOD",
    "RETRYABLE_CODES",
    "RETRYABLE_STATUSES",
    "AuthInfo",
    "AuthenticationError",
    "AuthorizationError",
    "CannedResponse",
    "Channel",
    "ConfigError",
    "Decision",
    "FileInfo",
    "HttpRequest",
    "HttpResponse",
    "IdempotencyCache",
    "Message",
    "NotFoundError",
    "Pacer",
    "Page",
    "PaginationError",
    "RateLimitError",
    "Reaction",
    "RetryPolicy",
    "SlackApiError",
    "SlackConfig",
    "SlackGatewayClient",
    "SlackGatewayError",
    "Stats",
    "StubTransport",
    "TTLCache",
    "TokenBucket",
    "Transport",
    "TransportError",
    "UrllibTransport",
    "User",
    "ValidationError",
    "__version__",
    "encode_multipart",
    "error_from_payload",
    "form_encode",
    "json_encode",
    "paginate",
    "retry_after_seconds",
    "stable_key",
]
