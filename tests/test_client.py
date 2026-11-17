"""The client chokepoint: pacing, retries, error mapping, caching, uploads."""

from __future__ import annotations

import urllib.parse

import pytest

from slack_api_gateway.client import SlackGatewayClient
from slack_api_gateway.config import SlackConfig
from slack_api_gateway.errors import (
    AuthenticationError,
    AuthorizationError,
    NotFoundError,
    RateLimitError,
    SlackApiError,
    TransportError,
    ValidationError,
)
from slack_api_gateway.transport import (
    CannedResponse,
    StubTransport,
    payload_error,
    payload_ok,
)

TOKEN_HEADER = "Bearer xoxb-test-token-not-real"


def decode(body: bytes) -> dict[str, list[str]]:
    return urllib.parse.parse_qs(body.decode())


def test_auth_test_sends_bearer_token_and_user_agent(make_client) -> None:
    transport = StubTransport({"auth.test": [payload_ok(team_id="T1", team="Acme", bot_id="B1")]})
    client = make_client(transport)

    info = client.auth_test()

    assert (info.team, info.team_id, info.bot_id) == ("Acme", "T1", "B1")
    request = transport.requests[0]
    assert request.method == "POST"
    assert request.url == "https://slack.test/api/auth.test"
    assert request.headers["Authorization"] == TOKEN_HEADER
    assert request.headers["Content-Type"] == "application/x-www-form-urlencoded"
    assert request.headers["User-Agent"].startswith("slack-api-gateway/")


def test_429_honours_retry_after_then_succeeds(make_client, clock) -> None:
    transport = StubTransport(
        [
            CannedResponse(status=429, headers={"retry-after": "2"}),
            payload_ok(team="Acme"),
        ]
    )
    client = make_client(transport)

    assert client.call("auth.test")["team"] == "Acme"
    assert clock.sleeps == [2.0]
    assert client.stats.rate_limited == 1
    assert client.stats.retries == 1
    # ``calls`` counts HTTP requests, so the 429 counts too.
    assert client.stats.calls == 2


def test_repeated_429_eventually_raises_rate_limit_error(make_client, clock) -> None:
    transport = StubTransport(
        [
            CannedResponse(status=429, headers={"retry-after": "1"}),
            CannedResponse(status=429, headers={"retry-after": "1"}),
        ]
    )
    client = make_client(transport, max_attempts=2)

    with pytest.raises(RateLimitError) as caught:
        client.call("chat.postMessage", {"channel": "C1", "text": "hi"})
    assert caught.value.retry_after == 1.0
    assert caught.value.status == 429
    assert len(clock.sleeps) == 1


def test_envelope_ratelimited_is_retried_with_server_delay(make_client, clock) -> None:
    transport = StubTransport(
        [
            payload_error("ratelimited", retry_after=3),
            payload_ok(ok=True, text="posted"),
        ]
    )
    client = make_client(transport)

    assert client.call("chat.postMessage", {"channel": "C1"})["text"] == "posted"
    assert clock.sleeps == [3.0]


def test_5xx_uses_exponential_backoff(make_client, clock) -> None:
    transport = StubTransport(
        [
            CannedResponse(status=503),
            CannedResponse(status=500),
            payload_ok(ok=True, team="Acme"),
        ]
    )
    client = make_client(transport, max_attempts=3)

    assert client.call("auth.test")["ok"] is True
    assert clock.sleeps == [0.5, 1.0]
    assert client.stats.retries == 2


def test_5xx_gives_up_as_transport_error(make_client, clock) -> None:
    transport = StubTransport([CannedResponse(status=503), CannedResponse(status=503)])
    client = make_client(transport, max_attempts=2)

    with pytest.raises(TransportError) as caught:
        client.call("auth.test")
    assert caught.value.status == 503
    assert "after 2 attempt(s)" in caught.value.message


def test_transport_failure_is_retried(make_client, clock) -> None:
    transport = StubTransport([CannedResponse(error="timeout"), payload_ok(ok=True)])
    client = make_client(transport)

    assert client.call("auth.test")["ok"] is True
    assert client.stats.transport_errors == 1
    assert clock.sleeps == [0.5]


