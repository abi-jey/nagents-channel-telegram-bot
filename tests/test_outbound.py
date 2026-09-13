import asyncio
import traceback
from dataclasses import replace
from typing import cast

import pytest
from aiohttp import web
from nagents.channels import ChannelAttachment
from nagents.channels import ChannelError
from nagents.channels import ChannelSend
from nagents.channels import ChannelValue

from nagents_channel_telegram_bot import TelegramBot

from .conftest import TOKEN
from .conftest import TelegramServer
from .conftest import failure
from .conftest import message
from .conftest import ok


async def test_send_preserves_plain_text_thread_and_strict_reply(bot: TelegramBot, server: TelegramServer) -> None:
    sent = await bot.send(ChannelSend(destination="-100", text="*literal* <html>", thread_id="42", reply_to="10"))
    assert sent.message_ids == ("900",)
    assert sent.metadata == {"destination": "-100", "thread_id": "42", "reply_to": "10"}
    assert server.calls("sendMessage") == [
        {
            "chat_id": "-100",
            "text": "*literal* <html>",
            "message_thread_id": 42,
            "reply_parameters": {"message_id": 10, "allow_sending_without_reply": False},
        }
    ]


@pytest.mark.parametrize("content", ["a" * 4096, "😀" * 2048])
async def test_utf16_boundary_accepted(bot: TelegramBot, content: str) -> None:
    assert (await bot.send(ChannelSend("-100", content))).message_ids == ("900",)


@pytest.mark.parametrize("content", ["a" * 4097, "😀" * 2049, "a" + "😀" * 2048, "", "\ud800"])
async def test_invalid_text_rejected_without_http(bot: TelegramBot, server: TelegramServer, content: str) -> None:
    with pytest.raises(ChannelError):
        await bot.send(ChannelSend("-100", content))
    assert server.calls("sendMessage") == []


@pytest.mark.parametrize("bad", [True, False, 1, [], {}, "0", "01", "+1", " 1", "1 ", "@chat", "1.0", str(2**63)])
@pytest.mark.parametrize("field", ["destination", "thread_id", "reply_to"])
async def test_invalid_routing_never_falls_back(
    bot: TelegramBot, server: TelegramServer, bad: object, field: str
) -> None:
    # Deliberately violate the typed API to verify runtime model/tool validation.
    value = cast("str", bad)
    outgoing = ChannelSend("-100", "hello")
    if field == "destination":
        outgoing = replace(outgoing, destination=value)
    elif field == "thread_id":
        outgoing = replace(outgoing, thread_id=value)
    else:
        outgoing = replace(outgoing, reply_to=value)
    with pytest.raises(ChannelError):
        await bot.send(outgoing)
    assert server.calls("sendMessage") == []


async def test_unsupported_outbound_options(bot: TelegramBot, server: TelegramServer) -> None:
    with pytest.raises(ChannelError, match="attachments"):
        await bot.send(ChannelSend("-100", "hello", attachments=(ChannelAttachment("telegram:file:x"),)))
    with pytest.raises(ChannelError, match="metadata"):
        await bot.send(ChannelSend("-100", "hello", metadata={"parse_mode": "HTML"}))
    assert server.calls("sendMessage") == []


async def test_actions_match_advertised_schemas(bot: TelegramBot, server: TelegramServer) -> None:
    for action in bot.actions:
        assert action.parameters["additionalProperties"] is False
    assert {action.name for action in bot.actions} == {"edit_message", "delete_message"}
    assert await bot.action("edit_message", {"destination": "-100", "message_id": "10", "text": "New *text*"}) == {
        "ok": True,
        "destination": "-100",
        "message_id": "10",
    }
    assert server.calls("editMessageText") == [{"chat_id": "-100", "message_id": 10, "text": "New *text*"}]
    assert await bot.action("delete_message", {"destination": "-100", "message_id": "10"}) == {
        "ok": True,
        "destination": "-100",
        "message_id": "10",
    }
    assert server.calls("deleteMessage") == [{"chat_id": "-100", "message_id": 10}]


