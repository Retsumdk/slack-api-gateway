"""The gateway client: one place where pacing, retries and errors compose.

Design notes that matter to a reader of this file:

* **One chokepoint.** Every call — typed helpers and raw ``call()`` alike — goes
  through :meth:`SlackGatewayClient.call`, so pacing, retry and error mapping
  cannot be bypassed.
* **The server is the authority.** Local token buckets are a courtesy that keeps
  a busy loop from hammering Slack; a ``429`` and its ``Retry-After`` always
  win, even when they contradict the local budget.
* **Answers are not hiccups.** ``channel_not_found`` is a result. It is never
  retried, only raised, as a typed exception.
* **Everything time-related is injected.** A test can advance a fake clock and
  assert the exact sleeps without waiting for them.
"""

from __future__ import annotations

import os
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from ._version import __version__
from .cache import IdempotencyCache, TTLCache, stable_key
from .config import SlackConfig
from .errors import (
    AuthenticationError,
    AuthorizationError,
    RateLimitError,
    SlackApiError,
    TransportError,
    ValidationError,
    error_from_payload,
    retry_after_seconds,
)
from .models import AuthInfo, Channel, FileInfo, Message, User
from .pagination import paginate
from .rate_limit import Pacer
from .retry import Decision, RetryPolicy
from .transport import (
    HttpRequest,
    HttpResponse,
    Transport,
    UrllibTransport,
    encode_multipart,
    form_encode,
)

#: Which pacing family a method belongs to. Anything unlisted is ``other``.
FAMILY_BY_METHOD: dict[str, str] = {
    "auth.test": "auth",
    "auth.revoke": "auth",
    "chat.postMessage": "chat",
    "chat.update": "chat",
    "chat.delete": "chat",
    "chat.postEphemeral": "chat",
    "chat.scheduleMessage": "chat",
    "reactions.add": "reactions",
    "reactions.remove": "reactions",
    "reactions.get": "reactions",
    "conversations.list": "conversations.list",
    "conversations.open": "conversations.list",
    "conversations.info": "conversations.read",
    "conversations.history": "conversations.read",
    "conversations.replies": "conversations.read",
    "conversations.members": "conversations.read",
    "users.list": "users",
    "users.info": "users",
    "users.lookupByEmail": "users",
    "files.getUploadURLExternal": "files",
    "files.completeUploadExternal": "files",
    "files.info": "files",
    "files.list": "files",
}


def family_for(method: str) -> str:
    """Return the pacing family for ``method``."""
    return FAMILY_BY_METHOD.get(method, "other")


@dataclass
class Stats:
    """Counters for one client instance. Cheap, and invaluable in an incident."""

    calls: int = 0
    retries: int = 0
    rate_limited: int = 0
    transport_errors: int = 0
    cache_hits: int = 0
    idempotent_replays: int = 0
    paced_seconds: float = 0.0

    def snapshot(self) -> dict[str, Any]:
        data = asdict(self)
        data["paced_seconds"] = round(self.paced_seconds, 3)
        return data


def _maybe_json(response: HttpResponse) -> dict[str, Any] | None:
    """Parse a body as a JSON object, or return ``None``."""
    try:
        payload = response.json()
    except ValueError:
        return None
    return dict(payload) if isinstance(payload, Mapping) else None


def _token_from_env(env: Mapping[str, str] | None = None) -> str | None:
    source = os.environ if env is None else env
    for name in ("SLACK_BOT_TOKEN", "SLACK_TOKEN", "SLACK_USER_TOKEN"):
        value = source.get(name)
        if value:
            return value.strip()
    return None


