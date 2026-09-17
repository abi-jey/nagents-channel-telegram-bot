import asyncio
from dataclasses import dataclass
from dataclasses import field
from dataclasses import replace
from typing import cast

import pytest
from aiohttp import web
from nagents.channels import ChannelActivity
from nagents.channels import ChannelError
from nagents.channels import ChannelMessage
from nagents.channels import ChannelValue

from nagents_channel_telegram_bot import TelegramBot
from nagents_channel_telegram_bot._activity import MAX_TYPING_TARGETS
from nagents_channel_telegram_bot._transport import Transport
from tests.hang_guard import HANG_GUARD

from .conftest import TOKEN
from .conftest import TelegramServer
from .conftest import failure
from .conftest import ok
from .test_receive import cancel


@dataclass
class SleepGate:
    delay: float
    release: asyncio.Event = field(default_factory=asyncio.Event)
    cancelled: asyncio.Event = field(default_factory=asyncio.Event)
    allow_cancel: asyncio.Event = field(default_factory=asyncio.Event)


class Clock:
    def __init__(self) -> None:
        self.now = 100.0
        self.waits: asyncio.Queue[SleepGate] = asyncio.Queue()

    def time(self) -> float:
        return self.now

    async def sleep(self, delay: float) -> None:
        gate = SleepGate(delay)
        gate.allow_cancel.set()
        self.waits.put_nowait(gate)
        try:
            await gate.release.wait()
        except asyncio.CancelledError:
            gate.cancelled.set()
            await gate.allow_cancel.wait()
            raise

    async def next_sleep(self) -> SleepGate:
        return await asyncio.wait_for(self.waits.get(), HANG_GUARD)

    def advance(self, gate: SleepGate) -> None:
        self.now += gate.delay
        gate.release.set()


@pytest.fixture
def clock(bot: TelegramBot) -> Clock:
    clock = Clock()
    bot._typing._sleep = clock.sleep
    bot._typing._clock = clock.time
    return clock


async def test_typing_immediate_idempotent_periodic_and_local_stop(
    bot: TelegramBot, server: TelegramServer, clock: Clock
) -> None:
    active = ChannelActivity("-100", True, thread_id="42", session_id="session-a")
    await bot.activity(active)
    first = await clock.next_sleep()
    assert first.delay == 4
    assert server.calls("sendChatAction") == [{"chat_id": "-100", "action": "typing", "message_thread_id": 42}]
    await bot.activity(active)
    assert len(server.calls("sendChatAction")) == 1
    clock.advance(first)
    second = await clock.next_sleep()
    assert second.delay == 4 and len(server.calls("sendChatAction")) == 2
    await bot.activity(replace(active, active=False))
    assert second.cancelled.is_set()
    assert bot._typing._entries == {}
    await bot.activity(replace(active, active=False))
    assert len(server.calls("sendChatAction")) == 2
    assert server.calls("sendMessage") == []


async def test_new_session_replaces_owner_and_late_stop_does_not_stop_it(
    bot: TelegramBot, server: TelegramServer, clock: Clock
) -> None:
    old = ChannelActivity("-100", True, session_id="old")
    new = replace(old, session_id="new")
    await bot.activity(old)
    old_wait = await clock.next_sleep()
    await bot.activity(new)
    new_wait = await clock.next_sleep()
    assert old_wait.cancelled.is_set()
    assert len(server.calls("sendChatAction")) == 2
    await bot.activity(replace(old, active=False))
    await bot.activity(ChannelActivity("-100", False))
    assert not new_wait.cancelled.is_set()
    await bot.activity(new)
    assert len(server.calls("sendChatAction")) == 2
    await bot.activity(replace(new, active=False))
    assert new_wait.cancelled.is_set()