def test_transport_failure_exhausts_retries(make_client) -> None:
    transport = StubTransport([CannedResponse(error="timeout")])
    client = make_client(transport, max_attempts=1)

    with pytest.raises(TransportError) as caught:
        client.call("auth.test")
    assert caught.value.code == "timeout"


def test_non_json_success_body_is_transport_error(make_client) -> None:
    transport = StubTransport([CannedResponse(status=200, body=b"<html>gateway</html>")])
    client = make_client(transport)

    with pytest.raises(TransportError) as caught:
        client.call("auth.test")
    assert "expected a JSON object" in caught.value.message


@pytest.mark.parametrize(
    ("error_code", "expected", "expected_code"),
    [
        ("invalid_auth", AuthenticationError, "invalid_auth"),
        ("token_revoked", AuthenticationError, "token_revoked"),
        ("missing_scope", AuthorizationError, "missing_scope"),
        ("not_in_channel", AuthorizationError, "not_in_channel"),
        ("channel_not_found", NotFoundError, "channel_not_found"),
        ("users_not_found", NotFoundError, "users_not_found"),
        ("invalid_arguments", ValidationError, "invalid_arguments"),
        ("something_new", SlackApiError, "something_new"),
    ],
)
def test_error_envelope_maps_to_typed_exception_and_is_not_retried(
    make_client, clock, error_code, expected, expected_code
) -> None:
    transport = StubTransport([payload_error(error_code)])
    client = make_client(transport)

    with pytest.raises(expected) as caught:
        client.call("conversations.info", {"channel": "C1"})
    assert caught.value.code == expected_code
    assert len(transport.requests) == 1
    assert clock.sleeps == []


def test_missing_scope_exposes_needed_scope(make_client) -> None:
    transport = StubTransport([payload_error("missing_scope", needed="chat:write")])
    client = make_client(transport)

    with pytest.raises(AuthorizationError) as caught:
        client.call("chat.postMessage", {"channel": "C1", "text": "hi"})
    assert caught.value.needed == "chat:write"


def test_http_401_without_envelope_is_authentication_error(make_client) -> None:
    transport = StubTransport([CannedResponse(status=401, payload={})])
    client = make_client(transport)

    with pytest.raises(AuthenticationError, match="rejected the token"):
        client.call("auth.test")


def test_http_403_with_scope_error_is_authorization_error(make_client) -> None:
    transport = StubTransport([payload_error("missing_scope", status=403, needed="files:write")])
    client = make_client(transport)

    with pytest.raises(AuthorizationError) as caught:
        client.call("files.info", {"file": "F1"})
    assert caught.value.needed == "files:write"


def test_missing_token_fails_before_any_request(monkeypatch) -> None:
    for name in ("SLACK_BOT_TOKEN", "SLACK_TOKEN", "SLACK_USER_TOKEN"):
        monkeypatch.delenv(name, raising=False)
    transport = StubTransport()
    client = SlackGatewayClient(None, config=SlackConfig(), transport=transport)

    with pytest.raises(AuthenticationError, match="no Slack token"):
        client.auth_test()
    assert transport.requests == []


def test_post_message_encodes_thread_and_broadcast(make_client) -> None:
    transport = StubTransport(
        [payload_ok(ok=True, channel="C1", ts="1700000000.000100", text="hi")]
    )
    client = make_client(transport)

    message = client.post_message("C1", "hi", thread_ts="1699999999.000100", reply_broadcast=True)

    assert (message.channel, message.ts, message.text) == ("C1", "1700000000.000100", "hi")
    body = decode(transport.requests[0].body)
    assert body["channel"] == ["C1"]
    assert body["text"] == ["hi"]
    assert body["thread_ts"] == ["1699999999.000100"]
    assert body["reply_broadcast"] == ["true"]


def test_post_message_accepts_blocks_instead_of_text(make_client) -> None:
    transport = StubTransport([payload_ok(ok=True, channel="C1", ts="1.1")])
    client = make_client(transport)

    blocks = [{"type": "section", "text": {"type": "mrkdwn", "text": "*deploy*"}}]
    client.post_message("C1", blocks=blocks)

    assert body_of(transport)["blocks"] == [
        '[{"type":"section","text":{"type":"mrkdwn","text":"*deploy*"}}]'
    ]


