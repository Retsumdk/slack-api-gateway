#!/usr/bin/env python3
"""Post an incident update to Slack, safely, from a script or a cron job.

What this example demonstrates:

* Block Kit blocks built from plain data, no hand-written JSON files.
* An ``idempotency_key`` derived from the incident ID, so re-running the job
  after a timeout cannot double-post the same update.
* Typed error handling: a missing channel and a bad token are different
  exceptions and deserve different exit codes, because a retry loop or a
  supervisor reacts differently to each.

Run it::

    export SLACK_BOT_TOKEN=xoxb-...
    python examples/incident_broadcast.py --channel C0123456789 \\
        --incident INC-42 --status investigating \\
        --summary "Elevated 5xx on the payments API"

The idempotency ledger lives inside the client process: if the same update is
posted twice by one long-running worker (a retried job step, a duplicate
webhook delivery), the second call replays the first result instead of
double-posting. A fresh process starts with an empty ledger by design.
"""

from __future__ import annotations

import argparse
import os
import sys
from typing import Any

from slack_api_gateway import (
    AuthenticationError,
    AuthorizationError,
    NotFoundError,
    SlackGatewayClient,
    SlackGatewayError,
)

STATUS_EMOJI = {
    "investigating": ":mag:",
    "identified": ":wrench:",
    "monitoring": ":eyes:",
    "resolved": ":white_check_mark:",
}


def build_blocks(*, incident: str, status: str, summary: str, owner: str) -> list[dict[str, Any]]:
    """Return the Block Kit payload for one incident update."""
    emoji = STATUS_EMOJI.get(status, ":information_source:")
    return [
        {
            "type": "header",
            "text": {"type": "plain_text", "text": f"{incident}: {status.upper()}"},
        },
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": f"{emoji} {summary}"},
        },
        {
            "type": "context",
            "elements": [{"type": "mrkdwn", "text": f"owner: *{owner}* · status: `{status}`"}],
        },
    ]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--channel", required=True, help="Channel ID, e.g. C0123456789")
    parser.add_argument("--incident", required=True, help="Incident identifier, e.g. INC-42")
    parser.add_argument(
        "--status",
        required=True,
        choices=sorted(STATUS_EMOJI),
        help="Current incident status",
    )
    parser.add_argument("--summary", required=True, help="One-line human summary")
    parser.add_argument("--owner", default=os.environ.get("USER", "on-call"))
    args = parser.parse_args(argv)

    if not os.environ.get("SLACK_BOT_TOKEN"):
        print("SLACK_BOT_TOKEN is not set; export a bot token first.", file=sys.stderr)
        return 2

    blocks = build_blocks(
        incident=args.incident, status=args.status, summary=args.summary, owner=args.owner
    )
    fallback_text = f"{args.incident} {args.status}: {args.summary}"

    with SlackGatewayClient() as client:
        try:
            message = client.post_message(
                args.channel,
                fallback_text,
                blocks=blocks,
                # Stable across reruns: same incident + status, same key.
                idempotency_key=f"{args.incident}:{args.status}",
            )
        except NotFoundError as exc:
            print(f"channel {args.channel} not found ({exc.code}); check the ID.", file=sys.stderr)
            return 1
        except (AuthenticationError, AuthorizationError) as exc:
            print(f"token rejected: {exc.code}. {exc.hint()}", file=sys.stderr)
            return 1
        except SlackGatewayError as exc:
            print(f"gateway failure: {exc} [{exc.code}]", file=sys.stderr)
            return 1

        replayed = client.stats.idempotent_replays > 0
        action = "replayed existing update" if replayed else "posted update"
        print(f"{action} in {message.channel} at ts={message.ts}")
        print(f"requests={client.stats.calls} retries={client.stats.retries}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
