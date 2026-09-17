import asyncio
import traceback
from collections.abc import Awaitable
from collections.abc import Callable
from dataclasses import dataclass
from importlib.metadata import entry_points
from importlib.resources import files
from typing import cast

import aiohttp
import pytest
from aiohttp import web
from nagents.channels import Channel
from nagents.channels import ChannelError
from nagents.channels import ChannelSend
from nagents.channels import ChannelValue
from nagents.channels import load_channel

from nagents_channel_telegram_bot import TelegramBot
from nagents_channel_telegram_bot._transport import Transport
from tests.hang_guard import HANG_GUARD

from .conftest import TOKEN
from .conftest import TelegramServer
from .conftest import failure
from .conftest import ok


def test_installed_plugin_is_offline(monkeypatch: pytest.MonkeyPatch) -> None:
    def no_session(*args: object, **kwargs: object) -> aiohttp.ClientSession:
        raise AssertionError("Construction must not create a session")

    monkeypatch.setattr(aiohttp, "ClientSession", no_session)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", TOKEN)
    assert len(entry_points(group="nagents.channels", name="telegram-bot")) == 1
    channel = load_channel("telegram-bot", {"name": "second-bot", "allowed_chat_ids": ["-100"]})
    assert isinstance(channel, Channel)
    assert isinstance(channel, TelegramBot)
    assert channel.name == "second-bot"
    assert files("nagents_channel_telegram_bot").joinpath("py.typed").is_file()
    assert TOKEN not in repr(channel)


@pytest.mark.parametrize(
    "config",
    [
        {"token": TOKEN, "token_env": "TELEGRAM_BOT_TOKEN"},
        {"base_url": "http://localhost"},
        {"unknown": True},
        {"token_env": ""},
        {"token_env": "a=b"},
        {"token_env": True},
        {"name": ""},
        {"name": []},
        {"name": "with space"},
        {"allowed_chat_ids": "-100"},
        {"allowed_chat_ids": [True]},
        {"allowed_chat_ids": [-100]},
        {"allowed_chat_ids": ["01"]},
        {"allowed_chat_ids": ["0"]},
        {"poll_timeout": True},
        {"poll_timeout": 0},
        {"poll_timeout": 51},
        {"poll_timeout": 1.5},
    ],
)
def test_config_rejects_invalid_values(monkeypatch: pytest.MonkeyPatch, config: dict[str, ChannelValue]) -> None:
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", TOKEN)
    with pytest.raises(ChannelError) as raised:
        TelegramBot.from_config(config)
    assert TOKEN not in str(raised.value)


def test_token_environment_and_missing_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    with pytest.raises(ChannelError, match="token"):
        TelegramBot.from_config({})
    monkeypatch.setenv("OTHER_TEST_TOKEN", TOKEN)
    assert TelegramBot.from_config({"token_env": "OTHER_TEST_TOKEN", "poll_timeout": 50}).name == "telegram"


@pytest.mark.parametrize("token", ["", "no-colon", "123:token/path", "123:token?query", "123:token\n"])
def test_token_path_injection_rejected(token: str) -> None:
    with pytest.raises(ChannelError):
        TelegramBot(token)


@pytest.mark.parametrize(
    "origin",
    [
        "http://example.com",
        "ftp://localhost",
        "https://",
        "https://example.com/path",
        "https://user:secret@example.com",
        "https://example.com?token=secret",
        "https://example.com#secret",
        "https://example.com:invalid",
        "https://example.com:0",
        "https://example.com:99999",
        " https://example.com",
        "https://example.com\n",
        "http://127.0.0.1.evil.invalid",
    ],
)
def test_base_url_validation(origin: str) -> None:
    with pytest.raises(ChannelError) as raised:
        TelegramBot(TOKEN, base_url=origin)
    assert origin not in str(raised.value)


@pytest.mark.parametrize("origin", ["https://api.telegram.org", "http://localhost:8000", "http://[::1]:8000/"])
def test_trusted_origins_construct_offline(origin: str) -> None:
    TelegramBot(TOKEN, base_url=origin)


def test_constructor_runtime_types() -> None:
    with pytest.raises(ChannelError):
        TelegramBot(TOKEN, allowed_chat_ids=cast("list[str]", "-100"))
    with pytest.raises(ChannelError):
        TelegramBot(TOKEN, poll_timeout=True)


async def test_requires_open_and_idempotent_close() -> None:
    bot = TelegramBot(TOKEN)
    with pytest.raises(ChannelError, match="not open"):
        await bot.send(ChannelSend("-100", "hello"))
    with pytest.raises(ChannelError, match="not open"):
        await bot.action("delete_message", {"destination": "-100", "message_id": "10"})
    await bot.close()
    await bot.close()