def body_of(transport: StubTransport) -> dict[str, list[str]]:
    return decode(transport.requests[0].body)


def test_post_message_rejects_empty_message(make_client) -> None:
    client = make_client(StubTransport())
    with pytest.raises(ValidationError, match="needs text or blocks") as caught:
        client.post_message("C1")
    assert caught.value.code == "no_text"


def test_post_message_rejects_malformed_blocks(make_client) -> None:
    client = make_client(StubTransport())
    with pytest.raises(ValidationError) as caught:
        client.post_message("C1", blocks=["not-a-block"])  # type: ignore[list-item]
    assert caught.value.code == "invalid_blocks"


def test_post_message_requires_channel(make_client) -> None:
    client = make_client(StubTransport())
    with pytest.raises(ValidationError, match="channel is required"):
        client.post_message("  ", "hi")


def test_idempotency_key_replays_instead_of_double_posting(make_client) -> None:
    transport = StubTransport(
        [
            payload_ok(ok=True, channel="C1", ts="1700000000.000100", text="deploy"),
            payload_ok(ok=True, channel="C1", ts="1700000001.000200", text="deploy"),
        ]
    )
    client = make_client(transport)

    first = client.post_message("C1", "deploy", idempotency_key="deploy-1")
    second = client.post_message("C1", "deploy", idempotency_key="deploy-1")
    third = client.post_message("C1", "deploy", idempotency_key="deploy-2")

    assert first.ts == second.ts
    assert third.ts != first.ts
    assert len(transport.requests) == 2
    assert len(transport.requests) == 2
    assert client.stats.idempotent_replays == 1


def test_get_channel_is_cached(make_client) -> None:
    transport = StubTransport(
        [payload_ok(channel={"id": "C1", "name": "engineering", "num_members": 42})]
    )
    client = make_client(transport)

    first = client.get_channel("C1")
    second = client.get_channel("C1")

    assert first == second
    assert first.name == "engineering"
    assert first.num_members == 42
    assert len(transport.requests) == 1
    assert client.stats.cache_hits == 1


def test_cache_can_be_disabled(make_client) -> None:
    transport = StubTransport(
        [
            payload_ok(channel={"id": "C1", "name": "engineering"}),
            payload_ok(channel={"id": "C1", "name": "engineering"}),
        ]
    )
    client = make_client(transport, cache_ttl=0.0)

    client.get_channel("C1")
    client.get_channel("C1")

    assert len(transport.requests) == 2
    assert client.stats.cache_hits == 0


def test_list_conversations_follows_cursors(make_client) -> None:
    transport = StubTransport(
        {
            "conversations.list": [
                CannedResponse(
                    payload={
                        "ok": True,
                        "channels": [
                            {"id": "C1", "name": "engineering"},
                            {"id": "C2", "name": "ops"},
                        ],
                        "response_metadata": {"next_cursor": "cursor-2"},
                    }
                ),
                CannedResponse(
                    payload={
                        "ok": True,
                        "channels": [{"id": "C3", "name": "random"}],
                        "response_metadata": {"next_cursor": ""},
                    }
                ),
            ]
        }
    )
    client = make_client(transport)

    channels = client.list_conversations(types="public_channel,private_channel")

    assert [channel.name for channel in channels] == ["engineering", "ops", "random"]
    assert decode(transport.requests[0].body)["types"] == ["public_channel,private_channel"]
    assert "cursor" not in decode(transport.requests[0].body)
    assert decode(transport.requests[1].body)["cursor"] == ["cursor-2"]


def test_history_trims_to_limit(make_client) -> None:
    messages = [{"type": "message", "ts": f"170000000{i}.0001", "text": f"m{i}"} for i in range(5)]
    transport = StubTransport({"conversations.history": [payload_ok(messages=messages)]})
    client = make_client(transport)

    history = client.history("C1", limit=2)

    assert [m.text for m in history] == ["m0", "m1"]
    assert decode(transport.requests[0].body)["limit"] == ["2"]


def test_history_rejects_non_positive_limit(make_client) -> None:
    client = make_client(StubTransport())
    with pytest.raises(ValidationError, match="limit must be at least 1"):
        client.history("C1", limit=0)


