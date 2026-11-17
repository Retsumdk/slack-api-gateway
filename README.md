# slack-api-gateway

[![CI](https://github.com/Retsumdk/slack-api-gateway/actions/workflows/ci.yml/badge.svg)](https://github.com/Retsumdk/slack-api-gateway/actions/workflows/ci.yml)
![Python](https://img.shields.io/badge/python-3.11%20%7C%203.12%20%7C%203.13-blue)
![Runtime dependencies](https://img.shields.io/badge/runtime%20dependencies-0-brightgreen)
![Tests](https://img.shields.io/badge/tests-177%20passing-brightgreen)

A typed Python client for the Slack Web API that treats rate limits, retries, pagination and
errors as first-class problems instead of something each integration re-solves badly.

## The problem

Anything that talks to Slack eventually hits the same four failures, and most integrations get
at least one of them wrong:

1. **`429 Too Many Requests`.** Slack answers with a `Retry-After` header that must be honoured
   exactly. Retrying on a locally invented schedule — or not retrying at all — either burns the
   method's budget for the whole workspace or drops the work.
2. **Retries that duplicate work.** A timed-out `chat.postMessage` has usually *succeeded* on
   Slack's side. Blindly retrying posts the same announcement twice. Slack offers no idempotency
   key, so this has to be solved in the client.
3. **Errors as strings.** `{"ok": false, "error": "channel_not_found"}` arrives with HTTP 200.
   Left as a string it becomes `if "not_found" in str(response)`, which silently swallows
   `users_not_found`, matches unrelated codes, and retries answers that will never change.
4. **Cursor pagination.** `response_metadata.next_cursor` can echo a cursor back under partial
   failure or a proxy bug. The naive `while cursor:` loop then spins forever, holding a worker.

The usual fix is a wrapper around `requests` in every repository. That wrapper tends to grow a
custom retry decorator, a hand-rolled limiter, and a dictionary of magic strings — none of it
tested, because testing it requires hitting Slack.

## What this does

| Guarantee | How it is enforced |
|---|---|
| **Every call is paced** | One token bucket per API family, refilled continuously at the documented per-minute budget. Bursts are allowed; sustained abuse is not. |
| **`429` always wins** | A server `Retry-After` overrides the local budget verbatim and resets the bucket. |
| **Retries are bounded and jittered** | Exponential backoff with *full jitter* (`random.uniform(0, cap)`), 5 attempts by default, capped at 30 s. A fleet that gets 429'd together does not retry in lockstep. |
| **Answers are never retried** | `channel_not_found`, `invalid_auth`, `missing_scope`, `invalid_blocks` raise typed exceptions on the first response. |
| **Writes can be replay-safe** | An in-process idempotency ledger keyed by a value you choose: the same write inside the TTL replays the stored result instead of posting twice. |
| **Pagination cannot loop** | Every cursor is recorded. A repeat, or the page cap, raises `PaginationError` instead of hanging. |
| **Zero runtime dependencies** | HTTP, JSON, form encoding and multipart upload are a few dozen lines of standard library. `pip install` cannot fail on a dependency. |
| **Time is injectable** | Clock, sleep, jitter and transport are constructor arguments, so the whole suite runs in 0.2 s with exact assertions on waits. |

## How it works

```
call(method, params)
   │
   ├─ cache lookup            read methods only, TTL-bounded
   ├─ pacer.acquire(family)   token bucket per call family
   ├─ transport.send(req)     urllib; raises TransportError on network failure
   │
   ├─ HTTP 429 ───────► honour Retry-After, reset bucket, retry
   ├─ HTTP 401/403 ───► AuthenticationError / AuthorizationError
   ├─ HTTP 5xx ───────► retry with jittered backoff, then TransportError
   ├─ non-JSON body ──► TransportError (proxy/HTML pages never masquerade as success)
   └─ {"ok": false} ──► error_from_payload() → typed exception, retried only if transient
```

Four decisions carry most of the design:

**One chokepoint.** Typed helpers (`post_message`, `history`, `upload_file`, …) and the raw
`call()` all pass through `SlackGatewayClient.call`. Pacing, retries and error mapping therefore
cannot be bypassed by a new helper, which is exactly how gateways rot.

**The server is the authority.** Local token buckets keep a busy loop from hammering Slack, but
when Slack says `Retry-After: 7` the client waits 7 seconds — not 0.5, not 30.

**Answers are not hiccups.** `channel_not_found` is data. The error taxonomy is a closed set of
exception classes, so `except NotFoundError` means what it says and a retry policy can never
spin on a permanent answer.

**Everything time-related is injected.** `clock`, `sleep`, `jitter` and `transport` are arguments.
That is why the suite asserts `clock.sleeps == [2.0]` instead of actually sleeping, and why the
offline demo is byte-identical on every machine.

## Getting started

Requires Python 3.11+ and nothing else.

```bash
git clone https://github.com/Retsumdk/slack-api-gateway.git
cd slack-api-gateway
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"          # or: pip install . for library use only
```

Create a Slack app with the scopes you need (`chat:write`, `channels:read`, `users:read`, …),
install it to the workspace, then export the bot token:

```bash
export SLACK_BOT_TOKEN="xoxb-..."
```

Verify the token and identify the caller:

```bash
$ slack-gateway whoami
Acme Rocketry (T0ACME) as @gateway-bot (U0BOT)
```

Send a message, read a channel, upload a file:

```bash
$ slack-gateway post C0ENG deploy 2026.11.17 is live
posted 1731000000.000100 to C0ENG

$ slack-gateway history C0ENG --limit 2
1731000000.000100  U0BOT: deploy 2026.11.17 is live
1730999000.000050  U0ADA: ack, watching graphs

$ slack-gateway upload ./incident.log --channel C0ENG --title incident.log --comment "full log attached"
uploaded incident.log (40 bytes) id=F0FILE
```

Errors are typed, carry an actionable hint and set a stable exit code:

```bash
$ slack-gateway history C0MISSING
error: Slack API error: channel_not_found [channel_not_found]
hint: Verify the ID: channel/user/message IDs are workspace-specific.
$ echo $?
1
```

## A tour you can run without a token

`slack-gateway demo` drives the real client — real pacing, retries, error mapping, pagination and
multipart encoding — against a scripted transport with a virtual clock. No network, no
credentials, deterministic output:

```text
$ slack-gateway demo
slack-api-gateway offline demo
transport=stub  network=disabled  clock=virtual

1. auth.test            -> Acme Rocketry (T0ACME) as B0GATE
2. conversations.list   -> #engineering, #ops, #random  (3 across 2 pages)
3. conversations.info   -> 2 lookups, 1 served from cache
4. chat.postMessage     -> 429 then retry -> ts=1723456789.000100 (retries=1, slept=1.0s)
5. identical repost     -> replayed ts=1723456789.000100 (idempotent_replays=1, extra requests=0)
6. conversations.history-> NotFoundError[channel_not_found] — not retried
7. users.lookupByEmail -> NotFoundError[users_not_found] — not retried
8. upload 3-step flow   -> id=F0FILE name=deploy-log.txt size=19 (presigned POSTs=1)
9. cursor loop          -> PaginationError[pagination_loop] after 2 pages

stats {"cache": {"entries": 1.0, "evictions": 0.0, "hit_rate": 0.333, "hits": 1.0, "misses": 2.0}, "cache_hits": 1, "calls": 11, "idempotent_replays": 1, "paced_seconds": 0.0, "rate_limited": 1, "retries": 1, "transport_errors": 0}

RESULT: PASS
```

That transcript is `demo-output.txt` in this repository, and a test asserts byte equality. If a
future change alters retry accounting, cache behaviour or error mapping, CI fails.

## CLI

| Command | What it does |
|---|---|
| `whoami` | `auth.test` — verify the token, print team and bot identity |
| `channels` | `conversations.list`, following cursors (`--types`, `--include-archived`, `--limit`) |
| `history <channel>` | `conversations.history` (`--limit`, `--oldest`, `--latest`) |
| `users` | `users.list` (`--limit`, `--include-deleted`) |
| `user <id\|email>` | `users.info` or `users.lookupByEmail` |
| `post <channel> <text…>` | `chat.postMessage` (`--blocks`, `--thread`, `--broadcast`, `--idempotency-key`) |
| `react <channel> <ts> <emoji>` | `reactions.add` (`thumbsup` or `:thumbsup:`) |
| `upload <path>` | three-step external upload (`--channel`, `--title`, `--comment`) |
| `raw <method>` | any Web API method (`--param k=v`, repeatable; values parse as JSON) |
| `demo` | the offline tour above |

Global flags: `--token`, `--base-url`, `--timeout`, `--max-attempts`, `--no-pacing`, `--json`,
`-v/--verbose` (dumps the stats snapshot to stderr).

Exit codes: `0` success, `1` Slack/transport failure, `2` usage or configuration error, `130`
interrupted.

## Library usage

Post an incident update with Block Kit, replay-safely:

```python
import os

from slack_api_gateway import NotFoundError, SlackGatewayClient

blocks = [
    {"type": "header", "text": {"type": "plain_text", "text": "INC-42: INVESTIGATING"}},
    {"type": "section", "text": {"type": "mrkdwn", "text": ":mag: Elevated 5xx on the payments API"}},
]

with SlackGatewayClient(os.environ["SLACK_BOT_TOKEN"]) as slack:
    try:
        message = slack.post_message(
            "C0123456789",
            "INC-42 investigating: elevated 5xx on the payments API",
            blocks=blocks,
            idempotency_key="INC-42:investigating",
        )
    except NotFoundError as exc:
        raise SystemExit(f"bad channel id: {exc.code}")
    print(message.ts)
```

Walk every conversation without thinking about cursors:

```python
from slack_api_gateway import SlackGatewayClient

with SlackGatewayClient() as slack:
    for channel in slack.list_conversations(types="public_channel,private_channel"):
        print(f"{channel.id}\t#{channel.name}\t{channel.num_members} members")
```

Upload with the current, non-deprecated flow (`files.upload` is gone; this uses
`files.getUploadURLExternal` → presigned POST → `files.completeUploadExternal`):

```python
from slack_api_gateway import SlackGatewayClient

with SlackGatewayClient() as slack:
    info = slack.upload_file(
        path="build/report.xml",
        channel="C0123456789",
        initial_comment="nightly build report",
    )
    print(info.id, info.permalink)
```

### Error taxonomy

Every failure is a `SlackGatewayError`. The specific class tells you whether to retry:

| Exception | Raised for | Retried? |
|---|---|---|
| `RateLimitError` | `429` after every attempt (`retry_after` included) | yes, until exhausted |
| `TransportError` | DNS/TLS/timeout, 5xx after retries, non-JSON body | yes, until exhausted |
| `AuthenticationError` | `invalid_auth`, `token_revoked`, HTTP 401 | never |
| `AuthorizationError` | `missing_scope`, `not_in_channel`, HTTP 403 (`needed` lists the scope) | never |
| `NotFoundError` | `channel_not_found`, `user_not_found`, `message_not_found` | never |
| `ValidationError` | `invalid_blocks`, `no_text`, bad local arguments | never |
| `PaginationError` | repeated cursor, or page cap reached | never |
| `ConfigError` | contradictory settings (bad `base_url`, negative timeout) | never |

Every `SlackApiError` exposes `.code` (Slack's string, always) and `.hint()` (the next step).

### Escape hatch

Any method, including ones added after this release:

```python
with SlackGatewayClient() as slack:
    data = slack.raw("team.info", {"team": "T01234567"})
```

## Configuration

Environment beats file beats defaults. The token is read from `SLACK_BOT_TOKEN` (or `SLACK_TOKEN`,
or `SLACK_USER_TOKEN`) unless passed explicitly.

| Variable | Field | Default |
|---|---|---|
| `SLACK_BOT_TOKEN` | token | — |
| `SLACK_BASE_URL` | `base_url` | `https://slack.com/api/` |
| `SLACK_TIMEOUT` | `timeout` (s) | `30.0` |
| `SLACK_MAX_ATTEMPTS` | `max_attempts` | `5` |
| `SLACK_RETRY_BASE_DELAY` | `retry_base_delay` (s) | `0.5` |
| `SLACK_RETRY_MAX_DELAY` | `retry_max_delay` (s) | `30.0` |
| `SLACK_CACHE_TTL` | `cache_ttl` (s, `0` disables) | `60.0` |
| `SLACK_RATE_LIMIT` | `rate_limit_enabled` | `true` |
| `SLACK_MAX_PAGES` | `max_pages` | `25` |

Programmatic equivalent, including a JSON file (unknown keys are rejected, not ignored):

```python
from slack_api_gateway import SlackConfig, SlackGatewayClient

config = SlackConfig.load("gateway.json")      # then environment overrides
client = SlackGatewayClient(config=config)
print(config.to_dict())                        # token redacted
```

## Quality gates

```bash
ruff check src tests examples     # lint (E,F,W,I,N,UP,B,C4,SIM,RUF,S,RET,PTH)
ruff format --check src tests examples
mypy                              # strict, no ignores, src/ only
pytest --cov=slack_api_gateway    # 177 tests, 94% statement+branch coverage
slack-gateway demo | diff - demo-output.txt
```

The suite needs no network and no token: `tests/conftest.py` builds a client against
`StubTransport` with a virtual clock, so retry accounting is asserted exactly
(`clock.sleeps == [2.0]`) rather than approximately. CI runs the same commands on Python 3.11,
3.12 and 3.13, plus a wheel build and an install-from-wheel smoke test.

## What this deliberately does not do

- **No Socket Mode / Events API listener.** This is a request/response gateway. Long-lived
  websocket consumers have a different failure model and belong in a separate component.
- **No OAuth install flow.** Token handling (rotation, storage) is the host application's job;
  this library only reads a token it is given.
- **No async client.** Blocking calls with explicit pacing are easier to reason about in workers
  and scripts. The transport is a protocol, so an async client can reuse everything else.
- **No third-party HTTP library.** `urllib` covers this surface; a dependency would buy nothing
  and cost determinism in CI.

## Layout

```
src/slack_api_gateway/
  client.py      the single chokepoint: pacing → retry → parse → map errors
  errors.py      exception taxonomy and Slack error-code mapping
  retry.py       retry decisions, jittered exponential backoff
  rate_limit.py  token bucket + per-family pacer
  pagination.py  cursor pagination with loop protection
  cache.py       TTL cache and idempotency ledger
  transport.py   HttpRequest/HttpResponse, urllib transport, stub transport
  models.py      typed, tolerant views over Slack payloads
  config.py      defaults → file → environment, validated
  cli.py         `slack-gateway` command line
  demo.py        deterministic offline tour
examples/        two runnable scripts (incident broadcast, channel digest)
tests/           177 tests, no network
```

## License

MIT — see [LICENSE](LICENSE).

Built by [Retsumdk](https://github.com/Retsumdk).
