"""CLI behaviour: stable exit codes, readable output, JSON on demand."""

from __future__ import annotations

import io
import json
import urllib.parse

import pytest

from slack_api_gateway import cli
from slack_api_gateway.transport import CannedResponse, StubTransport, payload_ok


def run(argv: list[str]) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    code = cli.main(argv, out=out, err=err)
    return code, out.getvalue(), err.getvalue()


@pytest.fixture
def offline(monkeypatch):
    """Route the CLI at a scripted transport instead of the network."""

    def install(responses, **client_kwargs):
        transport = StubTransport(responses)
        holder = {}

        def fake_client(_args):
            from slack_api_gateway.client import SlackGatewayClient
            from slack_api_gateway.config import SlackConfig

            holder["client"] = SlackGatewayClient(
                "xoxb-test-token-not-real",
                config=SlackConfig(cache_ttl=0.0, rate_limit_enabled=False),
                transport=transport,
                **client_kwargs,
            )
            return holder["client"]

        monkeypatch.setattr(cli, "_client", fake_client)
        return transport

    return install


def test_demo_runs_without_a_token() -> None:
    code, out, _ = run(["demo"])

    assert code == cli.EXIT_OK
    assert "RESULT: PASS" in out


def test_demo_json_is_machine_readable() -> None:
    code, out, _ = run(["--json", "demo"])
    payload = json.loads(out)

    assert code == cli.EXIT_OK
    assert payload["result"] == "PASS"
    assert any("chat.postMessage" in line for line in payload["transcript"])


def test_whoami_prints_the_authenticated_identity(offline) -> None:
    offline(
        {
            "auth.test": [
                payload_ok(team="Acme", team_id="T1", user="bot", user_id="U1", bot_id="B1")
            ]
        }
    )

    code, out, err = run(["whoami"])

    assert code == cli.EXIT_OK
    assert "Acme (T1) as @bot (U1)" in out
    assert err == ""


def test_whoami_json_exposes_ids(offline) -> None:
    offline({"auth.test": [payload_ok(team="Acme", team_id="T1", user="bot", user_id="U1")]})

    code, out, _ = run(["whoami", "--json"])
    payload = json.loads(out)

    assert code == cli.EXIT_OK
    assert payload == {
        "team": "Acme",
        "team_id": "T1",
        "user": "bot",
        "user_id": "U1",
        "bot_id": None,
    }


def test_channels_lists_names_and_member_counts(offline) -> None:
    offline(
        {
            "conversations.list": [
                payload_ok(
                    channels=[{"id": "C1", "name": "engineering", "num_members": 42}],
                    response_metadata={"next_cursor": ""},
                )
            ]
        }
    )

    code, out, _ = run(["channels"])

    assert code == cli.EXIT_OK
    assert "C1  #engineering  (42 members)" in out


def test_history_prints_sender_and_text(offline) -> None:
    offline(
        {
            "conversations.history": [
                payload_ok(
                    messages=[
                        {"type": "message", "ts": "1.1", "user": "U1", "text": "hello"},
                        {"type": "message", "ts": "1.2", "bot_id": "B1", "text": "from a bot"},
                    ],
                    response_metadata={"next_cursor": ""},
                )
            ]
        }
    )

    code, out, _ = run(["history", "C1", "--limit", "2"])

    assert code == cli.EXIT_OK
    assert "1.1  U1: hello" in out
    assert "1.2  B1: from a bot" in out


def test_user_flag_accepts_email_and_id(offline) -> None:
    offline(
        {
            "users.lookupByEmail": [
                payload_ok(user={"id": "U9", "name": "ada", "profile": {"email": "ada@acme.test"}})
            ],
            "users.info": [payload_ok(user={"id": "U9", "name": "ada"})],
        }
    )

    by_email = run(["user", "ada@acme.test"])
    by_id = run(["user", "U9"])

    assert by_email[0] == cli.EXIT_OK
    assert "ada@acme.test" in by_email[1]
    assert by_id[0] == cli.EXIT_OK
    assert "U9" in by_id[1]