def test_replies_reads_thread(make_client) -> None:
    transport = StubTransport(
        {
            "conversations.replies": [
                payload_ok(
                    messages=[
                        {
                            "type": "message",
                            "ts": "1.1",
                            "text": "parent",
                            "thread_ts": "1.1",
                            "reply_count": 1,
                        },
                        {"type": "message", "ts": "1.2", "text": "child", "thread_ts": "1.1"},
                    ]
                )
            ]
        }
    )
    client = make_client(transport)

    thread = client.replies("C1", "1.1")

    assert [m.text for m in thread] == ["parent", "child"]
    assert thread[0].is_thread_parent is True
    assert decode(transport.requests[0].body)["ts"] == ["1.1"]


def test_add_reaction_strips_colons(make_client) -> None:
    transport = StubTransport({"reactions.add": [payload_ok()]})
    client = make_client(transport)

    client.add_reaction("C1", "1700000000.000100", ":thumbsup:")

    assert decode(transport.requests[0].body)["name"] == ["thumbsup"]


def test_add_reaction_rejects_empty_name(make_client) -> None:
    client = make_client(StubTransport())
    with pytest.raises(ValidationError, match="reaction name is empty"):
        client.add_reaction("C1", "1.1", "::")


def test_find_user_by_email_validates_input(make_client) -> None:
    client = make_client(StubTransport())
    with pytest.raises(ValidationError, match="not an email address"):
        client.find_user_by_email("not-an-email")


def test_find_user_by_email_reads_nested_profile(make_client) -> None:
    transport = StubTransport(
        {
            "users.lookupByEmail": [
                payload_ok(
                    user={
                        "id": "U2",
                        "name": "dana",
                        "real_name": "Dana Keys",
                        "profile": {"display_name": "dana", "email": "dana@acme.test"},
                        "tz": "America/New_York",
                    }
                )
            ]
        }
    )
    client = make_client(transport)

    user = client.find_user_by_email("dana@acme.test")

    assert (user.id, user.real_name, user.email) == ("U2", "Dana Keys", "dana@acme.test")


def test_list_users_filters_deleted_by_default(make_client) -> None:
    transport = StubTransport(
        {
            "users.list": [
                payload_ok(
                    members=[
                        {"id": "U1", "name": "live"},
                        {"id": "U2", "name": "gone", "deleted": True},
                    ]
                )
            ]
        }
    )
    client = make_client(transport)

    assert [u.id for u in client.list_users()] == ["U1"]

    second = StubTransport(
        {
            "users.list": [
                payload_ok(
                    members=[
                        {"id": "U1", "name": "live"},
                        {"id": "U2", "name": "gone", "deleted": True},
                    ]
                )
            ]
        }
    )
    assert [u.id for u in make_client(second).list_users(include_deleted=True)] == ["U1", "U2"]


def test_update_and_delete_message(make_client) -> None:
    transport = StubTransport(
        {
            "chat.update": [payload_ok(ok=True, channel="C1", ts="1.1", text="edited")],
            "chat.delete": [payload_ok(ok=True, ts="1.1")],
        }
    )
    client = make_client(transport)

    assert client.update_message("C1", "1.1", "edited").text == "edited"
    assert client.delete_message("C1", "1.1") == "1.1"


def test_update_requires_content(make_client) -> None:
    client = make_client(StubTransport())
    with pytest.raises(ValidationError, match="update needs text or blocks"):
        client.update_message("C1", "1.1")


