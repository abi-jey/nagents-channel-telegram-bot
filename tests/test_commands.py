import asyncio
from dataclasses import replace

import pytest
from nagents.channels import ChannelCommand
from nagents.channels import ChannelMessage
from nagents.channels import ChannelValue

from nagents_channel_telegram_bot import TelegramBot
from nagents_channel_telegram_bot._transport import Transport

from .conftest import BOT_ID
from .conftest import TOKEN
from .conftest import TelegramServer
from .conftest import message
from .conftest import ok
from .conftest import update
from .test_receive import cancel


def command_message(text: str) -> ChannelMessage:
    token = text.split(maxsplit=1)[0]
    return ChannelMessage(
        "100",
        "-100",
        "7",
        text=text,
        metadata={
            "text_source": "text",
            "entities": [{"type": "bot_command", "offset": 0, "length": len(token)}],
        },
    )


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("/sessions", ChannelCommand("sessions")),
        ("/sessions \n", ChannelCommand("sessions")),
        ("/session", ChannelCommand("session")),
        ("/session session-123", ChannelCommand("session", "session-123")),
        ("/session main", ChannelCommand("session", "main")),
        ("/session new", ChannelCommand("session", "new")),
        ("/session default", ChannelCommand("session", "default")),
        ("/new", ChannelCommand("new")),
        ("/new  Title with spaces 😀  ", ChannelCommand("new", "Title with spaces 😀")),
        ("/new\nA title", ChannelCommand("new", "A title")),
        ("/session@offline_bot main", ChannelCommand("session", "main")),
        ("/sessions@OFFLINE_BOT", ChannelCommand("sessions")),
    ],
)
async def test_commands_parse_without_transport_or_session_io(
    bot: TelegramBot, monkeypatch: pytest.MonkeyPatch, text: str, expected: ChannelCommand
) -> None:
    async def forbidden(
        transport: Transport, method: str, payload: dict[str, ChannelValue], *, timeout: int = 15
    ) -> ChannelValue:
        raise AssertionError("Command parsing must not make a request")

    monkeypatch.setattr(Transport, "request", forbidden)
    assert bot.command(command_message(text)) == expected


@pytest.mark.parametrize(
    "text",
    [
        "/unknown",
        "/SESSION",
        "/sessions extra",
        "/sessionish",
        "/session/main",
        "/session@other_bot main",
        "/session@offline_bot.extra",
        "/session@@offline_bot",
        "/session@offline_bot/extra",
        " /session main",
        "\n/session main",
        "quoted /session main",
        "`/session main`",
        "/session\u00a0main",
    ],
)
async def test_unrecognized_or_nonliteral_commands_remain_model_input(bot: TelegramBot, text: str) -> None:
    assert bot.command(command_message(text)) is None


@pytest.mark.parametrize(
    "entities",
    [
        None,
        True,
        {},
        [],
        [None],
        [{"type": "code", "offset": 0, "length": 8}],
        [{"type": "bot_command", "offset": 1, "length": 8}],
        [{"type": "bot_command", "offset": False, "length": 8}],
        [{"type": "bot_command", "offset": 0, "length": True}],
        [{"type": "bot_command", "offset": 0, "length": 7}],
        [{"type": "bot_command", "offset": 0, "length": 9}],
        [{"type": "bot_command", "offset": 0, "length": 8}] * 2,
    ],
)
async def test_present_malformed_or_mismatched_entities_never_fallback(
    bot: TelegramBot, entities: ChannelValue
) -> None:
    event = command_message("/session main")
    assert bot.command(replace(event, metadata={"entities": entities})) is None


async def test_strict_literal_fallback_only_when_entities_absent(bot: TelegramBot) -> None:
    event = replace(command_message("/session@offline_bot main"), metadata={})
    assert bot.command(event) == ChannelCommand("session", "main")
    unknown_identity = TelegramBot(TOKEN)
    assert unknown_identity.command(event) is None
    assert unknown_identity.command(replace(event, text="/session main")) == ChannelCommand("session", "main")