async def test_chat_and_thread_keys_are_independent(bot: TelegramBot, server: TelegramServer, clock: Clock) -> None:
    first = ChannelActivity("-100", True, thread_id="42", session_id="same")
    second = replace(first, thread_id="43")
    third = replace(first, conversation_id="200")
    for event in (first, second, third):
        await bot.activity(event)
    waits = [await clock.next_sleep() for _ in range(3)]
    await bot.activity(replace(second, active=False))
    assert [gate.cancelled.is_set() for gate in waits] == [False, True, False]
    assert len(server.calls("sendChatAction")) == 3


@pytest.mark.parametrize("status", [400, 401, 403, 409, 429, 500])
async def test_indicator_errors_are_best_effort_and_only_the_next_period_repeats(
    bot: TelegramBot, server: TelegramServer, clock: Clock, caplog: pytest.LogCaptureFixture, status: int
) -> None:
    server.responses["sendChatAction"].append(failure(status, delay=12 if status == 429 else 0))
    event = ChannelActivity("-100", True, session_id="run")
    await bot.activity(event)
    wait = await clock.next_sleep()
    assert wait.delay == (12 if status == 429 else 4)
    assert len(server.calls("sendChatAction")) == 1
    assert TOKEN not in caplog.text
    clock.advance(wait)
    again = await clock.next_sleep()
    assert again.delay == 4
    assert len(server.calls("sendChatAction")) == 2


async def test_rate_limit_survives_session_handoff_and_applies_to_new_targets(
    bot: TelegramBot, server: TelegramServer, clock: Clock
) -> None:
    server.responses["sendChatAction"].append(failure(429, delay=120))
    old = ChannelActivity("-100", True, session_id="old")
    await bot.activity(old)
    old_wait = await clock.next_sleep()
    assert old_wait.delay == 120
    await bot.activity(replace(old, session_id="new"))
    new_wait = await clock.next_sleep()
    assert new_wait.delay == 120 and old_wait.cancelled.is_set()
    await bot.activity(ChannelActivity("200", True, session_id="another"))
    other_wait = await clock.next_sleep()
    assert other_wait.delay == 120
    assert len(server.calls("sendChatAction")) == 1
    clock.advance(new_wait)
    next_wait = await clock.next_sleep()
    assert next_wait.delay == 4 and len(server.calls("sendChatAction")) == 2


