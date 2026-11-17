"""Request encoding, response parsing, and the real urllib transport."""

from __future__ import annotations

import io
import json
import urllib.error
import urllib.parse
import urllib.request

import pytest

from slack_api_gateway.errors import TransportError
from slack_api_gateway.transport import (
    CannedResponse,
    HttpRequest,
    HttpResponse,
    StubTransport,
    UrllibTransport,
    encode_multipart,
    form_encode,
    json_encode,
    payload_error,
    payload_ok,
)


def parse(body: bytes) -> dict[str, list[str]]:
    return urllib.parse.parse_qs(body.decode())


def test_form_encode_drops_none_and_types_values() -> None:
    body = form_encode(
        {
            "channel": "C1",
            "text": "hi there",
            "reply_broadcast": True,
            "unfurl_links": False,
            "limit": 5,
            "blocks": [{"type": "section"}],
            "thread_ts": None,
        }
    )
    decoded = parse(body)
    assert decoded["channel"] == ["C1"]
    assert decoded["text"] == ["hi there"]
    assert decoded["reply_broadcast"] == ["true"]
    assert decoded["unfurl_links"] == ["false"]
    assert decoded["limit"] == ["5"]
    assert json.loads(decoded["blocks"][0]) == [{"type": "section"}]
    assert "thread_ts" not in decoded


def test_form_encode_escapes_delimiters() -> None:
    body = form_encode({"text": "a&b=c d"})
    assert parse(body)["text"] == ["a&b=c d"]


def test_json_encode_drops_none() -> None:
    assert json.loads(json_encode({"a": 1, "b": None})) == {"a": 1}


def test_multipart_body_is_well_formed() -> None:
    body, content_type = encode_multipart(
        {"title": "log"},
        file_field="file",
        filename="deploy log.txt",
        content=b"hello",
        content_type="text/plain",
    )
    boundary = content_type.split("boundary=", 1)[1]
    assert content_type.startswith("multipart/form-data; boundary=")
    assert body.startswith(f"--{boundary}".encode())
    assert body.endswith(f"--{boundary}--\r\n".encode())
    assert b'name="title"' in body and b"log" in body
    assert b'filename="deploy log.txt"' in body
    assert b"Content-Type: text/plain" in body
    assert body.count(b"hello") == 1


def test_response_json_raises_on_non_json() -> None:
    with pytest.raises(ValueError, match="not valid JSON"):
        HttpResponse(status=200, body=b"<html></html>").json()


def test_response_helpers() -> None:
    response = HttpResponse(status=429, headers={"retry-after": "3"}, body=b"{}")
    assert response.header("Retry-After") == "3"
    assert response.header("missing", "fallback") == "fallback"
    assert response.text == "{}"


def test_canned_response_defaults_to_ok() -> None:
    assert CannedResponse().to_response().json() == {"ok": True}


def test_canned_response_can_script_a_transport_failure() -> None:
    with pytest.raises(TransportError, match="scripted timeout"):
        CannedResponse(error="timeout").to_response()


def test_stub_transport_routes_by_method_and_records() -> None:
    transport = StubTransport({"auth.test": [payload_ok(team="Acme")]})
    response = transport.send(HttpRequest(method="POST", url="https://slack.com/api/auth.test"))
    assert response.json() == {"ok": True, "team": "Acme"}
    assert transport.methods_called == ["auth.test"]


def test_stub_transport_falls_back_to_global_queue_and_default() -> None:
    queued = StubTransport([payload_ok(step=1)])
    url = "https://slack.com/api/chat.postMessage"
    assert queued.send(HttpRequest(method="POST", url=url)).json()["step"] == 1
    defaulted = StubTransport(default=payload_ok(step="default"))
    assert defaulted.send(HttpRequest(method="POST", url=url)).json()["step"] == "default"


def test_stub_transport_refuses_unscripted_methods() -> None:
    with pytest.raises(AssertionError, match="no scripted response"):
        StubTransport().send(HttpRequest(method="POST", url="https://slack.com/api/x.y"))


def test_payload_helpers_shape_slack_envelopes() -> None:
    assert payload_ok(text="hi").payload == {"ok": True, "text": "hi"}
    assert payload_error("channel_not_found").payload == {
        "ok": False,
        "error": "channel_not_found",
    }


class FakeSocket:
    def __init__(self, body: bytes) -> None:
        self._body = io.BytesIO(body)
        self.status = 200
        self.headers = {"Content-Type": "application/json"}

    def read(self) -> bytes:
        return self._body.read()

    def __enter__(self) -> FakeSocket:
        return self

    def __exit__(self, *exc_info: object) -> None:
        return None


def test_urllib_transport_returns_status_and_json(monkeypatch) -> None:
    monkeypatch.setattr(urllib.request, "urlopen", lambda *a, **k: FakeSocket(b'{"ok": true}'))
    response = UrllibTransport(timeout=1.0).send(
        HttpRequest(method="POST", url="https://slack.com/api/auth.test", body=b"a=1")
    )
    assert response.status == 200
    assert response.json() == {"ok": True}
    assert response.header("content-type") == "application/json"


def test_urllib_transport_preserves_http_error_status(monkeypatch) -> None:
    def raise_http_error(*_args, **_kwargs):
        raise urllib.error.HTTPError(
            "https://slack.com/api/auth.test",
            429,
            "Too Many Requests",
            {"Retry-After": "2"},
            io.BytesIO(b'{"ok": false, "error": "ratelimited"}'),
        )

    monkeypatch.setattr(urllib.request, "urlopen", raise_http_error)
    response = UrllibTransport().send(
        HttpRequest(method="POST", url="https://slack.com/api/auth.test")
    )
    assert response.status == 429
    assert response.header("retry-after") == "2"
    assert response.json()["error"] == "ratelimited"


def test_urllib_transport_maps_network_failure_to_transport_error(monkeypatch) -> None:
    def raise_url_error(*_args, **_kwargs):
        raise urllib.error.URLError(TimeoutError("timed out"))

    monkeypatch.setattr(urllib.request, "urlopen", raise_url_error)
    with pytest.raises(TransportError) as caught:
        UrllibTransport().send(HttpRequest(method="POST", url="https://slack.com/api/auth.test"))
    assert caught.value.code == "timeout"
