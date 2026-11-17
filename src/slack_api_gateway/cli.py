"""Command-line front end: the gateway without writing Python.

Every subcommand is a thin wrapper over :class:`SlackGatewayClient`, which is
deliberate — a CLI that reimplements logic is a second, worse implementation.
Exit codes are stable so shell scripts can branch on them:

===  ==========================================================
0    success
1    Slack API / transport failure (message on stderr)
2    usage or configuration error
130  interrupted
===  ==========================================================
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any, TextIO

from ._version import __version__
from .client import SlackGatewayClient
from .config import SlackConfig
from .errors import ConfigError, SlackGatewayError

EXIT_OK = 0
EXIT_FAILURE = 1
EXIT_USAGE = 2
EXIT_INTERRUPT = 130


def _common_parser() -> argparse.ArgumentParser:
    """Options shared by the root parser and every subcommand.

    Defaults are ``SUPPRESS`` on purpose. The same options appear on the root
    parser and on each subcommand, so a plain ``default=None`` on the
    subcommand would overwrite a value the user passed *before* the
    subcommand. Suppressed defaults only appear when actually supplied.
    """
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument(
        "--token", default=argparse.SUPPRESS, help="Slack token (default: $SLACK_BOT_TOKEN)"
    )
    parser.add_argument(
        "--base-url",
        default=argparse.SUPPRESS,
        help="Web API root (default: https://slack.com/api/)",
    )
    parser.add_argument(
        "--timeout", type=float, default=argparse.SUPPRESS, help="Per-request timeout in seconds"
    )
    parser.add_argument(
        "--max-attempts",
        type=int,
        default=argparse.SUPPRESS,
        help="Total attempts per call, including the first",
    )
    parser.add_argument(
        "--no-pacing",
        action="store_true",
        default=argparse.SUPPRESS,
        help="Disable client-side token-bucket pacing",
    )
    parser.add_argument(
        "--json", action="store_true", default=argparse.SUPPRESS, help="Emit machine-readable JSON"
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        default=argparse.SUPPRESS,
        help="Log each request to stderr",
    )
    return parser


def _opt(args: argparse.Namespace, name: str, default: Any) -> Any:
    """Read a shared option that may be absent because its default is suppressed."""
    return getattr(args, name, default)


def build_parser() -> argparse.ArgumentParser:
    """Construct the full argument parser (exposed for tests and --help)."""
    common = _common_parser()
    parser = argparse.ArgumentParser(
        prog="slack-gateway",
        description="Typed Slack Web API gateway with pacing, retries and typed errors.",
        parents=[common],
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("whoami", parents=[common], help="Verify the token (auth.test)")
    sub.add_parser("demo", parents=[common], help="Run the offline demo, no token needed")

    channels = sub.add_parser("channels", parents=[common], help="List conversations")
    channels.add_argument(
        "--types", default="public_channel", help="Comma-separated conversation types"
    )
    channels.add_argument(
        "--include-archived", action="store_true", help="Include archived channels"
    )
    channels.add_argument("--limit", type=int, default=200, help="Page size (max 200)")

    history = sub.add_parser("history", parents=[common], help="Read a channel's messages")
    history.add_argument("channel", help="Channel ID (C…, D…, G…)")
    history.add_argument("--limit", type=int, default=20, help="Total messages to fetch")
    history.add_argument("--oldest", default=None, help="Only messages after this Slack ts")
    history.add_argument("--latest", default=None, help="Only messages before this Slack ts")

    users = sub.add_parser("users", parents=[common], help="List workspace members")
    users.add_argument("--limit", type=int, default=200, help="Page size (max 200)")
    users.add_argument("--include-deleted", action="store_true", help="Include deactivated members")

    user = sub.add_parser("user", parents=[common], help="Look up one member by ID or email")
    user.add_argument("identifier", help="User ID (U…) or email address")

    post = sub.add_parser("post", parents=[common], help="Post a message")
    post.add_argument("channel", help="Channel ID")
    post.add_argument("text", nargs="*", help="Message text (joined with spaces)")
    post.add_argument("--blocks", default=None, metavar="FILE", help="Block Kit JSON file")
    post.add_argument("--thread", default=None, metavar="TS", help="Reply in this thread")
    post.add_argument("--broadcast", action="store_true", help="Also send thread reply to channel")
    post.add_argument(
        "--idempotency-key", default=None, help="Replay-safe key; repeat it to avoid a double post"
    )

    react = sub.add_parser("react", parents=[common], help="Add a reaction to a message")
    react.add_argument("channel", help="Channel ID")
    react.add_argument("ts", help="Message timestamp")
    react.add_argument("emoji", help="Emoji name, with or without colons")

    upload = sub.add_parser("upload", parents=[common], help="Upload a file")
    upload.add_argument("path", help="Local file path")
    upload.add_argument("--channel", default=None, help="Also share into this channel")
    upload.add_argument("--title", default=None, help="File title")
    upload.add_argument("--comment", default=None, help="Comment posted with the file")

    raw = sub.add_parser("raw", parents=[common], help="Call any Web API method")
    raw.add_argument("method", help="Method name, e.g. team.info")
    raw.add_argument(
        "--param",
        action="append",
        default=[],
        metavar="KEY=VALUE",
        help="Method parameter (repeatable); values parse as JSON when possible",
    )
    return parser


def _client(args: argparse.Namespace) -> SlackGatewayClient:
    config = SlackConfig.from_env()
    updates: dict[str, Any] = {}
    if _opt(args, "base_url", None):
        updates["base_url"] = args.base_url
    if _opt(args, "timeout", None) is not None:
        updates["timeout"] = args.timeout
    if _opt(args, "max_attempts", None) is not None:
        updates["max_attempts"] = args.max_attempts
    if _opt(args, "no_pacing", False):
        updates["rate_limit_enabled"] = False
    if updates:
        config = SlackConfig(**{**config.to_dict(redact=False), **updates})
    config.validate()
    return SlackGatewayClient(token=_opt(args, "token", None), config=config)


def _emit(payload: Any, *, as_json: bool, out: TextIO, fallback: str) -> None:
    if as_json:
        out.write(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    else:
        out.write(fallback + "\n")


def _parse_params(pairs: Sequence[str]) -> dict[str, Any]:
    params: dict[str, Any] = {}
    for pair in pairs:
        if "=" not in pair:
            raise ConfigError(f"--param needs KEY=VALUE, got {pair!r}", code="config_error")
        key, _, raw_value = pair.partition("=")
        try:
            params[key] = json.loads(raw_value)
        except json.JSONDecodeError:
            params[key] = raw_value
    return params


def _dispatch(args: argparse.Namespace, client: SlackGatewayClient, out: TextIO) -> None:
    as_json = bool(_opt(args, "json", False))

    if args.command == "whoami":
        info = client.auth_test()
        _emit(
            {
                "team": info.team,
                "team_id": info.team_id,
                "user": info.user,
                "user_id": info.user_id,
                "bot_id": info.bot_id,
            },
            as_json=as_json,
            out=out,
            fallback=f"{info.team} ({info.team_id}) as @{info.user} ({info.user_id})",
        )
        return

    if args.command == "channels":
        channels = client.list_conversations(
            types=args.types, exclude_archived=not args.include_archived, limit=args.limit
        )
        rows = [
            {"id": c.id, "name": c.name, "members": c.num_members, "archived": c.is_archived}
            for c in channels
        ]
        _emit(
            rows,
            as_json=as_json,
            out=out,
            fallback="\n".join(f"{c.id}  #{c.name}  ({c.num_members} members)" for c in channels)
            or "no channels",
        )
        return

    if args.command == "history":
        messages = client.history(
            args.channel, limit=args.limit, oldest=args.oldest, latest=args.latest
        )
        rows = [{"ts": m.ts, "user": m.user, "bot_id": m.bot_id, "text": m.text} for m in messages]
        _emit(
            rows,
            as_json=as_json,
            out=out,
            fallback="\n".join(f"{m.ts}  {m.user or m.bot_id or '?'}: {m.text}" for m in messages)
            or "no messages",
        )
        return

    if args.command == "users":
        members = client.list_users(limit=args.limit, include_deleted=args.include_deleted)
        rows = [
            {
                "id": u.id,
                "name": u.name,
                "real_name": u.real_name,
                "is_bot": u.is_bot,
                "deleted": u.deleted,
            }
            for u in members
        ]
        _emit(
            rows,
            as_json=as_json,
            out=out,
            fallback="\n".join(
                f"{u.id}  {u.real_name or u.name}{' [bot]' if u.is_bot else ''}" for u in members
            )
            or "no members",
        )
        return

    if args.command == "user":
        found = (
            client.find_user_by_email(args.identifier)
            if "@" in args.identifier
            else client.get_user(args.identifier)
        )
        _emit(
            {
                "id": found.id,
                "name": found.name,
                "real_name": found.real_name,
                "email": found.email,
                "is_bot": found.is_bot,
            },
            as_json=as_json,
            out=out,
            fallback=f"{found.id}  {found.real_name or found.name}  <{found.email or 'hidden'}>",
        )
        return

    if args.command == "post":
        blocks: list[dict[str, Any]] | None = None
        if args.blocks:
            blocks = json.loads(Path(args.blocks).read_text(encoding="utf-8"))
            if not isinstance(blocks, list):
                raise ConfigError("--blocks file must contain a JSON array", code="config_error")
        text = " ".join(args.text) or None
        message = client.post_message(
            args.channel,
            text,
            blocks=blocks,
            thread_ts=args.thread,
            reply_broadcast=args.broadcast or None,
            idempotency_key=args.idempotency_key,
        )
        _emit(
            {"channel": message.channel, "ts": message.ts, "text": message.text},
            as_json=as_json,
            out=out,
            fallback=f"posted {message.ts} to {args.channel}",
        )
        return

    if args.command == "react":
        client.add_reaction(args.channel, args.ts, args.emoji)
        _emit(
            {"ok": True, "reaction": args.emoji.strip(":")},
            as_json=as_json,
            out=out,
            fallback=f"reacted :{args.emoji.strip(':')}: to {args.ts}",
        )
        return

    if args.command == "upload":
        uploaded = client.upload_file(
            path=args.path,
            channel=args.channel,
            title=args.title,
            initial_comment=args.comment,
        )
        _emit(
            {
                "id": uploaded.id,
                "name": uploaded.name,
                "size": uploaded.size,
                "permalink": uploaded.permalink,
            },
            as_json=as_json,
            out=out,
            fallback=f"uploaded {uploaded.name} ({uploaded.size} bytes) id={uploaded.id}",
        )
        return

    if args.command == "raw":
        payload = client.call(args.method, _parse_params(args.param))
        _emit(payload, as_json=True, out=out, fallback=json.dumps(payload, sort_keys=True))
        return

    raise ConfigError(f"unknown command: {args.command}", code="config_error")


def main(
    argv: Sequence[str] | None = None, out: TextIO | None = None, err: TextIO | None = None
) -> int:
    """Entry point. Returns an exit code; never raises for expected failures."""
    stdout = out or sys.stdout
    stderr = err or sys.stderr
    parser = build_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)

    if args.command == "demo":
        from .demo import run_demo

        transcript = run_demo()
        if _opt(args, "json", False):
            payload = {"result": "PASS", "transcript": transcript.splitlines()}
            stdout.write(json.dumps(payload, indent=2) + "\n")
        else:
            stdout.write(transcript)
        return EXIT_OK

    try:
        client = _client(args)
        try:
            _dispatch(args, client, stdout)
        finally:
            if _opt(args, "verbose", False):
                stderr.write(json.dumps(client.stats_snapshot(), sort_keys=True) + "\n")
            client.close()
    except ConfigError as exc:
        stderr.write(f"config error: {exc}\n")
        return EXIT_USAGE
    except SlackGatewayError as exc:
        hint = getattr(exc, "hint", lambda: "")()
        stderr.write(f"error: {exc} [{exc.code}]\n")
        if hint:
            stderr.write(f"hint: {hint}\n")
        return EXIT_FAILURE
    except KeyboardInterrupt:  # pragma: no cover - interactive only
        stderr.write("interrupted\n")
        return EXIT_INTERRUPT
    return EXIT_OK


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
