"""Configuration precedence, parsing and validation."""

from __future__ import annotations

import json

import pytest

from slack_api_gateway import SlackConfig
from slack_api_gateway.errors import ConfigError


def test_defaults_are_sane() -> None:
    config = SlackConfig()
    assert config.base_url == "https://slack.com/api/"
    assert config.max_attempts == 5
    assert config.retry_max_delay >= config.retry_base_delay
    assert config.cache_ttl > 0
    config.validate()


def test_env_overrides_every_supported_field() -> None:
    config = SlackConfig.from_env(
        {
            "SLACK_BOT_TOKEN": "xoxb-from-env",
            "SLACK_BASE_URL": "https://proxy.internal/api",
            "SLACK_TIMEOUT": "7.5",
            "SLACK_MAX_ATTEMPTS": "2",
            "SLACK_RETRY_BASE_DELAY": "0.1",
            "SLACK_RETRY_MAX_DELAY": "2",
            "SLACK_CACHE_TTL": "0",
            "SLACK_RATE_LIMIT": "false",
            "SLACK_MAX_PAGES": "3",
        }
    )
    assert config.token == "xoxb-from-env"
    assert config.base_url == "https://proxy.internal/api"
    assert config.timeout == 7.5
    assert config.max_attempts == 2
    assert config.retry_base_delay == 0.1
    assert config.retry_max_delay == 2.0
    assert config.cache_ttl == 0.0
    assert config.rate_limit_enabled is False
    assert config.max_pages == 3


def test_token_prefers_bot_token_over_legacy_name() -> None:
    config = SlackConfig.from_env({"SLACK_TOKEN": "legacy", "SLACK_BOT_TOKEN": "bot"})
    assert config.token == "bot"


def test_invalid_boolean_env_is_rejected() -> None:
    with pytest.raises(ConfigError, match="expected a boolean"):
        SlackConfig.from_env({"SLACK_RATE_LIMIT": "maybe"})


def test_invalid_numeric_env_is_rejected() -> None:
    with pytest.raises(ConfigError, match="SLACK_TIMEOUT must be a number"):
        SlackConfig.from_env({"SLACK_TIMEOUT": "soon"})


def test_file_is_loaded_and_validated(tmp_path) -> None:
    path = tmp_path / "gateway.json"
    path.write_text(json.dumps({"base_url": "https://example.test/api", "max_pages": 4}))
    config = SlackConfig.from_file(path)
    assert config.base_url == "https://example.test/api"
    assert config.max_pages == 4


def test_unknown_file_keys_are_rejected(tmp_path) -> None:
    path = tmp_path / "gateway.json"
    path.write_text(json.dumps({"base_uri": "typo"}))
    with pytest.raises(ConfigError, match="unknown config keys: base_uri"):
        SlackConfig.from_file(path)


def test_missing_file_is_rejected(tmp_path) -> None:
    with pytest.raises(ConfigError, match="config file not found"):
        SlackConfig.from_file(tmp_path / "nope.json")


def test_malformed_json_is_rejected(tmp_path) -> None:
    path = tmp_path / "gateway.json"
    path.write_text("{not json")
    with pytest.raises(ConfigError, match="not valid JSON"):
        SlackConfig.from_file(path)


def test_non_object_json_is_rejected(tmp_path) -> None:
    path = tmp_path / "gateway.json"
    path.write_text("[1, 2]")
    with pytest.raises(ConfigError, match="must contain a JSON object"):
        SlackConfig.from_file(path)


def test_load_prefers_environment_over_file(tmp_path) -> None:
    path = tmp_path / "gateway.json"
    path.write_text(json.dumps({"timeout": 5.0, "max_pages": 4}))
    config = SlackConfig.load(path, env={"SLACK_TIMEOUT": "12"})
    assert config.timeout == 12.0
    assert config.max_pages == 4  # untouched file value survives


@pytest.mark.parametrize(
    "kwargs, message",
    [
        ({"base_url": "slack.com/api"}, "must start with http"),
        ({"timeout": 0}, "timeout must be > 0"),
        ({"max_attempts": 0}, "max_attempts must be >= 1"),
        ({"retry_base_delay": -1.0}, "retry delays must be >= 0"),
        ({"retry_base_delay": 5.0, "retry_max_delay": 1.0}, "retry_max_delay must be >="),
        ({"cache_ttl": -1.0}, "cache_ttl must be >= 0"),
        ({"max_pages": 0}, "max_pages must be >= 1"),
    ],
)
def test_validate_rejects_impossible_values(kwargs, message) -> None:
    with pytest.raises(ConfigError, match=message):
        SlackConfig(**kwargs).validate()


def test_token_is_redacted_by_default() -> None:
    config = SlackConfig(token="xoxb-super-secret-token")
    assert config.to_dict()["token"] == "xoxb…en"
    assert config.to_dict(redact=False)["token"] == "xoxb-super-secret-token"


def test_with_token_returns_a_copy() -> None:
    base = SlackConfig()
    swapped = base.with_token("xoxb-new")
    assert base.token is None and swapped.token == "xoxb-new"