class SlackGatewayClient:
    """Typed Slack Web API client with pacing, retries and typed errors.

    Args:
        token: Bot/user token. Falls back to ``SLACK_BOT_TOKEN``,
            ``SLACK_TOKEN``, then ``SLACK_USER_TOKEN``.
        config: Settings object; ``None`` means "load from env".
        transport: Any :class:`~slack_api_gateway.transport.Transport`.
            Defaults to real HTTP over :mod:`urllib`.
        retry_policy: Overrides the policy derived from ``config``.
        pacer: Overrides client-side pacing.
        clock: Monotonic clock used by the cache and pacer.
        sleep: Blocking sleep used for backoff; inject in tests.

    Raises:
        ConfigError: If ``config`` is inconsistent.
    """

    def __init__(
        self,
        token: str | None = None,
        *,
        config: SlackConfig | None = None,
        transport: Transport | None = None,
        retry_policy: RetryPolicy | None = None,
        pacer: Pacer | None = None,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        cache: TTLCache[dict[str, Any]] | None = None,
        idempotency: IdempotencyCache | None = None,
    ) -> None:
        self._config = (config or SlackConfig.from_env()).with_token(
            token if token is not None else (config.token if config else None) or _token_from_env()
        )
        self._config.validate()
        self._clock = clock
        self._sleep = sleep
        self._transport: Transport = transport or UrllibTransport(timeout=self._config.timeout)
        self._retry = retry_policy or RetryPolicy(
            max_attempts=self._config.max_attempts,
            base_delay=self._config.retry_base_delay,
            max_delay=self._config.retry_max_delay,
        )
        self._pacer = pacer or Pacer(
            enabled=self._config.rate_limit_enabled, clock=clock, sleep=sleep
        )
        if cache is not None:
            self._cache: TTLCache[dict[str, Any]] | None = cache
        elif self._config.cache_ttl > 0:
            self._cache = TTLCache(self._config.cache_ttl, clock=clock)
        else:
            self._cache = None
        self._idempotency = idempotency or IdempotencyCache(clock=clock)
        self.stats = Stats()
        self._closed = False

    # -- lifecycle ---------------------------------------------------------

    def __enter__(self) -> SlackGatewayClient:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def close(self) -> None:
        """Release resources. Safe to call more than once."""
        self._closed = True

    @property
    def config(self) -> SlackConfig:
        return self._config

    @property
    def retry_policy(self) -> RetryPolicy:
        return self._retry

    def stats_snapshot(self) -> dict[str, Any]:
        """Counters plus cache effectiveness, ready for JSON."""
        data = self.stats.snapshot()
        data["cache"] = self._cache.stats() if self._cache is not None else None
        return data

    # -- transport plumbing ------------------------------------------------

    def _url(self, method: str) -> str:
        return f"{self._config.base_url.rstrip('/')}/{method}"

    def _headers(
        self, *, content_type: str = "application/x-www-form-urlencoded"
    ) -> dict[str, str]:
        token = self._config.token
        if not token:
            raise AuthenticationError(
                "no Slack token configured: pass token=, or set SLACK_BOT_TOKEN",
                code="not_authed",
            )
        return {
            "Authorization": f"Bearer {token}",
            "Content-Type": content_type,
            "User-Agent": f"slack-api-gateway/{__version__}",
            "Accept": "application/json",
        }

    def _wait(self, decision: Decision) -> None:
        self.stats.retries += 1
        if decision.delay > 0:
            self._sleep(decision.delay)

    # -- the single call chokepoint ---------------------------------------

    def call(
        self,
        method: str,
        params: Mapping[str, Any] | None = None,
        *,
        cacheable: bool = False,
    ) -> dict[str, Any]:
        """Call any Web API method and return the parsed success payload.

        Args:
            method: e.g. ``"chat.postMessage"``.
            params: Method parameters; ``None`` values are dropped.
            cacheable: Serve and store this response in the TTL cache.

        Raises:
            AuthenticationError: Token missing, revoked, or rejected (401).
            AuthorizationError: Valid token, missing scope/membership.
            NotFoundError: Slack said the entity does not exist.
            ValidationError: Slack or the client rejected the arguments.
            RateLimitError: Still 429 after every retry.
            TransportError: Network failure, or an unusable HTTP response.
        """
        clean: dict[str, Any] = dict(params or {})
        key = stable_key(method, clean)
        if cacheable and self._cache is not None:
            cached = self._cache.get(key)
            if cached is not None:
                self.stats.cache_hits += 1
                return dict(cached)

        payload = self._perform(method, clean)

        if cacheable and self._cache is not None:
            self._cache.set(key, dict(payload))
        return payload

    def _perform(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        family = family_for(method)
        url = self._url(method)
        body = form_encode(params)
        attempt = 0

        while True:
            attempt += 1
            self.stats.paced_seconds += self._pacer.acquire(family)
            request = HttpRequest(method="POST", url=url, headers=self._headers(), body=body)

            try:
                response = self._transport.send(request)
            except TransportError as exc:
                self.stats.transport_errors += 1
                decision = self._retry.decide(attempt=attempt, transport_error=True)
                if not decision.retry:
                    raise TransportError(
                        f"{method}: {exc.message} (gave up after {attempt} attempt(s))",
                        code=exc.code,
                        status=exc.status,
                    ) from exc
                self._wait(decision)
                continue

            self.stats.calls += 1

            if response.status == 429:
                self.stats.rate_limited += 1
                wait = retry_after_seconds(response.header("retry-after"), default=1.0)
                decision = self._retry.decide(attempt=attempt, status=429, retry_after=wait)
                if not decision.retry:
                    raise RateLimitError(
                        f"{method}: rate limited by Slack, retry after {wait:g}s",
                        retry_after=wait,
                        payload=_maybe_json(response),
                    )
                self._wait(decision)
                continue

            if response.status in (401, 403):
                rejected = _maybe_json(response) or {}
                error = error_from_payload(rejected, status=response.status)
                if isinstance(error, (AuthenticationError, AuthorizationError)):
                    raise error
                raise AuthenticationError(
                    f"{method}: Slack rejected the token (HTTP {response.status})",
                    code="invalid_auth",
                    status=response.status,
                    payload=rejected,
                )

            if response.status >= 500:
                decision = self._retry.decide(attempt=attempt, status=response.status)
                if not decision.retry:
                    raise TransportError(
                        f"{method}: Slack returned HTTP {response.status} "
                        f"after {attempt} attempt(s)",
                        status=response.status,
                    )
                self._wait(decision)
                continue

            payload = _maybe_json(response)
            if payload is None:
                if 200 <= response.status < 300:
                    raise TransportError(
                        f"{method}: expected a JSON object, got {response.text[:200]!r}",
                        status=response.status,
                    )
                raise TransportError(
                    f"{method}: HTTP {response.status}: {response.text[:200]}",
                    status=response.status,
                )

            if not payload.get("ok", False):
                error = error_from_payload(payload, status=response.status)
                raw_retry_after = payload.get("retry_after")
                retry_after = (
                    float(raw_retry_after) if isinstance(raw_retry_after, (int, float)) else None
                )
                decision = self._retry.decide(
                    attempt=attempt,
                    status=response.status,
                    code=error.code,
                    retry_after=retry_after,
                )
                if not decision.retry:
                    raise error
                self._wait(decision)
                continue

            return dict(payload)

    # -- identity ----------------------------------------------------------

    def auth_test(self) -> AuthInfo:
        """Verify the token and identify the caller (``auth.test``)."""
        return AuthInfo.from_dict(self.call("auth.test"))

    # -- conversations -----------------------------------------------------

    def get_channel(self, channel_id: str) -> Channel:
        """Fetch one conversation (cached for ``cache_ttl`` seconds)."""
        self._require(channel_id, "channel")
        payload = self.call("conversations.info", {"channel": channel_id}, cacheable=True)
        raw = payload.get("channel")
        return Channel.from_dict(raw if isinstance(raw, Mapping) else payload)

    def list_conversations(
        self,
        *,
        types: str = "public_channel",
        exclude_archived: bool = True,
        limit: int = 200,
    ) -> list[Channel]:
        """Every conversation matching ``types``, following cursors to the end.

        Raises:
            PaginationError: A cursor repeated, or the page cap was hit.
        """
        raw = paginate(
            lambda page: self.call(
                "conversations.list",
                {
                    "types": types,
                    "exclude_archived": exclude_archived,
                    **page,
                },
            ),
            page_size=min(limit, 200),
            max_pages=self._config.max_pages,
        )
        return [Channel.from_dict(item) for item in raw]

    def history(
        self,
        channel: str,
        *,
        limit: int = 50,
        oldest: str | None = None,
        latest: str | None = None,
        inclusive: bool = False,
    ) -> list[Message]:
        """Read a conversation's messages, newest first.

        Slack returns newest-first and caps a page at 200; ``limit`` is the
        total row budget, so a ``limit`` of 50 issues at most one page.
        """
        self._require(channel, "channel")
        if limit < 1:
            raise ValidationError("limit must be at least 1", code="invalid_arguments")
        page_size = min(limit, 200)
        raw = paginate(
            lambda page: self.call(
                "conversations.history",
                {
                    "channel": channel,
                    "oldest": oldest,
                    "latest": latest,
                    "inclusive": inclusive,
                    **page,
                },
            ),
            page_size=page_size,
            max_pages=min(self._config.max_pages, max(1, (limit + page_size - 1) // page_size)),
        )
        return [Message.from_dict(item) for item in raw[:limit]]

    def replies(self, channel: str, thread_ts: str, *, limit: int = 100) -> list[Message]:
        """Every message in a thread, parent first."""
        self._require(channel, "channel")
        self._require(thread_ts, "thread_ts")
        raw = paginate(
            lambda page: self.call(
                "conversations.replies", {"channel": channel, "ts": thread_ts, **page}
            ),
            page_size=min(limit, 200),
            max_pages=self._config.max_pages,
        )
        return [Message.from_dict(item) for item in raw[:limit]]

    # -- messages ----------------------------------------------------------

    def post_message(
        self,
        channel: str,
        text: str | None = None,
        *,
        blocks: Sequence[Mapping[str, Any]] | None = None,
        thread_ts: str | None = None,
        reply_broadcast: bool | None = None,
        unfurl_links: bool | None = None,
        idempotency_key: str | None = None,
    ) -> Message:
        """Post a message, optionally in a thread.

        Pass ``idempotency_key`` to make the write replay-safe: if a request
        with the same key already succeeded inside the ledger TTL, the stored
        response is returned and Slack is not called again. Slack has no
        idempotency parameter of its own, so this is the local equivalent.
        """
        self._require(channel, "channel")
        if not text and not blocks:
            raise ValidationError("a message needs text or blocks", code="no_text")
        if blocks is not None and (
            not isinstance(blocks, Sequence) or not all(isinstance(b, Mapping) for b in blocks)
        ):
            raise ValidationError("blocks must be a list of JSON objects", code="invalid_blocks")

        if idempotency_key:
            replay = self._idempotency.replay(stable_key("chat.postMessage", idempotency_key))
            if replay is not None:
                self.stats.idempotent_replays += 1
                return Message.from_dict(replay)

        payload = self.call(
            "chat.postMessage",
            {
                "channel": channel,
                "text": text,
                "blocks": list(blocks) if blocks else None,
                "thread_ts": thread_ts,
                "reply_broadcast": reply_broadcast,
                "unfurl_links": unfurl_links,
            },
        )
        if idempotency_key:
            self._idempotency.remember(
                stable_key("chat.postMessage", idempotency_key), dict(payload)
            )
        return Message.from_dict(payload)

    def update_message(
        self,
        channel: str,
        ts: str,
        text: str | None = None,
        *,
        blocks: Sequence[Mapping[str, Any]] | None = None,
    ) -> Message:
        """Replace a message's text and/or blocks."""
        self._require(channel, "channel")
        self._require(ts, "ts")
        if not text and not blocks:
            raise ValidationError("an update needs text or blocks", code="no_text")
        payload = self.call(
            "chat.update",
            {
                "channel": channel,
                "ts": ts,
                "text": text,
                "blocks": list(blocks) if blocks else None,
            },
        )
        return Message.from_dict(payload)

    def delete_message(self, channel: str, ts: str) -> str:
        """Delete a message; returns the timestamp Slack reports."""
        self._require(channel, "channel")
        self._require(ts, "ts")
        payload = self.call("chat.delete", {"channel": channel, "ts": ts})
        return str(payload.get("ts") or ts)

    # -- reactions ---------------------------------------------------------

    def add_reaction(self, channel: str, timestamp: str, name: str) -> None:
        """React to a message. ``name`` accepts ``thumbsup`` or ``:thumbsup:``."""
        self._require(channel, "channel")
        self._require(timestamp, "timestamp")
        emoji = name.strip().strip(":")
        if not emoji:
            raise ValidationError("reaction name is empty", code="invalid_name")
        self.call("reactions.add", {"channel": channel, "timestamp": timestamp, "name": emoji})

    def remove_reaction(self, channel: str, timestamp: str, name: str) -> None:
        """Remove one of *this app's* reactions."""
        self._require(channel, "channel")
        self._require(timestamp, "timestamp")
        emoji = name.strip().strip(":")
        if not emoji:
            raise ValidationError("reaction name is empty", code="invalid_name")
        self.call("reactions.remove", {"channel": channel, "timestamp": timestamp, "name": emoji})

    # -- users -------------------------------------------------------------

    def get_user(self, user_id: str) -> User:
        """Fetch one member (cached for ``cache_ttl`` seconds)."""
        self._require(user_id, "user")
        payload = self.call("users.info", {"user": user_id}, cacheable=True)
        return User.from_dict(payload)

    def find_user_by_email(self, email: str) -> User:
        """Resolve an email address to a member.

        Raises:
            NotFoundError: Slack answers ``users_not_found`` for guests and
                deactivated accounts as well as unknown addresses.
        """
        self._require(email, "email")
        if "@" not in email:
            raise ValidationError(f"not an email address: {email!r}", code="invalid_arguments")
        payload = self.call("users.lookupByEmail", {"email": email}, cacheable=True)
        return User.from_dict(payload)

    def list_users(self, *, limit: int = 200, include_deleted: bool = False) -> list[User]:
        """Every workspace member, following cursors."""
        raw = paginate(
            lambda page: self.call("users.list", dict(page)),
            page_size=min(limit, 200),
            max_pages=self._config.max_pages,
        )
        users = [User.from_dict(item) for item in raw]
        if include_deleted:
            return users
        return [user for user in users if not user.deleted]

    # -- files -------------------------------------------------------------

    def upload_file(
        self,
        *,
        path: str | None = None,
        content: bytes | str | None = None,
        filename: str | None = None,
        channel: str | None = None,
        title: str | None = None,
        initial_comment: str | None = None,
        mimetype: str = "application/octet-stream",
    ) -> FileInfo:
        """Upload a file with Slack's three-step external upload flow.

        ``files.upload`` is deprecated; the supported path is
        ``files.getUploadURLExternal`` → POST the bytes to the presigned URL →
        ``files.completeUploadExternal``. Doing it this way keeps large files
        off the Web API host and out of the request-signing path.
        """
        if (path is None) == (content is None):
            raise ValidationError("pass exactly one of path= or content=", code="no_file_data")
        if path is not None:
            source = Path(path)
            try:
                data = source.read_bytes()
            except OSError as exc:
                raise ValidationError(f"cannot read {path}: {exc}", code="no_file_data") from exc
            name = filename or source.name or "upload.bin"
        else:
            if isinstance(content, str):
                data = content.encode("utf-8")
            elif isinstance(content, (bytes, bytearray)):
                data = bytes(content)
            else:
                raise ValidationError("content must be str or bytes", code="no_file_data")
            name = filename or ""
            if not name:
                raise ValidationError(
                    "filename is required with content=", code="invalid_arguments"
                )
        if not data:
            raise ValidationError("refusing to upload an empty file", code="no_file_data")

        slot = self.call("files.getUploadURLExternal", {"filename": name, "length": len(data)})
        upload_url = str(slot.get("upload_url") or "")
        file_id = str(slot.get("file_id") or "")
        if not upload_url or not file_id:
            raise SlackApiError(
                "files.getUploadURLExternal returned no upload_url/file_id",
                code="invalid_response",
                payload=slot,
            )

        body, content_type = encode_multipart(
            {}, file_field="file", filename=name, content=data, content_type=mimetype
        )
        self._post_bytes(upload_url, body, content_type, method="upload")

        complete = self.call(
            "files.completeUploadExternal",
            {
                "files": [{"id": file_id, "title": title or name}],
                "channel_id": channel,
                "initial_comment": initial_comment,
            },
        )
        files = complete.get("files")
        if isinstance(files, Sequence) and files and isinstance(files[0], Mapping):
            return FileInfo.from_dict(files[0])
        return FileInfo(id=file_id, name=name, title=title or name, size=len(data))

    def _post_bytes(self, url: str, body: bytes, content_type: str, *, method: str) -> HttpResponse:
        """POST raw bytes to a presigned URL, retrying transient failures."""
        attempt = 0
        while True:
            attempt += 1
            self.stats.paced_seconds += self._pacer.acquire(
                family_for("files.getUploadURLExternal")
            )
            request = HttpRequest(
                method="POST",
                url=url,
                headers={
                    "Content-Type": content_type,
                    "User-Agent": f"slack-api-gateway/{__version__}",
                },
                body=body,
            )
            try:
                response = self._transport.send(request)
            except TransportError as exc:
                self.stats.transport_errors += 1
                decision = self._retry.decide(attempt=attempt, transport_error=True)
                if not decision.retry:
                    raise TransportError(
                        f"{method}: {exc.message} after {attempt} attempt(s)", code=exc.code
                    ) from exc
                self._wait(decision)
                continue
            self.stats.calls += 1
            if response.status >= 500:
                decision = self._retry.decide(attempt=attempt, status=response.status)
                if not decision.retry:
                    raise TransportError(
                        f"{method}: presigned upload failed with HTTP {response.status}",
                        status=response.status,
                    )
                self._wait(decision)
                continue
            if response.status >= 400:
                raise TransportError(
                    f"{method}: presigned upload rejected with HTTP {response.status}: "
                    f"{response.text[:200]}",
                    status=response.status,
                )
            return response

    # -- helpers -----------------------------------------------------------

    @staticmethod
    def _require(value: str | None, field_name: str) -> None:
        if value is None or not str(value).strip():
            raise ValidationError(f"{field_name} is required", code="invalid_arguments")

    def raw(self, method: str, params: Mapping[str, Any] | None = None) -> dict[str, Any]:
        """Alias for :meth:`call`, spelled for readability at call sites."""
        return self.call(method, params)


__all__ = ["FAMILY_BY_METHOD", "SlackGatewayClient", "Stats", "family_for"]