def test_post_joins_words_and_threads(offline) -> None:
    transport = offline(
        {"chat.postMessage": [payload_ok(channel="C1", ts="1.9", text="deploy is live")]}
    )

    code, out, _ = run(["post", "C1", "deploy", "is", "live", "--thread", "1.1"])

    assert code == cli.EXIT_OK
    assert "posted 1.9 to C1" in out
    body = urllib.parse.parse_qs(transport.requests[0].body.decode())
    assert body["text"] == ["deploy is live"]
    assert body["thread_ts"] == ["1.1"]


def test_react_accepts_colon_wrapped_emoji(offline) -> None:
    transport = offline({"reactions.add": [payload_ok()]})

    code, out, _ = run(["react", "C1", "1.1", ":thumbsup:"])

    assert code == cli.EXIT_OK
    assert "reacted :thumbsup: to 1.1" in out
    body = urllib.parse.parse_qs(transport.requests[0].body.decode())
    assert body["name"] == ["thumbsup"]


def test_upload_reports_the_created_file(offline) -> None:
    offline(
        {
            "files.getUploadURLExternal": [
                payload_ok(upload_url="https://files.slack.test/upload/v1/slot1", file_id="F1")
            ],
            "slot1": [CannedResponse(status=200, body=b"OK")],
            "files.completeUploadExternal": [
                payload_ok(files=[{"id": "F1", "name": "report.txt", "size": 7}])
            ],
        }
    )

    code, out, _ = run(["upload", "tests/test_demo.py"])

    assert code == cli.EXIT_OK
    assert "uploaded report.txt" in out
    assert "id=F1" in out
    assert "id=F1" in out


def test_raw_parses_json_parameters(offline) -> None:
    transport = offline({"team.info": [payload_ok(team={"id": "T1", "name": "Acme"})]})

    code, out, _ = run(["raw", "team.info", "--param", "count=3", "--param", "name=Acme"])

    assert code == cli.EXIT_OK
    assert "Acme" in out
    body = urllib.parse.parse_qs(transport.requests[0].body.decode())
    assert body["count"] == ["3"]
    assert body["name"] == ["Acme"]


def test_missing_token_is_a_clean_failure(monkeypatch) -> None:
    for name in ("SLACK_BOT_TOKEN", "SLACK_TOKEN", "SLACK_USER_TOKEN"):
        monkeypatch.delenv(name, raising=False)

    code, _, err = run(["whoami"])

    assert code == cli.EXIT_FAILURE
    assert "not_authed" in err
    assert "hint:" in err


def test_api_error_exits_one_with_hint(offline) -> None:
    offline({"auth.test": [CannedResponse(payload={"ok": False, "error": "invalid_auth"})]})

    code, _, err = run(["whoami"])

    assert code == cli.EXIT_FAILURE
    assert "invalid_auth" in err


def test_verbose_writes_counters_to_stderr(offline) -> None:
    offline({"auth.test": [payload_ok(team="Acme", team_id="T1")]})

    code, _out, err = run(["whoami", "--verbose"])

    assert code == cli.EXIT_OK
    counters = json.loads(err.strip().splitlines()[-1])
    assert counters["calls"] == 1
    assert "cache" in counters


def test_unknown_command_is_a_usage_error() -> None:
    with pytest.raises(SystemExit) as excinfo:
        cli.main(["definitely-not-a-command"], out=io.StringIO(), err=io.StringIO())

    assert excinfo.value.code == cli.EXIT_USAGE


def test_version_flag_reports_package_version(capsys) -> None:
    with pytest.raises(SystemExit) as excinfo:
        cli.main(["--version"])

    assert excinfo.value.code == cli.EXIT_OK
    assert "slack-gateway" in capsys.readouterr().out
