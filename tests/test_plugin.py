import traceback
from importlib.metadata import entry_points

import pytest
from nagents.channels import ChannelError
from nagents.channels import ChannelPlugin
from nagents.channels import ChannelValue
from nagents.channels import load_channel

from nagents_channel_telegram_bot import TelegramBot
from nagents_channel_telegram_bot import plugin
from nagents_channel_telegram_bot._transport import Transport

from .conftest import TOKEN


def test_descriptor_entry_point_exposes_flat_secret_aware_schema(monkeypatch: pytest.MonkeyPatch) -> None:
    def no_transport(transport: Transport) -> None:
        raise AssertionError("Discovery and configuration must stay offline")

    monkeypatch.setattr(Transport, "open", no_transport)
    (entry_point,) = entry_points(group="nagents.channels", name="telegram-bot")
    descriptor = entry_point.load()
    assert descriptor is plugin
    assert isinstance(descriptor, ChannelPlugin)
    assert descriptor.name == "Telegram Bot"
    assert callable(descriptor)
    schema = descriptor.config_schema
    assert schema["type"] == "object" and schema["additionalProperties"] is False
    properties = schema["properties"]
    assert isinstance(properties, dict)
    assert set(properties) == {
        "name",
        "token",
        "token_env",
        "allowed_chat_ids",
        "allowed_user_ids",
        "allowed_usernames",
        "private_chats_only",
        "poll_timeout",
    }
    token_schema = properties["token"]
    assert isinstance(token_schema, dict) and token_schema["writeOnly"] is True
    env_schema = properties["token_env"]
    assert isinstance(env_schema, dict) and "default" not in env_schema
    assert "default" not in token_schema
    channel = load_channel("telegram-bot", {"token": TOKEN, "name": "web-connection", "allowed_chat_ids": ["-100"]})
    assert isinstance(channel, TelegramBot) and channel.name == "web-connection"
    assert TOKEN not in repr(plugin)
    assert TOKEN not in repr(schema)
    assert TOKEN not in repr(channel)


def test_direct_private_token_does_not_read_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "invalid environment token")
    channel = plugin({"token": TOKEN})
    assert isinstance(channel, TelegramBot)
    assert channel.name == "telegram"


def test_explicit_environment_and_default_environment_remain_supported(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", TOKEN)
    monkeypatch.setenv("PRIVATE_BOT_TOKEN", TOKEN)
    assert isinstance(plugin({}), TelegramBot)
    assert isinstance(plugin({"token_env": "PRIVATE_BOT_TOKEN"}), TelegramBot)


@pytest.mark.parametrize("value", [None, True, 123, [], {}, "", "123:secret/path", "123:secret\n"])
def test_invalid_direct_token_is_sanitized(value: ChannelValue) -> None:
    with pytest.raises(ChannelError) as raised:
        TelegramBot.from_config({"token": value})
    assert "secret" not in "".join(traceback.format_exception(raised.value))


@pytest.mark.parametrize("token_env", ["TELEGRAM_BOT_TOKEN", "", None, 12])
@pytest.mark.parametrize("token", [TOKEN, "", None])
def test_both_credential_keys_always_rejected(token: ChannelValue, token_env: ChannelValue) -> None:
    with pytest.raises(ChannelError, match="not both") as raised:
        plugin({"token": token, "token_env": token_env})
    assert TOKEN not in "".join(traceback.format_exception(raised.value))