async def test_malformed_unicode_and_entity_ranges_are_not_commands(bot: TelegramBot) -> None:
    assert bot.command(command_message("/new \ud800")) is None
    event = command_message("/new 😀")
    metadata: dict[str, ChannelValue] = {
        "entities": [
            {"type": "bot_command", "offset": 0, "length": 4},
            {"type": "bold", "offset": 5, "length": 2},
        ]
    }
    assert bot.command(replace(event, metadata=metadata)) == ChannelCommand("new", "😀")
    assert bot.command(replace(event, text="/new a", metadata=metadata)) is None


@pytest.mark.parametrize("kind", ["edited_message", "channel_post", "edited_channel_post", "callback_query"])
async def test_only_new_chat_messages_are_commands(bot: TelegramBot, kind: str) -> None:
    assert bot.command(replace(command_message("/new"), event_type=kind)) is None


@pytest.mark.parametrize(
    "metadata",
    [
        {"forward_origin": {"type": "hidden_user"}},
        {"forward_origin": None},
        {"forward_date": 1},
        {"forward_from": {"id": 1}},
        {"forward_from_chat": {"id": -100}},
        {"forward_sender_name": "original"},
        {"is_automatic_forward": True},
        {"edit_date": 123},
        {"text_source": "caption"},
    ],
)
async def test_forwarded_edited_or_caption_text_is_not_a_host_command(
    bot: TelegramBot, metadata: dict[str, ChannelValue]
) -> None:
    event = command_message("/new")
    assert bot.command(replace(event, metadata={**event.metadata, **metadata})) is None


async def test_entity_provenance_survives_polling_but_unknown_fields_do_not(
    bot: TelegramBot, server: TelegramServer
) -> None:
    body: dict[str, ChannelValue] = {
        **message(),
        "text": "/session@offline_bot main",
        "entities": [
            {
                "type": "bot_command",
                "offset": 0,
                "length": 20,
                "token": "do-not-copy",
                "url": "https://unused.invalid",
            }
        ],
    }
    caption: dict[str, ChannelValue] = {
        **message(11),
        "text": "",
        "caption": "/new",
        "caption_entities": [
            {
                "type": "bot_command",
                "offset": 0,
                "length": 4,
            }
        ],
    }
    forwarded: dict[str, ChannelValue] = {
        **body,
        "forward_origin": {"type": "hidden_user", "sender_user_name": "Original", "date": 1},
    }
    server.responses["getUpdates"].append(
        ok(
            [
                update(100, body=body),
                update(101, body=caption),
                update(102, body=forwarded),
            ]
        )
    )
    events: list[ChannelMessage] = []

    async def receive(event: ChannelMessage) -> None:
        events.append(event)

    task = asyncio.create_task(bot.listen(receive))
    try:
        await server.next_poll()
        await server.next_poll()
        assert events[0].metadata["entities"] == [{"type": "bot_command", "offset": 0, "length": 20}]
        assert "do-not-copy" not in repr(events)
        assert "unused.invalid" not in repr(events)
        assert bot.command(events[0]) == ChannelCommand("session", "main")
        assert bot.command(events[1]) is None and events[1].metadata["text_source"] == "caption"
        assert bot.command(events[2]) is None
        assert server.calls("sendMessage") == []
    finally:
        await cancel(task)


async def test_optional_username_in_getme_remains_compatible(server: TelegramServer) -> None:
    server.responses["getMe"].append(ok({"id": BOT_ID, "is_bot": True}))
    bot = TelegramBot(TOKEN, base_url=server.origin)
    try:
        await bot.open()
        assert bot.command(command_message("/new")) == ChannelCommand("new")
        assert bot.command(command_message("/new@offline_bot")) is None
    finally:
        await bot.close()