@pytest.mark.parametrize(
    "bad_me",
    [
        {"id": True, "is_bot": True},
        {"id": 1, "is_bot": False},
        [],
        {"id": 1, "is_bot": True, "username": True},
        {"id": 1, "is_bot": True, "username": "invalid/username"},
    ],
)
async def test_failed_handshake_closes_owned_session(
    server: TelegramServer, monkeypatch: pytest.MonkeyPatch, bad_me: ChannelValue
) -> None:
    sessions: list[aiohttp.ClientSession] = []
    original = Transport.open

    def capture(transport: Transport) -> None:
        original(transport)
        assert transport._session is not None
        sessions.append(transport._session)

    monkeypatch.setattr(Transport, "open", capture)
    server.responses["getMe"].append(ok(bad_me))
    bot = TelegramBot(TOKEN, base_url=server.origin)
    with pytest.raises(ChannelError):
        await bot.open()
    assert sessions[0].closed
    await bot.open()
    await bot.open()
    assert len(server.calls("getMe")) == 2
    await bot.close()
    assert all(session.closed for session in sessions)


async def test_cancelled_open_closes_owned_session(server: TelegramServer, monkeypatch: pytest.MonkeyPatch) -> None:
    entered = asyncio.Event()
    sessions: list[aiohttp.ClientSession] = []
    original = Transport.open

    def capture(transport: Transport) -> None:
        original(transport)
        assert transport._session is not None
        sessions.append(transport._session)

    async def blocked(request: web.Request) -> web.Response:
        entered.set()
        await asyncio.Event().wait()
        return ok({})

    monkeypatch.setattr(Transport, "open", capture)
    server.responses["getMe"].append(blocked)
    bot = TelegramBot(TOKEN, base_url=server.origin)
    task = asyncio.create_task(bot.open())
    await asyncio.wait_for(entered.wait(), HANG_GUARD)
    with pytest.raises(ChannelError, match="already opening"):
        await bot.open()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert sessions[0].closed


async def test_read_retry_budget_and_sanitization(server: TelegramServer, retry_delays: list[float]) -> None:
    server.responses["getMe"].extend(failure(503) for _ in range(4))
    bot = TelegramBot(TOKEN, base_url=server.origin)
    with pytest.raises(ChannelError) as raised:
        await bot.open()
    assert not raised.value.outcome_unknown
    assert retry_delays == [1, 2, 4]
    assert len(server.calls("getMe")) == 4
    assert TOKEN not in "".join(traceback.format_exception(raised.value))


async def test_rate_limit_above_cap_fails_without_early_retry(
    server: TelegramServer, retry_delays: list[float]
) -> None:
    server.responses["getMe"].append(failure(429, delay=120))
    with pytest.raises(ChannelError) as raised:
        await TelegramBot(TOKEN, base_url=server.origin).open()
    assert raised.value.retry_after == 120
    assert retry_delays == []
    assert len(server.calls("getMe")) == 1


async def test_retry_sleep_cancellable(server: TelegramServer, monkeypatch: pytest.MonkeyPatch) -> None:
    entered = asyncio.Event()

    async def sleep(delay: float) -> None:
        assert delay == 60
        entered.set()
        await asyncio.Event().wait()

    @dataclass
    class Clock:
        sleep: Callable[[float], Awaitable[None]]

    monkeypatch.setattr("nagents_channel_telegram_bot._transport.asyncio", Clock(sleep))
    server.responses["getMe"].append(failure(429, delay=60))
    bot = TelegramBot(TOKEN, base_url=server.origin)
    task = asyncio.create_task(bot.open())
    await asyncio.wait_for(entered.wait(), HANG_GUARD)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert len(server.calls("getMe")) == 1


async def test_malformed_json_retry_and_permanent_non_json_error(
    server: TelegramServer, retry_delays: list[float]
) -> None:
    server.responses["getMe"].extend(
        [
            web.Response(text="invalid JSON"),
            web.Response(text="secret body", status=401),
        ]
    )
    with pytest.raises(ChannelError, match="401"):
        await TelegramBot(TOKEN, base_url=server.origin).open()
    assert retry_delays == [1]
    assert len(server.calls("getMe")) == 2


async def test_http_200_api_error_code_still_permanent(server: TelegramServer, retry_delays: list[float]) -> None:
    server.responses["getMe"].append(web.json_response({"ok": False, "error_code": 403, "description": TOKEN}))
    with pytest.raises(ChannelError, match="403"):
        await TelegramBot(TOKEN, base_url=server.origin).open()
    assert retry_delays == []


async def test_oversized_response_is_bounded_and_send_outcome_unknown(bot: TelegramBot, server: TelegramServer) -> None:
    server.responses["sendMessage"].append(web.Response(body=b" " * (8 * 1024 * 1024 + 1)))
    with pytest.raises(ChannelError) as raised:
        await bot.send(ChannelSend("-100", "hello"))
    assert raised.value.outcome_unknown
    assert len(server.calls("sendMessage")) == 1
