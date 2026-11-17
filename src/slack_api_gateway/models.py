"""Typed views over Slack's JSON payloads.

Slack returns deeply nested dictionaries with wildly inconsistent keys.
These dataclasses give callers autocompletion and a stable shape, and every
one of them tolerates missing fields — Slack adds and removes keys between
API versions, so a missing optional key must never raise.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any


def _get(data: Mapping[str, Any], *path: str) -> Any:
    """Read a dotted path, returning ``None`` if any hop is missing."""
    current: Any = data
    for key in path:
        if not isinstance(current, Mapping):
            return None
        current = current.get(key)
    return current


@dataclass(frozen=True, slots=True)
class AuthInfo:
    """Result of ``auth.test`` — who the token belongs to."""

    ok: bool = False
    team_id: str = ""
    team: str = ""
    user_id: str = ""
    user: str = ""
    bot_id: str | None = None
    is_enterprise_install: bool = False
    url: str | None = None

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> AuthInfo:
        return cls(
            ok=bool(data.get("ok")),
            team_id=str(data.get("team_id") or ""),
            team=str(data.get("team") or ""),
            user_id=str(data.get("user_id") or ""),
            user=str(data.get("user") or ""),
            bot_id=data.get("bot_id"),
            is_enterprise_install=bool(data.get("is_enterprise_install")),
            url=data.get("url"),
        )


@dataclass(frozen=True, slots=True)
class Channel:
    """A public channel, private channel, DM, or group DM."""

    id: str = ""
    name: str = ""
    is_channel: bool = False
    is_private: bool = False
    is_archived: bool = False
    is_member: bool = False
    num_members: int = 0
    created: int = 0
    topic: str = ""
    purpose: str = ""

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> Channel:
        return cls(
            id=str(data.get("id") or ""),
            name=str(data.get("name") or data.get("user") or data.get("id") or ""),
            is_channel=bool(data.get("is_channel")),
            is_private=bool(data.get("is_private") or data.get("is_group")),
            is_archived=bool(data.get("is_archived")),
            is_member=bool(data.get("is_member")),
            num_members=int(data.get("num_members") or 0),
            created=int(data.get("created") or 0),
            topic=str(_get(data, "topic", "value") or ""),
            purpose=str(_get(data, "purpose", "value") or ""),
        )


@dataclass(frozen=True, slots=True)
class User:
    """A workspace member or bot."""

    id: str = ""
    name: str = ""
    real_name: str = ""
    display_name: str = ""
    email: str | None = None
    is_bot: bool = False
    is_admin: bool = False
    deleted: bool = False
    tz: str | None = None

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> User:
        # ``users.info`` nests the record under "user"; list methods do not.
        nested = data.get("user")
        if isinstance(nested, Mapping):
            data = nested
        raw_profile = data.get("profile")
        profile: Mapping[str, Any] = raw_profile if isinstance(raw_profile, Mapping) else {}
        real_name = str(data.get("real_name") or profile.get("real_name") or "")
        display_name = str(profile.get("display_name") or data.get("name") or "")
        return cls(
            id=str(data.get("id") or ""),
            name=str(data.get("name") or display_name),
            real_name=real_name,
            display_name=display_name,
            email=profile.get("email") or data.get("email"),
            is_bot=bool(data.get("is_bot")),
            is_admin=bool(data.get("is_admin")),
            deleted=bool(data.get("deleted")),
            tz=data.get("tz"),
        )


@dataclass(frozen=True, slots=True)
class Reaction:
    """One emoji reaction and who left it."""

    name: str = ""
    count: int = 0
    users: Sequence[str] = field(default_factory=tuple)

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> Reaction:
        raw_users = data.get("users")
        users: Sequence[Any] = raw_users if isinstance(raw_users, Sequence) else ()
        return cls(
            name=str(data.get("name") or ""),
            count=int(data.get("count") or 0),
            users=tuple(str(u) for u in users),
        )


@dataclass(frozen=True, slots=True)
class Message:
    """A posted or retrieved message."""

    type: str = "message"
    subtype: str | None = None
    ts: str = ""
    text: str = ""
    user: str | None = None
    bot_id: str | None = None
    channel: str = ""
    thread_ts: str | None = None
    reply_count: int = 0
    reactions: Sequence[Reaction] = field(default_factory=tuple)

    @property
    def is_thread_parent(self) -> bool:
        return bool(self.reply_count) and self.thread_ts in (None, self.ts)

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> Message:
        raw_reactions = data.get("reactions")
        reactions: list[Reaction] = []
        if isinstance(raw_reactions, Sequence):
            reactions = [Reaction.from_dict(r) for r in raw_reactions if isinstance(r, Mapping)]
        return cls(
            type=str(data.get("type") or "message"),
            subtype=data.get("subtype"),
            ts=str(data.get("ts") or ""),
            text=str(data.get("text") or ""),
            user=data.get("user"),
            bot_id=data.get("bot_id"),
            channel=str(data.get("channel") or ""),
            thread_ts=data.get("thread_ts"),
            reply_count=int(data.get("reply_count") or 0),
            reactions=tuple(reactions),
        )


@dataclass(frozen=True, slots=True)
class FileInfo:
    """A file registered with Slack."""

    id: str = ""
    name: str = ""
    title: str = ""
    mimetype: str = ""
    size: int = 0
    permalink: str | None = None
    url_private: str | None = None
    channels: Sequence[str] = field(default_factory=tuple)

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> FileInfo:
        raw_channels = data.get("channels")
        channels: Sequence[Any] = raw_channels if isinstance(raw_channels, Sequence) else ()
        return cls(
            id=str(data.get("id") or ""),
            name=str(data.get("name") or ""),
            title=str(data.get("title") or data.get("name") or ""),
            mimetype=str(data.get("mimetype") or ""),
            size=int(data.get("size") or 0),
            permalink=data.get("permalink"),
            url_private=data.get("url_private"),
            channels=tuple(str(c) for c in channels),
        )


@dataclass(frozen=True, slots=True)
class Page:
    """One page of a cursor-paginated response."""

    items: Sequence[Mapping[str, Any]] = field(default_factory=tuple)
    next_cursor: str = ""

    @property
    def has_more(self) -> bool:
        return bool(self.next_cursor)

    @classmethod
    def from_dict(cls, data: Mapping[str, Any], key: str) -> Page:
        raw = data.get(key)
        items: Sequence[Mapping[str, Any]] = (
            tuple(i for i in raw if isinstance(i, Mapping)) if isinstance(raw, Sequence) else ()
        )
        return cls(
            items=items, next_cursor=str(_get(data, "response_metadata", "next_cursor") or "")
        )


__all__ = [
    "AuthInfo",
    "Channel",
    "FileInfo",
    "Message",
    "Page",
    "Reaction",
    "User",
]


def describe(items: Sequence[Any]) -> dict[str, int]:
    """Small helper used by the CLI to summarise a result set."""
    return {"count": len(items)}
