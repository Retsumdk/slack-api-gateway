#!/usr/bin/env python3
"""Summarise recent activity across one or more channels.

Real-world shape of the job: a scheduler runs this every morning, posts the
digest to a channel, and never trips Slack's rate limits because the gateway
paces and caches for it. The user lookup in particular is cached — one HTTP
call per person, not one per message.

Usage:
    export SLACK_BOT_TOKEN=xoxb-...
    python examples/channel_digest.py --channel C0ENG --channel C0OPS --limit 50
"""

from __future__ import annotations

import argparse
import sys
from collections import Counter

from slack_api_gateway import (
    NotFoundError,
    SlackGatewayClient,
    SlackGatewayError,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Summarise recent channel activity.")
    parser.add_argument("--channel", nargs="+", required=True, help="One or more channel IDs")
    parser.add_argument("--limit", type=int, default=50, help="Messages per channel (default 50)")
    return parser


def display_name(client: SlackGatewayClient, user_id: str, cache: dict[str, str]) -> str:
    """Resolve a user ID to a readable name, one API call per person per run."""
    if user_id not in cache:
        try:
            user = client.get_user(user_id)
        except NotFoundError:
            cache[user_id] = user_id
        else:
            cache[user_id] = user.real_name or user.display_name or user.name or user_id
    return cache[user_id]


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    names: dict[str, str] = {}
    exit_code = 0

    with SlackGatewayClient() as client:
        for channel_id in args.channel:
            try:
                messages = client.history(channel_id, limit=args.limit)
            except SlackGatewayError as exc:
                print(f"#{channel_id}: unavailable ({exc.code})", file=sys.stderr)
                exit_code = 1
                continue

            speakers = Counter(
                display_name(client, message.user, names)
                for message in messages
                if message.user and not message.subtype
            )
            reactions = Counter(
                reaction.name for message in messages for reaction in message.reactions
            )

            print(f"#{channel_id}: {len(messages)} message(s)")
            for speaker, count in speakers.most_common(5):
                print(f"  {count:>3}  {speaker}")
            if reactions:
                top = ", ".join(f":{name}: x{count}" for name, count in reactions.most_common(3))
                print(f"  reactions: {top}")
            if messages:
                newest = messages[0]
                print(f"  latest: {newest.ts} — {newest.text[:80]}")

        print(f"cache: {client.stats_snapshot()['cache']}")
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
