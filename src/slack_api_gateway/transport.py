"""HTTP transport: a tiny protocol plus a stdlib implementation.

The gateway never imports a third-party HTTP client. Everything it needs —
form encoding, JSON, multipart upload — is a few dozen lines of standard
library, which means ``pip install`` cannot fail and the test suite can drive
the client through :class:`StubTransport` with zero network access.

A transport's only job is to turn an :class:`HttpRequest` into an
:class:`HttpResponse`. It raises :class:`TransportError` for network-level
failures and returns *every* HTTP status verbatim, including 4xx/5xx, so the
client can decide what is retryable.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol

from .errors import TransportError


@dataclass(frozen=True, slots=True)
class HttpRequest:
    """A fully encoded outbound request."""

    method: str
    url: str
    headers: Mapping[str, str] = field(default_factory=dict)
    body: bytes = b""

    @property
    def content_type(self) -> str:
        return self.headers.get("Content-Type", "")


@dataclass(frozen=True, slots=True)
class HttpResponse:
    """Raw response, status preserved exactly as received."""

    status: int
    headers: Mapping[str, str] = field(default_factory=dict)
    body: bytes = b""

    def json(self) -> Any:
        """Parse the body as JSON, raising ``ValueError`` when it is not JSON."""
        try:
            return json.loads(self.body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError("response body is not valid JSON") from exc

    @property
    def text(self) -> str:
        return self.body.decode("utf-8", errors="replace")

    def header(self, name: str, default: str | None = None) -> str | None:
        return self.headers.get(name.lower(), default)


class Transport(Protocol):
    """Anything that can perform an :class:`HttpRequest`."""

    def send(self, request: HttpRequest) -> HttpResponse: ...


class UrllibTransport:
    """Real HTTP over :mod:`urllib`.

    TLS verification is not configurable. A gateway that can be told to skip
    certificate checks is a gateway that will one day be told to skip them, so
    the option simply does not exist.

    Args:
        timeout: Socket timeout in seconds (connect and read).
    """

    #: Refused before a socket is opened, so a stale or hostile base URL cannot
    #: turn the client into a ``file://`` reader.
    ALLOWED_SCHEMES = frozenset({"http", "https"})

    def __init__(self, *, timeout: float = 30.0) -> None:
        self.timeout = timeout

    def send(self, request: HttpRequest) -> HttpResponse:
        scheme = urllib.parse.urlsplit(request.url).scheme.lower()
        if scheme not in self.ALLOWED_SCHEMES:
            raise TransportError(
                f"refusing to send to a non-HTTP URL: {request.url!r}",
                code="invalid_url",
            )
        http_request = urllib.request.Request(  # noqa: S310 - scheme checked above
            request.url,
            data=request.body or None,
            headers=dict(request.headers),
            method=request.method.upper(),
        )
        try:
            # Scheme is checked above; urllib is the stdlib's only HTTP client.
            with urllib.request.urlopen(  # noqa: S310
                http_request, timeout=self.timeout
            ) as response:
                return HttpResponse(
                    status=int(response.status),
                    headers={k.lower(): v for k, v in response.headers.items()},
                    body=response.read(),
                )
        except urllib.error.HTTPError as exc:  # 4xx/5xx still carry a Slack JSON body
            body = exc.read() if hasattr(exc, "read") else b""
            headers = {k.lower(): v for k, v in (exc.headers or {}).items()}
            return HttpResponse(status=int(exc.code), headers=headers, body=body)
        except urllib.error.URLError as exc:
            reason = getattr(exc, "reason", exc)
            code = "timeout" if isinstance(reason, TimeoutError) else "connection_error"
            raise TransportError(
                f"transport failure for {request.url}: {reason}", code=code
            ) from exc
        except TimeoutError as exc:
            raise TransportError(
                f"timeout after {self.timeout}s: {request.url}", code="timeout"
            ) from exc


@dataclass
class CannedResponse:
    """One scripted response for :class:`StubTransport`.

    Exactly one of ``payload``, ``body`` or ``error`` is normally set:

    * ``payload`` — a dict serialised to JSON (the Slack shape).
    * ``body`` — raw bytes, for malformed/HTML responses.
    * ``error`` — a transport code such as ``"timeout"`` to raise instead.
    """

    status: int = 200
    payload: dict[str, Any] | None = None
    headers: dict[str, str] = field(default_factory=dict)
    body: bytes | None = None
    error: str | None = None

    def to_response(self) -> HttpResponse:
        if self.error:
            raise TransportError(f"scripted {self.error}", code=self.error)
        body = self.body
        if body is None:
            body = json.dumps(self.payload if self.payload is not None else {"ok": True}).encode()
        headers = {"content-type": "application/json; charset=utf-8"}
        headers.update({k.lower(): v for k, v in self.headers.items()})
        return HttpResponse(status=self.status, headers=headers, body=body)


class StubTransport:
    """Deterministic transport for tests and the offline demo.

    Responses can be queued globally (consumed in order) or per method, where
    the method is taken from the URL's last path segment. Every request is
    recorded in :attr:`requests` so tests can assert on what was actually sent.
    """

    def __init__(
        self,
        responses: Sequence[CannedResponse] | Mapping[str, Sequence[CannedResponse]] = (),
        *,
        default: CannedResponse | None = None,
    ) -> None:
        self.requests: list[HttpRequest] = []
        self._by_method: dict[str, list[CannedResponse]] = {}
        self._queue: list[CannedResponse] = []
        self._default = default
        if isinstance(responses, Mapping):
            for method, items in responses.items():
                self._by_method[method] = list(items)
        else:
            self._queue = list(responses)

    def queue(self, method: str | None, *responses: CannedResponse) -> None:
        """Append responses, either to a method's queue or the global queue."""
        target = self._queue if method is None else self._by_method.setdefault(method, [])
        target.extend(responses)

    def send(self, request: HttpRequest) -> HttpResponse:
        self.requests.append(request)
        method = request.url.rstrip("/").rsplit("/", 1)[-1]
        queue = self._by_method.get(method)
        if queue:
            return queue.pop(0).to_response()
        if self._queue:
            return self._queue.pop(0).to_response()
        if self._default is not None:
            return self._default.to_response()
        raise AssertionError(f"StubTransport has no scripted response for {method!r}")

    @property
    def methods_called(self) -> list[str]:
        return [request.url.rstrip("/").rsplit("/", 1)[-1] for request in self.requests]


