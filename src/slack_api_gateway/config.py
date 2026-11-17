"""Configuration: defaults, then a JSON file, then environment variables.

Precedence is deliberate and documented in the README: environment beats file
beats defaults, so a checked-in config file never wins over a deployment-time
secret. Nothing here reads the token from disk by default — the token comes
from ``SLACK_BOT_TOKEN`` (or ``SLACK_TOKEN``) unless passed explicitly.
"""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from .errors import ConfigError

DEFAULT_BASE_URL = "https://slack.com/api/"

ENV_TOKEN = ("SLACK_BOT_TOKEN", "SLACK_TOKEN")

_TRUTHY = {"1", "true", "yes", "on", "y"}
_FALSY = {"0", "false", "no", "off", "n"}


def _as_bool(raw: str) -> bool:
    lowered = raw.strip().lower()
    if lowered in _TRUTHY:
        return True
    if lowered in _FALSY:
        return False
    raise ConfigError(f"expected a boolean, got {raw!r}", code="config_error")


def _as_float(raw: str, field: str) -> float:
    try:
        return float(raw)
    except ValueError as exc:
        raise ConfigError(f"{field} must be a number, got {raw!r}", code="config_error") from exc


def _as_int(raw: str, field: str) -> int:
    try:
        return int(raw)
    except ValueError as exc:
        raise ConfigError(f"{field} must be an integer, got {raw!r}", code="config_error") from exc


@dataclass(frozen=True, slots=True)
class SlackConfig:
    """Immutable gateway settings.

    Attributes:
        token: Bot or user token. ``None`` means "read from the environment".
        base_url: Web API root; overridable for tests and proxies.
        timeout: Per-request timeout in seconds.
        max_attempts: Total attempts per call, including the first.
        retry_base_delay: First backoff delay in seconds.
        retry_max_delay: Backoff ceiling in seconds.
        cache_ttl: Lifetime for cached read responses; ``0`` disables the cache.
        rate_limit_enabled: Whether to pace calls against Slack's tiers.
        max_pages: Page ceiling for paginated helpers.
    """

    token: str | None = None
    base_url: str = DEFAULT_BASE_URL
    timeout: float = 30.0
    max_attempts: int = 5
    retry_base_delay: float = 0.5
    retry_max_delay: float = 30.0
    cache_ttl: float = 60.0
    rate_limit_enabled: bool = True
    max_pages: int = 25

    # -- construction ------------------------------------------------------
    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> SlackConfig:
        """Read every supported ``SLACK_*`` variable that is present."""
        source: Mapping[str, str] = os.environ if env is None else env
        values: dict[str, Any] = {}

        token = next((source[name] for name in ENV_TOKEN if source.get(name)), None)
        if token:
            values["token"] = token

        if source.get("SLACK_BASE_URL"):
            values["base_url"] = source["SLACK_BASE_URL"]
        if source.get("SLACK_TIMEOUT"):
            values["timeout"] = _as_float(source["SLACK_TIMEOUT"], "SLACK_TIMEOUT")
        if source.get("SLACK_MAX_ATTEMPTS"):
            values["max_attempts"] = _as_int(source["SLACK_MAX_ATTEMPTS"], "SLACK_MAX_ATTEMPTS")
        if source.get("SLACK_RETRY_BASE_DELAY"):
            values["retry_base_delay"] = _as_float(
                source["SLACK_RETRY_BASE_DELAY"], "SLACK_RETRY_BASE_DELAY"
            )
        if source.get("SLACK_RETRY_MAX_DELAY"):
            values["retry_max_delay"] = _as_float(
                source["SLACK_RETRY_MAX_DELAY"], "SLACK_RETRY_MAX_DELAY"
            )
        if source.get("SLACK_CACHE_TTL"):
            values["cache_ttl"] = _as_float(source["SLACK_CACHE_TTL"], "SLACK_CACHE_TTL")
        if source.get("SLACK_RATE_LIMIT"):
            values["rate_limit_enabled"] = _as_bool(source["SLACK_RATE_LIMIT"])
        if source.get("SLACK_MAX_PAGES"):
            values["max_pages"] = _as_int(source["SLACK_MAX_PAGES"], "SLACK_MAX_PAGES")

        config = cls(**values)
        config.validate()
        return config

    @classmethod
    def from_file(cls, path: str | Path) -> SlackConfig:
        """Load a JSON config document (unknown keys are rejected).

        A YAML file would need a third-party parser; this package deliberately
        ships with none, so JSON is the supported on-disk format.
        """
        config_path = Path(path)
        if not config_path.exists():
            raise ConfigError(f"config file not found: {config_path}", code="config_error")
        try:
            parsed = json.loads(config_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise ConfigError(f"config file is not valid JSON: {exc}", code="config_error") from exc
        if not isinstance(parsed, dict):
            raise ConfigError("config file must contain a JSON object", code="config_error")
        allowed = set(cls.__dataclass_fields__)
        unknown = set(parsed) - allowed
        if unknown:
            raise ConfigError(
                f"unknown config keys: {', '.join(sorted(unknown))}", code="config_error"
            )
        config = cls(**parsed)
        config.validate()
        return config

    @classmethod
    def load(
        cls, path: str | Path | None = None, *, env: Mapping[str, str] | None = None
    ) -> SlackConfig:
        """File first, then environment overrides, then validation."""
        base = cls.from_file(path) if path else cls()
        overrides = cls.from_env(env)
        supplied = {
            field: getattr(overrides, field)
            for field in cls.__dataclass_fields__
            if getattr(overrides, field) != getattr(cls(), field)
        }
        merged = replace(base, **supplied)
        merged.validate()
        return merged

    # -- validation --------------------------------------------------------
    def validate(self) -> None:
        """Raise :class:`ConfigError` on values that cannot work."""
        if not self.base_url.startswith(("http://", "https://")):
            raise ConfigError(
                f"base_url must start with http:// or https://, got {self.base_url!r}",
                code="config_error",
            )
        if self.timeout <= 0:
            raise ConfigError("timeout must be > 0", code="config_error")
        if self.max_attempts < 1:
            raise ConfigError("max_attempts must be >= 1", code="config_error")
        if self.retry_base_delay < 0 or self.retry_max_delay < 0:
            raise ConfigError("retry delays must be >= 0", code="config_error")
        if self.retry_max_delay < self.retry_base_delay:
            raise ConfigError("retry_max_delay must be >= retry_base_delay", code="config_error")
        if self.cache_ttl < 0:
            raise ConfigError("cache_ttl must be >= 0", code="config_error")
        if self.max_pages < 1:
            raise ConfigError("max_pages must be >= 1", code="config_error")

    def with_token(self, token: str | None) -> SlackConfig:
        """Return a copy carrying ``token``."""
        return replace(self, token=token)

    def to_dict(self, *, redact: bool = True) -> dict[str, Any]:
        """Serialise; the token is redacted unless ``redact=False``."""
        data: dict[str, Any] = {
            "base_url": self.base_url,
            "timeout": self.timeout,
            "max_attempts": self.max_attempts,
            "retry_base_delay": self.retry_base_delay,
            "retry_max_delay": self.retry_max_delay,
            "cache_ttl": self.cache_ttl,
            "rate_limit_enabled": self.rate_limit_enabled,
            "max_pages": self.max_pages,
        }
        if self.token is None:
            data["token"] = None
        elif redact:
            data["token"] = f"{self.token[:4]}…{self.token[-2:]}" if len(self.token) > 8 else "…"
        else:
            data["token"] = self.token
        return data


__all__ = ["DEFAULT_BASE_URL", "ENV_TOKEN", "SlackConfig"]