async def test_unexpected_indicator_error_never_leaks_or_fails_activity(
    bot: TelegramBot, clock: Clock, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    async def broken(
        transport: Transport, method: str, payload: dict[str, ChannelValue], *, timeout: int = 15
    ) -> ChannelValue:
        assert method == "sendChatAction" and timeout == 5
        raise RuntimeError(f"https://private.invalid/bot{TOKEN}")

    monkeypatch.setattr(Transport, "request", broken)
    await bot.activity(ChannelActivity("-100", True))
    assert (await clock.next_sleep()).delay == 4
    assert TOKEN not in caplog.text


@pytest.mark.parametrize("bad", [True, 1, [], "0", "01", "1.0", "@chat", str(2**63)])
@pytest.mark.parametrize("field_name", ["conversation_id", "thread_id"])
async def test_activity_routing_is_validated_before_http(
    bot: TelegramBot, server: TelegramServer, bad: object, field_name: str
) -> None:
    event = ChannelActivity("-100", True)
    value = cast("str", bad)
    event = (
        replace(event, conversation_id=value) if field_name == "conversation_id" else replace(event, thread_id=value)
    )
    with pytest.raises(ChannelError):
        await bot.activity(event)
    assert server.calls("sendChatAction") == []


async def test_activity_other_fields_are_strict(bot: TelegramBot, server: TelegramServer) -> None:
    with pytest.raises(ChannelError, match="active"):
        await bot.activity(ChannelActivity("-100", cast("bool", 1)))
    with pytest.raises(ChannelError, match="session_id"):
        await bot.activity(ChannelActivity("-100", True, session_id=cast("str", [])))
    assert server.calls("sendChatAction") == []


async def test_disallowed_or_closed_activity_does_not_create_tasks(server: TelegramServer) -> None:
    bot = TelegramBot(TOKEN, base_url=server.origin, allowed_chat_ids=["-100"])
    await bot.activity(ChannelActivity("-100", True))
    assert bot._typing._entries == {}
    await bot.open()
    try:
        await bot.activity(ChannelActivity("200", True))
        assert bot._typing._entries == {}
    finally:
        await bot.close()
    await bot.activity(ChannelActivity("-100", True))
    assert bot._typing._entries == {}
    assert server.calls("sendChatAction") == []


async def test_typing_worker_count_is_bounded(bot: TelegramBot, server: TelegramServer, clock: Clock) -> None:
    await asyncio.gather(
        *(
            bot.activity(ChannelActivity(str(index + 1), True, session_id="same"))
            for index in range(MAX_TYPING_TARGETS + 1)
        )
    )
    waits = [await clock.next_sleep() for _ in range(MAX_TYPING_TARGETS)]
    assert len(bot._typing._entries) == MAX_TYPING_TARGETS
    assert len(server.calls("sendChatAction")) == MAX_TYPING_TARGETS
    await bot.close()
    assert bot._typing._entries == {}
    assert all(gate.cancelled.is_set() for gate in waits)


async def test_stop_cancels_inflight_first_indicator(bot: TelegramBot, server: TelegramServer) -> None:
    entered = asyncio.Event()

    async def blocked(request: web.Request) -> web.Response:
        entered.set()
        await asyncio.Event().wait()
        return ok(True)

    server.responses["sendChatAction"].append(blocked)
    event = ChannelActivity("-100", True, session_id="run")
    start = asyncio.create_task(bot.activity(event))
    await asyncio.wait_for(entered.wait(), HANG_GUARD)
    await bot.activity(replace(event, active=False))
    await asyncio.wait_for(start, HANG_GUARD)
    assert bot._typing._entries == {}
    assert len(server.calls("sendChatAction")) == 1


async def test_cancelled_start_cleans_its_owned_indicator(bot: TelegramBot, server: TelegramServer) -> None:
    entered = asyncio.Event()

    async def blocked(request: web.Request) -> web.Response:
        entered.set()
        await asyncio.Event().wait()
        return ok(True)

    server.responses["sendChatAction"].append(blocked)
    start = asyncio.create_task(bot.activity(ChannelActivity("-100", True)))
    await asyncio.wait_for(entered.wait(), HANG_GUARD)
    await cancel(start)
    assert bot._typing._entries == {}


async def test_cancelling_duplicate_start_does_not_cancel_original(
    bot: TelegramBot, server: TelegramServer, clock: Clock
) -> None:
    entered = asyncio.Event()
    release = asyncio.Event()

    async def blocked(request: web.Request) -> web.Response:
        entered.set()
        await release.wait()
        return ok(True)

    server.responses["sendChatAction"].append(blocked)
    event = ChannelActivity("-100", True)
    original = asyncio.create_task(bot.activity(event))
    await asyncio.wait_for(entered.wait(), HANG_GUARD)
    duplicate = asyncio.create_task(bot.activity(event))
    await asyncio.sleep(0)
    await cancel(duplicate)
    assert len(bot._typing._entries) == 1
    release.set()
    await asyncio.wait_for(original, HANG_GUARD)
    assert (await clock.next_sleep()).delay == 4
    assert len(server.calls("sendChatAction")) == 1


async def test_close_shields_cleanup_and_blocks_activity_during_shutdown(
    bot: TelegramBot, server: TelegramServer, clock: Clock, monkeypatch: pytest.MonkeyPatch
) -> None:
    await bot.activity(ChannelActivity("-100", True))
    wait = await clock.next_sleep()
    session = bot._transport._session
    assert session is not None
    entered = asyncio.Event()
    release = asyncio.Event()
    original = Transport.close

    async def delayed(transport: Transport) -> None:
        entered.set()
        await release.wait()
        await original(transport)

    monkeypatch.setattr(Transport, "close", delayed)
    closing = asyncio.create_task(bot.close())
    await asyncio.wait_for(entered.wait(), HANG_GUARD)
    assert wait.cancelled.is_set()
    await bot.activity(ChannelActivity("200", True))
    with pytest.raises(ChannelError, match="closing"):
        await bot.open()
    closing.cancel()
    await asyncio.sleep(0)
    closing.cancel()
    await asyncio.sleep(0)
    assert not closing.done()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await closing
    assert session.closed and bot._typing._entries == {}
    assert len(server.calls("sendChatAction")) == 1
    await asyncio.gather(bot.close(), bot.close())
    await bot.open()
    await bot.activity(ChannelActivity("-100", True))
    assert len(server.calls("sendChatAction")) == 2


async def test_close_during_session_handoff_cannot_spawn_a_new_worker(
    bot: TelegramBot, server: TelegramServer, clock: Clock
) -> None:
    await bot.activity(ChannelActivity("-100", True, session_id="old"))
    old_wait = await clock.next_sleep()
    old_wait.allow_cancel.clear()
    handoff = asyncio.create_task(bot.activity(ChannelActivity("-100", True, session_id="new")))
    await asyncio.wait_for(old_wait.cancelled.wait(), HANG_GUARD)
    closing = asyncio.create_task(bot.close())
    await asyncio.sleep(0)
    await bot.activity(ChannelActivity("200", True))
    old_wait.allow_cancel.set()
    await asyncio.wait_for(asyncio.gather(handoff, closing), HANG_GUARD)
    assert bot._typing._entries == {}
    assert len(server.calls("sendChatAction")) == 1
    assert bot._transport._session is None


async def test_typing_and_polling_share_owned_resources_but_not_tasks(
    bot: TelegramBot, server: TelegramServer, clock: Clock
) -> None:
    async def receive(event: ChannelMessage) -> None:
        raise AssertionError("No updates queued")

    poll = asyncio.create_task(bot.listen(receive))
    await server.next_poll()
    await bot.activity(ChannelActivity("-100", True))
    wait = await clock.next_sleep()
    await cancel(poll)
    assert not wait.cancelled.is_set()
    await bot.close()
    assert wait.cancelled.is_set()
    assert bot._transport._session is None


async def test_close_during_open_prevents_activity_and_drains_session(server: TelegramServer) -> None:
    entered = asyncio.Event()
    release = asyncio.Event()

    async def blocked(request: web.Request) -> web.Response:
        entered.set()
        await release.wait()
        return ok({"id": 123456, "is_bot": True, "username": "offline_bot"})

    server.responses["getMe"].append(blocked)
    bot = TelegramBot(TOKEN, base_url=server.origin)
    opening = asyncio.create_task(bot.open())
    await asyncio.wait_for(entered.wait(), HANG_GUARD)
    session = bot._transport._session
    assert session is not None
    closing = asyncio.create_task(bot.close())
    await asyncio.sleep(0)
    await bot.activity(ChannelActivity("-100", True))
    release.set()
    with pytest.raises(ChannelError, match="closing"):
        await opening
    await asyncio.wait_for(closing, HANG_GUARD)
    assert session.closed and bot._typing._entries == {}
    assert server.calls("sendChatAction") == []
    await bot.open()
    await bot.close()


async def test_failed_open_cleanup_survives_repeated_cancellation(
    server: TelegramServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    entered = asyncio.Event()
    release = asyncio.Event()
    original = Transport.close

    async def delayed(transport: Transport) -> None:
        entered.set()
        await release.wait()
        await original(transport)

    monkeypatch.setattr(Transport, "close", delayed)
    server.responses["getMe"].append(failure(401))
    bot = TelegramBot(TOKEN, base_url=server.origin)
    opening = asyncio.create_task(bot.open())
    await asyncio.wait_for(entered.wait(), HANG_GUARD)
    session = bot._transport._session
    assert session is not None
    opening.cancel()
    await asyncio.sleep(0)
    opening.cancel()
    await asyncio.sleep(0)
    assert not opening.done()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await opening
    assert session.closed
    assert bot._typing._entries == {}
    await bot.close()