# -- encoding helpers ------------------------------------------------------


def form_encode(params: Mapping[str, Any]) -> bytes:
    """Encode Slack parameters as ``application/x-www-form-urlencoded``.

    Slack accepts JSON-encoded strings for structured values, so lists and
    dicts are serialised with :func:`json.dumps`; booleans become the literal
    ``true``/``false`` Slack expects; ``None`` is dropped rather than sent as
    an empty string, which Slack would read as an argument.
    """
    pairs: list[tuple[str, str]] = []
    for name, value in params.items():
        if value is None:
            continue
        if isinstance(value, bool):
            pairs.append((name, "true" if value else "false"))
        elif isinstance(value, (list, tuple, dict)):
            pairs.append((name, json.dumps(value, separators=(",", ":"))))
        else:
            pairs.append((name, str(value)))
    return urllib.parse.urlencode(pairs).encode("utf-8")


def json_encode(params: Mapping[str, Any]) -> bytes:
    """Encode parameters as a JSON object body."""
    return json.dumps({k: v for k, v in params.items() if v is not None}).encode("utf-8")


def encode_multipart(
    fields: Mapping[str, str],
    *,
    file_field: str,
    filename: str,
    content: bytes,
    content_type: str = "application/octet-stream",
) -> tuple[bytes, str]:
    """Build a ``multipart/form-data`` body containing one file part.

    Returns the body and the matching ``Content-Type`` header value.
    """
    boundary = f"----slackgateway{uuid.uuid4().hex}"
    chunks: list[bytes] = []
    for name, value in fields.items():
        chunks.append(f"--{boundary}\r\n".encode())
        chunks.append(f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode())
        chunks.append(f"{value}\r\n".encode())
    chunks.append(f"--{boundary}\r\n".encode())
    chunks.append(
        f'Content-Disposition: form-data; name="{file_field}"; filename="{filename}"\r\n'.encode()
    )
    chunks.append(f"Content-Type: {content_type}\r\n\r\n".encode())
    chunks.append(content)
    chunks.append(b"\r\n")
    chunks.append(f"--{boundary}--\r\n".encode())
    return b"".join(chunks), f"multipart/form-data; boundary={boundary}"


def payload_ok(**fields: Any) -> CannedResponse:
    """A canned ``{"ok": true, ...}`` response."""
    return CannedResponse(payload={"ok": True, **fields})


def payload_error(code: str, status: int = 200, **extra: Any) -> CannedResponse:
    """A canned Slack error response (``ok: false``)."""
    return CannedResponse(status=status, payload={"ok": False, "error": code, **extra})


__all__ = [
    "CannedResponse",
    "HttpRequest",
    "HttpResponse",
    "StubTransport",
    "Transport",
    "UrllibTransport",
    "encode_multipart",
    "form_encode",
    "json_encode",
    "payload_error",
    "payload_ok",
]