@pytest.mark.parametrize(
    ("name", "arguments"),
    [
        ("unknown", {}),
        ("delete_message", {}),
        ("delete_message", {"destination": "-100", "message_id": "10", "thread_id": "4"}),
        ("edit_message", {"destination": "-100", "message_id": "10"}),
        ("edit_message", {"destination": "-100", "message_id": "10", "text": "hello", "parse_mode": "HTML"}),
        ("edit_message", {"destination": "-100", "message_id": "10", "text": "😀" * 2049}),
        ("delete_message", {"destination": ["-100"], "message_id": "10"}),
        ("delete_message", {"destination": "-100", "message_id": True}),
        ("delete_message", {"destination": "-100", "message_id": 10}),
        ("delete_message", {"destination": "-100", "message_id": "0"}),
        ("delete_message", {"destination": "-100", "message_id": "-10"}),
    ],
)
async def test_action_arguments_validated(
    bot: TelegramBot, server: TelegramServer, name: str, arguments: dict[str, ChannelValue]
) -> None:
    with pytest.raises(ChannelError):
        await bot.action(name, arguments)
    assert server.calls("editMessageText") == []
    assert server.calls("deleteMessage") == []


@pytest.mark.parametrize("method", ["sendMessage", "editMessageText", "deleteMessage"])
@pytest.mark.parametrize("status", [400, 401, 403, 409, 429, 500, 503])
async def test_outbound_never_retries_and_sanitizes_errors(
    bot: TelegramBot, server: TelegramServer, method: str, status: int
) -> None:
    server.responses[method].append(failure(status, delay=12))
    with pytest.raises(ChannelError) as raised:
        if method == "sendMessage":
            await bot.send(ChannelSend("-100", "hello", thread_id="10"))
        elif method == "editMessageText":
            await bot.action("edit_message", {"destination": "-100", "message_id": "10", "text": "new"})
        else:
            await bot.action("delete_message", {"destination": "-100", "message_id": "10"})
    assert raised.value.outcome_unknown is (status >= 500)
    if status == 429:
        assert raised.value.retry_after == 12
    rendered = "".join(traceback.format_exception(raised.value))
    assert TOKEN not in rendered
    assert "secret.invalid" not in rendered
    assert "SECRET" not in rendered
    assert len(server.calls(method)) == 1


@pytest.mark.parametrize("method", ["sendMessage", "editMessageText", "deleteMessage"])
async def test_uncertain_transport_failure_not_retried(bot: TelegramBot, server: TelegramServer, method: str) -> None:
    async def disconnect(request: web.Request) -> web.StreamResponse:
        assert request.transport is not None
        request.transport.close()
        return web.Response()

    server.responses[method].append(disconnect)
    with pytest.raises(ChannelError) as raised:
        if method == "sendMessage":
            await bot.send(ChannelSend("-100", "hello"))
        elif method == "editMessageText":
            await bot.action("edit_message", {"destination": "-100", "message_id": "10", "text": "new"})
        else:
            await bot.action("delete_message", {"destination": "-100", "message_id": "10"})
    assert raised.value.outcome_unknown
    assert TOKEN not in "".join(traceback.format_exception(raised.value))
    assert len(server.calls(method)) == 1


@pytest.mark.parametrize("result", [True, {}, {"message_id": True}, message(0), message(900, chat_id=-200)])
async def test_malformed_send_confirmation_is_unknown(
    bot: TelegramBot, server: TelegramServer, result: ChannelValue
) -> None:
    server.responses["sendMessage"].append(ok(result))
    with pytest.raises(ChannelError) as raised:
        await bot.send(ChannelSend("-100", "hello"))
    assert raised.value.outcome_unknown
    assert len(server.calls("sendMessage")) == 1


async def test_redirect_not_followed(bot: TelegramBot, server: TelegramServer) -> None:
    server.responses["sendMessage"].append(
        web.Response(
            status=307,
            headers={
                "Location": f"{server.origin}/bot{TOKEN}/deleteMessage",
            },
        )
    )
    with pytest.raises(ChannelError) as raised:
        await bot.send(ChannelSend("-100", "hello"))
    assert raised.value.outcome_unknown
    assert server.calls("deleteMessage") == []
    assert len(server.calls("sendMessage")) == 1


async def test_outbound_cancellation_not_retried(bot: TelegramBot, server: TelegramServer) -> None:
    entered = asyncio.Event()

    async def blocked(request: web.Request) -> web.Response:
        entered.set()
        await asyncio.Event().wait()
        return ok(message(900))

    server.responses["sendMessage"].append(blocked)
    task = asyncio.create_task(bot.send(ChannelSend("-100", "hello")))
    await asyncio.wait_for(entered.wait(), 2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert len(server.calls("sendMessage")) == 1
