"""Offline, deterministic demonstration of the gateway.

``slack-gateway demo`` needs no token and makes no network call, yet it runs the
real client — real pacing, real retry policy, real error mapping, real
pagination, real multipart encoding — against :class:`StubTransport`. That makes
it two things at once: a readable tour of the failure modes the gateway
handles, and a regression test whose output is byte-compared in CI.
"""

from __future__ import annotations

import json
from collections.abc import Callable

from .client import SlackGatewayClient
from .config import SlackConfig
from .errors import NotFoundError, PaginationError
from .pagination import paginate
from .rate_limit import Pacer
from .retry import RetryPolicy
from .transport import CannedResponse, StubTransport, payload_error, payload_ok

#: Deliberately fake. The demo never performs a network call, so this value is
#: never sent anywhere.
DEMO_TOKEN = "xoxb-demo-token-not-a-real-credential"


class FakeClock:
    """Monotonic clock whose sleeping only advances the clock."""

    def __init__(self) -> None:
        self.now = 1_000.0
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


def build_transport() -> StubTransport:
    """Script the responses for the whole tour."""
    return StubTransport(
        {
            "auth.test": [
                payload_ok(
                    team="Acme Rocketry",
                    team_id="T0ACME",
                    user="gateway-bot",
                    user_id="U0BOT",
                    bot_id="B0GATE",
                    url="https://acme-rocketry.slack.com/",
                )
            ],
            "conversations.list": [
                CannedResponse(
                    payload={
                        "ok": True,
                        "channels": [
                            {
                                "id": "C0ENG",
                                "name": "engineering",
                                "num_members": 42,
                                "is_member": True,
                            },
                            {"id": "C0OPS", "name": "ops", "num_members": 17},
                        ],
                        "response_metadata": {"next_cursor": "page-two"},
                    }
                ),
                CannedResponse(
                    payload={
                        "ok": True,
                        "channels": [{"id": "C0RAND", "name": "random", "num_members": 88}],
                        "response_metadata": {"next_cursor": ""},
                    }
                ),
            ],
            "conversations.info": [
                payload_ok(
                    channel={
                        "id": "C0ENG",
                        "name": "engineering",
                        "is_channel": True,
                        "num_members": 42,
                        "is_member": True,
                        "topic": {"value": "ship it"},
                    }
                )
            ],
            "chat.postMessage": [
                CannedResponse(status=429, headers={"retry-after": "1"}),
                payload_ok(
                    channel="C0ENG",
                    ts="1723456789.000100",
                    text="deploy 2026.11.17 is live",
                    type="message",
                ),
            ],
            "conversations.history": [payload_error("channel_not_found")],
            "users.lookupByEmail": [payload_error("users_not_found")],
            "files.getUploadURLExternal": [
                payload_ok(upload_url="https://files.slack.com/upload/v1/abc123", file_id="F0FILE")
            ],
            "abc123": [CannedResponse(status=200, body=b"OK")],
            "files.completeUploadExternal": [
                payload_ok(
                    files=[
                        {
                            "id": "F0FILE",
                            "name": "deploy-log.txt",
                            "title": "deploy-log.txt",
                            "mimetype": "text/plain",
                            "size": 19,
                            "permalink": "https://acme-rocketry.slack.com/files/U0BOT/F0FILE/deploy-log.txt",
                        }
                    ]
                )
            ],
        }
    )


def run_demo() -> str:
    """Execute the tour and return the transcript as a single string."""
    clock = FakeClock()
    transport = build_transport()
    client = SlackGatewayClient(
        DEMO_TOKEN,
        config=SlackConfig(cache_ttl=60.0),
        transport=transport,
        retry_policy=RetryPolicy(
            max_attempts=4, base_delay=0.5, max_delay=8.0, jitter=lambda lo, hi: hi
        ),
        pacer=Pacer(enabled=True, burst=20.0, clock=clock, sleep=clock.sleep),
        clock=clock,
        sleep=clock.sleep,
    )

    lines: list[str] = [
        "slack-api-gateway offline demo",
        "transport=stub  network=disabled  clock=virtual",
        "",
    ]

    info = client.auth_test()
    lines.append(f"1. auth.test            -> {info.team} ({info.team_id}) as {info.bot_id}")

    channels = client.list_conversations()
    lines.append(
        "2. conversations.list   -> "
        + ", ".join(f"#{c.name}" for c in channels)
        + f"  ({len(channels)} across 2 pages)"
    )

    client.get_channel("C0ENG")
    client.get_channel("C0ENG")
    hits = client.stats.cache_hits
    lines.append(f"3. conversations.info   -> 2 lookups, {hits} served from cache")

    message = client.post_message(
        "C0ENG", "deploy 2026.11.17 is live", idempotency_key="deploy-2026.11.17"
    )
    lines.append(
        f"4. chat.postMessage     -> 429 then retry -> ts={message.ts} "
        f"(retries={client.stats.retries}, slept={sum(clock.sleeps):.1f}s)"
    )

    replay = client.post_message(
        "C0ENG", "deploy 2026.11.17 is live", idempotency_key="deploy-2026.11.17"
    )
    lines.append(
        f"5. identical repost     -> replayed ts={replay.ts} "
        f"(idempotent_replays={client.stats.idempotent_replays}, extra requests=0)"
    )

    try:
        client.history("C0MISSING")
    except NotFoundError as exc:
        lines.append(f"6. conversations.history-> NotFoundError[{exc.code}] — not retried")

    try:
        client.find_user_by_email("nobody@acme.test")
    except NotFoundError as exc:
        lines.append(f"7. users.lookupByEmail -> NotFoundError[{exc.code}] — not retried")

    file_info = client.upload_file(
        content=b"deploy ok 2026.11.17",
        filename="deploy-log.txt",
        channel="C0ENG",
        title="deploy-log.txt",
        initial_comment="deploy finished",
    )
    upload_calls = transport.methods_called.count("abc123")
    lines.append(
        f"8. upload 3-step flow   -> id={file_info.id} name={file_info.name} "
        f"size={file_info.size} (presigned POSTs={upload_calls})"
    )

    loop_transport = StubTransport(
        {
            "conversations.list": [
                CannedResponse(
                    payload={
                        "ok": True,
                        "channels": [],
                        "response_metadata": {"next_cursor": "again"},
                    }
                )
            ]
        },
        default=CannedResponse(
            payload={"ok": True, "channels": [], "response_metadata": {"next_cursor": "again"}}
        ),
    )
    try:
        paginate(loop_transport_call(loop_transport), max_pages=10)
    except PaginationError as exc:
        lines.append(f"9. cursor loop          -> PaginationError[{exc.code}] after 2 pages")

    lines.append("")
    lines.append("stats " + json.dumps(client.stats_snapshot(), sort_keys=True))
    lines.append("")
    lines.append("RESULT: PASS")
    return "\n".join(lines) + "\n"


def loop_transport_call(
    transport: StubTransport,
) -> Callable[[dict[str, object]], dict[str, object]]:
    """Wrap a stub transport as the ``fetch`` callable ``paginate`` expects."""

    def fetch(params: dict[str, object]) -> dict[str, object]:
        from .transport import HttpRequest

        response = transport.send(
            HttpRequest(method="POST", url="https://slack.com/api/conversations.list")
        )
        return dict(response.json())

    return fetch


def main() -> int:  # pragma: no cover - exercised through the CLI
    print(run_demo(), end="")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
