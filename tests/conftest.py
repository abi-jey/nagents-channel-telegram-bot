import asyncio
import socket
from collections import defaultdict
from collections import deque
from collections.abc import AsyncIterator
from collections.abc import Awaitable
from collections.abc import Callable
from dataclasses import dataclass
from dataclasses import field

import pytest
from aiohttp import web
from nagents.channels import ChannelValue

from nagents_channel_telegram_bot import TelegramBot
from nagents_channel_telegram_bot._validation import json_value
from nagents_channel_telegram_bot._validation import object_value

TOKEN = "123456:OFFLINE_TEST_TOKEN"
BOT_ID = 123456
BOT_USERNAME = "offline_bot"
ResponseFactory = Callable[[web.Request], Awaitable[web.StreamResponse]]


def ok(result: ChannelValue) -> web.Response:
    return web.json_response({"ok": True, "result": result})


def failure(status: int, *, delay: int = 0) -> web.Response:
    return web.json_response(
        {
            "ok": False,
            "error_code": status,
            "description": f"SECRET {TOKEN} https://secret.invalid/bot{TOKEN}",
            "parameters": {"retry_after": delay},
        },
        status=status,
    )


def message(message_id: int = 10, *, chat_id: int = -100, sender_id: int = 7) -> dict[str, ChannelValue]:
    return {
        "message_id": message_id,
        "date": 1700000000,
        "chat": {"id": chat_id, "type": "supergroup", "title": "Test group"},
        "from": {"id": sender_id, "is_bot": sender_id == BOT_ID, "first_name": "Test sender"},
        "text": "Hello",
    }


def update(update_id: int = 100, *, kind: str = "message", body: ChannelValue = None) -> dict[str, ChannelValue]:
    return {"update_id": update_id, kind: message() if body is None else body}


@dataclass
class TelegramServer:
    origin: str = ""
    requests: list[tuple[str, dict[str, ChannelValue]]] = field(default_factory=list)
    responses: dict[str, deque[web.StreamResponse | ResponseFactory]] = field(
        default_factory=lambda: defaultdict(deque)
    )
    polls: asyncio.Queue[dict[str, ChannelValue]] = field(default_factory=asyncio.Queue)
    idle: asyncio.Event = field(default_factory=asyncio.Event)

    async def handle(self, request: web.Request) -> web.StreamResponse:
        assert request.match_info["token"] == TOKEN
        method = request.match_info["method"]
        raw: object = await request.json()
        payload = object_value(json_value(raw))
        self.requests.append((method, payload))
        if method == "getUpdates":
            self.polls.put_nowait(payload)
        if self.responses[method]:
            response = self.responses[method].popleft()
            return await response(request) if callable(response) else response
        if method == "getMe":
            return ok({"id": BOT_ID, "is_bot": True, "first_name": "Offline bot", "username": BOT_USERNAME})
        if method == "getUpdates":
            await self.idle.wait()
            return ok([])
        if method in ("answerCallbackQuery", "deleteMessage", "sendChatAction"):
            return ok(True)
        if method in ("sendMessage", "editMessageText"):
            chat_id = payload["chat_id"]
            assert isinstance(chat_id, str)
            remote_id = payload.get("message_id", 900)
            assert type(remote_id) is int
            return ok(message(remote_id, chat_id=int(chat_id), sender_id=BOT_ID))
        raise AssertionError(f"Unexpected method: {method}")

    def calls(self, method: str) -> list[dict[str, ChannelValue]]:
        return [payload for name, payload in self.requests if name == method]

    async def next_poll(self) -> dict[str, ChannelValue]:
        return await asyncio.wait_for(self.polls.get(), timeout=2)


@pytest.fixture
async def server() -> AsyncIterator[TelegramServer]:
    fake = TelegramServer()
    app = web.Application()
    app.router.add_post("/bot{token}/{method}", fake.handle)
    runner = web.AppRunner(app, access_log=None, shutdown_timeout=0.01)
    await runner.setup()
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    sock.setblocking(False)
    fake.origin = f"http://127.0.0.1:{sock.getsockname()[1]}"
    site = web.SockSite(runner, sock)
    await site.start()
    try:
        yield fake
    finally:
        await runner.cleanup()


@pytest.fixture
async def bot(server: TelegramServer) -> AsyncIterator[TelegramBot]:
    channel = TelegramBot(TOKEN, base_url=server.origin, poll_timeout=1)
    await channel.open()
    try:
        yield channel
    finally:
        await channel.close()


@pytest.fixture
def retry_delays(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    delays: list[float] = []

    async def sleep(delay: float) -> None:
        delays.append(delay)

    # Patch only the transport module's namespace, not asyncio itself.
    @dataclass
    class Clock:
        sleep: Callable[[float], Awaitable[None]]

    monkeypatch.setattr("nagents_channel_telegram_bot._transport.asyncio", Clock(sleep))
    return delays