def test_upload_file_runs_three_step_flow(make_client) -> None:
    transport = StubTransport(
        {
            "files.getUploadURLExternal": [
                payload_ok(upload_url="https://files.slack.test/upload/xyz", file_id="F1")
            ],
            "xyz": [CannedResponse(status=200, body=b"OK")],
            "files.completeUploadExternal": [
                payload_ok(
                    files=[
                        {
                            "id": "F1",
                            "name": "deploy.log",
                            "title": "deploy.log",
                            "mimetype": "text/plain",
                            "size": 11,
                            "permalink": "https://acme.slack.com/files/F1",
                        }
                    ]
                )
            ],
        }
    )
    client = make_client(transport)

    info = client.upload_file(
        content=b"deploy ok\n",
        filename="deploy.log",
        channel="C1",
        title="deploy.log",
        initial_comment="done",
    )

    assert info.id == "F1" and info.permalink.endswith("/F1")
    assert transport.methods_called == [
        "files.getUploadURLExternal",
        "xyz",
        "files.completeUploadExternal",
    ]
    slot_params = decode(transport.requests[0].body)
    assert slot_params["filename"] == ["deploy.log"]
    assert slot_params["length"] == ["10"]

    upload = transport.requests[1]
    assert upload.url == "https://files.slack.test/upload/xyz"
    assert upload.headers["Content-Type"].startswith("multipart/form-data; boundary=")
    assert b'filename="deploy.log"' in upload.body
    assert b"deploy ok\n" in upload.body

    complete = decode(transport.requests[2].body)
    assert complete["files"] == ['[{"id":"F1","title":"deploy.log"}]']
    assert complete["channel_id"] == ["C1"]
    assert complete["initial_comment"] == ["done"]


def test_upload_requires_exactly_one_source(make_client, tmp_path) -> None:
    target = tmp_path / "file.txt"
    target.write_text("data")
    client = make_client(StubTransport())

    with pytest.raises(ValidationError, match="exactly one of path= or content="):
        client.upload_file()
    with pytest.raises(ValidationError, match="exactly one of path= or content="):
        client.upload_file(path=str(target), content=b"data")


def test_upload_reads_path_and_derives_filename(make_client, tmp_path) -> None:
    target = tmp_path / "notes.md"
    target.write_text("# notes")
    transport = StubTransport(
        {
            "files.getUploadURLExternal": [
                payload_ok(upload_url="https://files.slack.test/upload/xyz", file_id="F2")
            ],
            "xyz": [CannedResponse(status=200, body=b"OK")],
            "files.completeUploadExternal": [payload_ok(files=[{"id": "F2", "name": "notes.md"}])],
        }
    )
    client = make_client(transport)

    info = client.upload_file(path=str(target))

    assert info.name == "notes.md"
    assert decode(transport.requests[0].body)["filename"] == ["notes.md"]


def test_upload_rejects_empty_and_unreadable_content(make_client, tmp_path) -> None:
    client = make_client(StubTransport())

    with pytest.raises(ValidationError, match="refusing to upload an empty file"):
        client.upload_file(content=b"", filename="empty.txt")
    with pytest.raises(ValidationError, match="filename is required"):
        client.upload_file(content=b"data")
    with pytest.raises(ValidationError, match="cannot read"):
        client.upload_file(path=str(tmp_path / "missing.txt"))


def test_upload_requires_upload_url_from_slack(make_client) -> None:
    transport = StubTransport({"files.getUploadURLExternal": [payload_ok(file_id="F1")]})
    client = make_client(transport)

    with pytest.raises(SlackApiError, match="no upload_url/file_id") as caught:
        client.upload_file(content=b"data", filename="a.txt")
    assert caught.value.code == "invalid_response"


def test_upload_presigned_failure_is_transport_error(make_client) -> None:
    transport = StubTransport(
        {
            "files.getUploadURLExternal": [
                payload_ok(upload_url="https://files.slack.test/upload/xyz", file_id="F1")
            ],
            "xyz": [CannedResponse(status=400, body=b"bad request")],
        }
    )
    client = make_client(transport)

    with pytest.raises(TransportError) as caught:
        client.upload_file(content=b"data", filename="a.txt")
    assert caught.value.status == 400


def test_raw_passthrough(make_client) -> None:
    transport = StubTransport({"team.info": [payload_ok(team={"id": "T1", "name": "Acme"})]})
    client = make_client(transport)

    payload = client.raw("team.info")

    assert payload["team"]["name"] == "Acme"
    assert client.stats_snapshot()["calls"] == 1


def test_stats_snapshot_includes_cache_block(make_client) -> None:
    client = make_client(StubTransport({"auth.test": [payload_ok()]}))
    client.auth_test()

    snapshot = client.stats_snapshot()
    assert snapshot["calls"] == 1
    assert snapshot["cache"]["entries"] == 0.0
    assert snapshot["paced_seconds"] >= 0.0


def test_close_is_idempotent_and_context_manager_works(make_client) -> None:
    client = make_client(StubTransport())
    with client as handle:
        assert handle is client
    client.close()
    client.close()
