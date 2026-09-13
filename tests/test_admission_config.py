import re
from typing import TYPE_CHECKING
from typing import cast

import pytest
from nagents.channels import ChannelError
from nagents.channels import ChannelValue

from nagents_channel_telegram_bot import TelegramBot
from nagents_channel_telegram_bot import plugin

from .conftest import TOKEN

if TYPE_CHECKING:
    from collections.abc import Sequence


@pytest.mark.parametrize("field", ["allowed_user_ids", "allowed_usernames"])
@pytest.mark.parametrize("value", [None, True, 7, 7.0, "offline_user", {}, [True], [7], [None], [[]], [{}]])
def test_user_lists_reject_wrong_config_types(field: str, value: ChannelValue) -> None:
    with pytest.raises(ChannelError):
        plugin({"token": TOKEN, field: value})


@pytest.mark.parametrize(
    "value",
    ["", "0", "-7", "+7", "07", "7.0", " 7", "7 ", "7\n", "\uff17", "\u0667", str(2**63), "1" * 100],
)
def test_user_ids_reject_noncanonical_or_out_of_range_values(value: str) -> None:
    with pytest.raises(ChannelError):
        plugin({"token": TOKEN, "allowed_user_ids": [value]})
    with pytest.raises(ChannelError):
        TelegramBot(TOKEN, allowed_user_ids=[value])


@pytest.mark.parametrize(
    "value",
    [
        "",
        "@",
        "test",
        "@test",
        "a" * 33,
        "@" + "a" * 33,
        "@@offline_user",
        " offline_user",
        "offline_user ",
        "offline_user\n",
        "off\nline",
        "off-line",
        "off.line",
        "t.me/offline_user",
        "\u043effline_user",
        "\uff4fffline_user",
        "offl\u0130ne",
        "offl\u0131ne",
        "offl\u212ane",
        "offliné",
        "offli\u200bne",
        "😀test",
    ],
)
def test_usernames_reject_malformed_ascii_lengths_and_confusables(value: str) -> None:
    with pytest.raises(ChannelError):
        plugin({"token": TOKEN, "allowed_usernames": [value]})
    with pytest.raises(ChannelError):
        TelegramBot(TOKEN, allowed_usernames=[value])


@pytest.mark.parametrize("value", [None, 0, 1, 0.0, "true", "false", [], {}])
def test_private_flag_requires_actual_boolean(value: ChannelValue) -> None:
    with pytest.raises(ChannelError, match="private_chats_only"):
        plugin({"token": TOKEN, "private_chats_only": value})
    with pytest.raises(ChannelError, match="private_chats_only"):
        TelegramBot(TOKEN, private_chats_only=cast("bool", value))


@pytest.mark.parametrize("value", [None, True, 7, "offline_user", {"offline_user"}, {"offline_user": True}])
def test_constructor_rejects_non_list_tuple_user_collections(value: object) -> None:
    entries = cast("Sequence[str]", value)
    with pytest.raises(ChannelError, match="allowed_user_ids"):
        TelegramBot(TOKEN, allowed_user_ids=entries)
    with pytest.raises(ChannelError, match="allowed_usernames"):
        TelegramBot(TOKEN, allowed_usernames=entries)


@pytest.mark.parametrize("value", [True, 7, None, [], {}])
def test_constructor_rejects_non_string_user_entries(value: object) -> None:
    entries = cast("list[str]", [value])
    with pytest.raises(ChannelError):
        TelegramBot(TOKEN, allowed_user_ids=entries)
    with pytest.raises(ChannelError):
        TelegramBot(TOKEN, allowed_usernames=entries)


def test_factory_requires_json_lists_but_constructor_supports_tuples() -> None:
    for field in ("allowed_user_ids", "allowed_usernames"):
        with pytest.raises(ChannelError):
            plugin({"token": TOKEN, field: cast("ChannelValue", ())})
    channel = TelegramBot(TOKEN, allowed_user_ids=("7",), allowed_usernames=("@OFFLINE_USER",))
    assert channel._allowed_users == frozenset({"7"})
    assert channel._allowed_usernames == frozenset({"offline_user"})


def test_factory_normalizes_copies_and_deduplicates_config() -> None:
    ids: list[ChannelValue] = ["7", str(2**63 - 1), "7"]
    names: list[ChannelValue] = ["@OFFLINE_USER", "offline_user", "A" * 5, "@" + "B" * 32]
    channel = plugin({"token": TOKEN, "allowed_user_ids": ids, "allowed_usernames": names, "private_chats_only": True})
    assert isinstance(channel, TelegramBot)
    ids.clear()
    names.clear()
    assert channel._allowed_users == frozenset({"7", str(2**63 - 1)})
    assert channel._allowed_usernames == frozenset({"offline_user", "a" * 5, "b" * 32})
    assert channel._private_chats_only is True


def test_new_filter_defaults_are_unconstrained_in_factory_and_constructor() -> None:
    for channel in (plugin({"token": TOKEN}), TelegramBot(TOKEN)):
        assert isinstance(channel, TelegramBot)
        assert not channel._allowed_users
        assert not channel._allowed_usernames
        assert channel._private_chats_only is False


@pytest.mark.parametrize(
    ("field", "valid", "invalid"),
    [
        (
            "allowed_usernames",
            ["abcde", "@OFFLINE_USER", "x" * 32, "@" + "x" * 32],
            ["test", "x" * 33, "@@offline_user", "off-line", "\u043effline_user"],
        ),
        ("allowed_user_ids", ["7", str(2**63 - 1)], ["0", "-7", "07", "+7", "\uff17"]),
    ],
)
def test_schema_advertises_strict_user_items(field: str, valid: list[str], invalid: list[str]) -> None:
    properties = plugin.config_schema["properties"]
    assert isinstance(properties, dict)
    schema = properties[field]
    assert isinstance(schema, dict) and schema["type"] == "array" and schema["default"] == []
    items = schema["items"]
    assert isinstance(items, dict) and items["type"] == "string"
    pattern = items["pattern"]
    assert isinstance(pattern, str)
    assert all(re.fullmatch(pattern, value) for value in valid)
    assert all(re.fullmatch(pattern, value) is None for value in invalid)
    private = properties["private_chats_only"]
    assert isinstance(private, dict) and private["type"] == "boolean" and private["default"] is False
