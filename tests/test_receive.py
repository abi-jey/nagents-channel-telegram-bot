import asyncio

import pytest
from aiohttp import web
from nagents.channels import ChannelError
from nagents.channels import ChannelMessage
from nagents.channels import ChannelValue

from nagents_channel_telegram_bot import TelegramBot
from tests.hang_guard import HANG_GUARD

from .conftest import BOT_ID
from .conftest import TOKEN
from .conftest import TelegramServer
from .conftest import failure
from .conftest import message
from .conftest import ok
from .conftest import update


async def cancel(task: asyncio.Task[None]) -> None:
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


async def test_durable_admission_backpressure_and_duplicate_offsets(bot: TelegramBot, server: TelegramServer) -> None:
    server.responses["getUpdates"].append(ok([update(100), update(100), update(101)]))
    accepted: list[str] = []
    entered = asyncio.Event()
    release = asyncio.Event()

    async def receive(event: ChannelMessage) -> None:
        if not accepted:
            entered.set()
            await release.wait()
        accepted.append(event.message_id)

    task = asyncio.create_task(bot.listen(receive))
    try:
        first = await server.next_poll()
        await asyncio.wait_for(entered.wait(), HANG_GUARD)
        assert first == {
            "offset": 0,
            "limit": 20,
            "timeout": 1,
            "allowed_updates": [
                "message",
                "edited_message",
                "channel_post",
                "edited_channel_post",
                "callback_query",
            ],
        }
        assert len(server.calls("getUpdates")) == 1
        assert accepted == []
        release.set()
        assert (await server.next_poll())["offset"] == 102
        assert accepted == ["100", "101"]
        assert server.calls("sendMessage") == []
    finally:
        await cancel(task)


async def test_failed_admission_replays_current_event_only(bot: TelegramBot, server: TelegramServer) -> None:
    batch: list[ChannelValue] = [update(100), update(101), update(102)]
    server.responses["getUpdates"].extend([ok(batch), ok(batch)])
    accepted: list[str] = []

    async def failing(event: ChannelMessage) -> None:
        if event.message_id == "101":
            raise RuntimeError("Inbox unavailable")
        accepted.append(event.message_id)

    with pytest.raises(RuntimeError, match="Inbox unavailable"):
        await bot.listen(failing)
    assert accepted == ["100"]
    assert (await server.next_poll())["offset"] == 0

    async def receive(event: ChannelMessage) -> None:
        accepted.append(event.message_id)

    task = asyncio.create_task(bot.listen(receive))
    try:
        assert (await server.next_poll())["offset"] == 101
        assert (await server.next_poll())["offset"] == 103
        assert accepted == ["100", "101", "102"]
    finally:
        await cancel(task)


async def test_cancelled_admission_leaves_current_event_pending(bot: TelegramBot, server: TelegramServer) -> None:
    server.responses["getUpdates"].extend([ok([update()]), ok([update()])])
    entered = asyncio.Event()

    async def blocked(event: ChannelMessage) -> None:
        entered.set()
        await asyncio.Event().wait()

    task = asyncio.create_task(bot.listen(blocked))
    await server.next_poll()
    await asyncio.wait_for(entered.wait(), HANG_GUARD)
    await cancel(task)
    seen: list[str] = []

    async def receive(event: ChannelMessage) -> None:
        seen.append(event.message_id)

    task = asyncio.create_task(bot.listen(receive))
    try:
        assert (await server.next_poll())["offset"] == 0
        assert (await server.next_poll())["offset"] == 101
        assert seen == ["100"]
    finally:
        await cancel(task)


async def test_restart_replays_stable_identity_for_core_dedup(server: TelegramServer) -> None:
    identities: list[tuple[str, str]] = []

    async def admitted_then_stopped(event: ChannelMessage) -> None:
        identities.append(("telegram", event.message_id))
        # Simulate cancellation after the core committed but before it returned.
        raise asyncio.CancelledError

    for _ in range(2):
        server.responses["getUpdates"].append(ok([update()]))
        channel = TelegramBot(TOKEN, base_url=server.origin)
        try:
            await channel.open()
            with pytest.raises(asyncio.CancelledError):
                await channel.listen(admitted_then_stopped)
        finally:
            await channel.close()
    assert identities == [("telegram", "100"), ("telegram", "100")]
    assert [call["offset"] for call in server.calls("getUpdates")] == [0, 0]


@pytest.mark.parametrize("kind", ["message", "edited_message", "channel_post", "edited_channel_post"])
@pytest.mark.parametrize("has_reply", [False, True])
async def test_provenance_mapping(bot: TelegramBot, server: TelegramServer, kind: str, has_reply: bool) -> None:
    body = message()
    body.update(
        {
            "message_thread_id": 42,
            "sender_chat": {"id": -200, "type": "channel"},
            "forward_origin": {"type": "hidden_user", "sender_user_name": "Original", "date": 10},
            "external_reply": {"origin": {"type": "chat", "date": 10}, "location": {"latitude": 1.5}},
            "edit_date": 1700000001,
        }
    )
    if has_reply:
        body["reply_to_message"] = {**message(9, sender_id=8), "message_thread_id": 42}
    server.responses["getUpdates"].append(ok([update(kind=kind, body=body)]))
    events: list[ChannelMessage] = []

    async def receive(event: ChannelMessage) -> None:
        events.append(event)

    task = asyncio.create_task(bot.listen(receive))
    try:
        await server.next_poll()
        await server.next_poll()
        (event,) = events
        assert (event.message_id, event.conversation_id, event.sender_id) == ("100", "-100", "-200")
        assert (event.thread_id, event.reply_to, event.text, event.event_type) == ("42", "10", "Hello", kind)
        assert event.metadata["telegram_message_id"] == "10"
        assert event.metadata["forward_origin"] == body["forward_origin"]
        assert event.metadata["external_reply"] == body["external_reply"]
        if has_reply:
            assert event.metadata["in_reply_to"] == "9"
            reply = event.metadata["reply_to_message"]
            assert isinstance(reply, dict) and "text" not in reply
            assert reply["message_id"] == 9
        else:
            assert "in_reply_to" not in event.metadata
            assert "reply_to_message" not in event.metadata
    finally:
        await cancel(task)


async def test_media_references_no_download(bot: TelegramBot, server: TelegramServer) -> None:
    photo = message()
    photo.pop("text")
    photo.update(
        {
            "caption": "Photo caption",
            "media_group_id": "album-1",
            "photo": [
                {"file_id": "large", "file_unique_id": "unique", "width": 100, "height": 100, "file_size": 90},
                {"file_id": "small", "width": 10, "height": 10},
            ],
        }
    )
    animation = message(11)
    animation.pop("text")
    file: dict[str, ChannelValue] = {
        "file_id": "animation",
        "file_name": "clip.mp4",
        "mime_type": "video/mp4",
        "duration": 3,
    }
    animation.update({"animation": file, "document": file})
    server.responses["getUpdates"].append(ok([update(body=photo), update(101, body=animation)]))
    events: list[ChannelMessage] = []

    async def receive(event: ChannelMessage) -> None:
        events.append(event)

    task = asyncio.create_task(bot.listen(receive))
    try:
        await server.next_poll()
        await server.next_poll()
        assert events[0].text == "Photo caption"
        assert events[0].attachments[0].reference == "telegram:file:large"
        assert events[0].attachments[0].size == 90
        assert events[0].metadata["media_group_id"] == "album-1"
        assert len(events[1].attachments) == 1
        assert events[1].attachments[0].filename == "clip.mp4"
        assert "http" not in repr(events)
        assert TOKEN not in repr(events)
        assert {name for name, _ in server.requests} == {"getMe", "getUpdates"}
    finally:
        await cancel(task)


@pytest.mark.parametrize("ack_status", [200, 400, 429, 500])
async def test_callback_ack_before_admission_not_a_reply(
    bot: TelegramBot, server: TelegramServer, ack_status: int
) -> None:
    source = message(sender_id=BOT_ID)
    source["message_thread_id"] = 42
    source["reply_to_message"] = message(9)
    callback: dict[str, ChannelValue] = {
        "id": "callback-1",
        "from": {"id": 8},
        "message": source,
        "data": "pressed",
        "chat_instance": "ci",
    }
    server.responses["getUpdates"].append(ok([update(kind="callback_query", body=callback)]))
    if ack_status != 200:
        server.responses["answerCallbackQuery"].append(failure(ack_status))
    events: list[ChannelMessage] = []

    async def receive(event: ChannelMessage) -> None:
        assert server.calls("answerCallbackQuery") == [{"callback_query_id": "callback-1"}]
        assert len(server.calls("getUpdates")) == 1
        events.append(event)

    task = asyncio.create_task(bot.listen(receive))
    try:
        await server.next_poll()
        assert (await server.next_poll())["offset"] == 101
        (event,) = events
        assert (event.sender_id, event.text, event.reply_to, event.thread_id) == ("8", "pressed", "10", "42")
        assert event.message_id == "100"
        assert event.metadata["in_reply_to"] == "9"
        assert event.metadata["callback_acknowledged"] is (ack_status == 200)
        assert event.attachments == ()
        assert server.calls("sendMessage") == []
    finally:
        await cancel(task)


async def test_filtering_and_inaccessible_callback(server: TelegramServer) -> None:
    channel = TelegramBot(TOKEN, base_url=server.origin, allowed_chat_ids=["-100"])
    batch: list[ChannelValue] = [
        update(100, body=message(chat_id=-999)),
        update(101, body=message(sender_id=BOT_ID)),
        update(102, kind="poll", body={"id": "poll"}),
        update(103, body={**message(), "text": "", "new_chat_members": [{"id": 5}]}),
        update(104, kind="callback_query", body={"id": "inline", "from": {"id": 8}, "inline_message_id": "x"}),
        update(105, body={**message(), "direct_messages_topic": {"topic_id": 1}}),
        update(106, body={**message(), "business_connection_id": "business"}),
        update(107, body=message(0)),
        update(
            108,
            kind="callback_query",
            body={
                "id": "inaccessible",
                "from": {"id": 8},
                "data": "yes",
                "message": {"date": 0, "chat": {"id": -100}, "message_id": 8},
            },
        ),
        update(109, kind="channel_post", body={"message_id": 11, "chat": {"id": -100}, "text": "A post"}),
    ]
    server.responses["getUpdates"].append(ok(batch))
    events: list[ChannelMessage] = []

    async def receive(event: ChannelMessage) -> None:
        events.append(event)

    await channel.open()
    task = asyncio.create_task(channel.listen(receive))
    try:
        await server.next_poll()
        assert (await server.next_poll())["offset"] == 110
        assert [event.message_id for event in events] == ["108", "109"]
        assert events[1].sender_id == "-100"
        assert server.calls("answerCallbackQuery") == [{"callback_query_id": "inaccessible"}]
    finally:
        await cancel(task)
        await channel.close()


async def test_poll_retry_keeps_offset(bot: TelegramBot, server: TelegramServer, retry_delays: list[float]) -> None:
    server.responses["getUpdates"].extend([failure(500), failure(429, delay=3), ok([update()])])
    events: list[str] = []

    async def receive(event: ChannelMessage) -> None:
        events.append(event.message_id)

    task = asyncio.create_task(bot.listen(receive))
    try:
        offsets = [(await server.next_poll())["offset"] for _ in range(4)]
        assert offsets == [0, 0, 0, 101]
        assert retry_delays == [1, 3]
        assert events == ["100"]
    finally:
        await cancel(task)


async def test_transient_outage_is_survived(
    bot: TelegramBot, server: TelegramServer, retry_delays: list[float], poll_delays: list[float]
) -> None:
    # Four 500s exhaust the transport's per-request budget, so the listener must
    # back off and poll again rather than stopping the whole connector.
    server.responses["getUpdates"].extend([failure(500), failure(500), failure(500), failure(500), ok([update(101)])])
    accepted: list[str] = []
    delivered = asyncio.Event()

    async def receive(event: ChannelMessage) -> None:
        accepted.append(event.message_id)
        delivered.set()

    task = asyncio.create_task(bot.listen(receive))
    try:
        await asyncio.wait_for(delivered.wait(), HANG_GUARD)
        assert accepted == ["101"]
        assert retry_delays == [1, 2, 4]
        assert poll_delays == [1]
    finally:
        await cancel(task)


async def test_repeated_outages_back_off_to_the_cap(
    bot: TelegramBot, server: TelegramServer, retry_delays: list[float], poll_delays: list[float]
) -> None:
    server.responses["getUpdates"].extend(failure(503) for _ in range(32))
    server.responses["getUpdates"].append(ok([update(101)]))
    delivered = asyncio.Event()

    async def receive(event: ChannelMessage) -> None:
        delivered.set()

    task = asyncio.create_task(bot.listen(receive))
    try:
        await asyncio.wait_for(delivered.wait(), HANG_GUARD)
        assert retry_delays == [1, 2, 4] * 8
        assert poll_delays == [1, 2, 4, 8, 16, 32, 60, 60]
    finally:
        await cancel(task)


@pytest.mark.parametrize("status", [401, 403, 409])
async def test_permanent_poll_failure(bot: TelegramBot, server: TelegramServer, status: int) -> None:
    server.responses["getUpdates"].append(failure(status))

    async def receive(event: ChannelMessage) -> None:
        raise AssertionError("Should not receive")

    with pytest.raises(ChannelError, match=str(status)) as raised:
        await bot.listen(receive)
    assert TOKEN not in str(raised.value)
    assert len(server.calls("getUpdates")) == 1
    assert server.calls("deleteWebhook") == []


@pytest.mark.parametrize("result", [True, {}, [True], [{"update_id": True}], [update(101), update(100)]])
async def test_malformed_batches_never_admit(bot: TelegramBot, server: TelegramServer, result: ChannelValue) -> None:
    server.responses["getUpdates"].append(ok(result))

    async def receive(event: ChannelMessage) -> None:
        raise AssertionError("Should not receive")

    with pytest.raises(ChannelError):
        await bot.listen(receive)
    assert len(server.calls("getUpdates")) == 1


async def test_single_listener_and_cancellation_during_poll(bot: TelegramBot, server: TelegramServer) -> None:
    async def receive(event: ChannelMessage) -> None:
        raise AssertionError("Should not receive")

    task = asyncio.create_task(bot.listen(receive))
    await server.next_poll()
    with pytest.raises(ChannelError, match="already has"):
        await bot.listen(receive)
    await cancel(task)
    assert server.calls("getUpdates")[0]["offset"] == 0


async def test_callback_cancellation_does_not_ack_update(bot: TelegramBot, server: TelegramServer) -> None:
    entered = asyncio.Event()

    async def blocked(request: web.Request) -> web.Response:
        entered.set()
        await asyncio.Event().wait()
        return ok(True)

    server.responses["answerCallbackQuery"].append(blocked)
    server.responses["getUpdates"].append(
        ok(
            [
                update(
                    kind="callback_query",
                    body={
                        "id": "q",
                        "from": {"id": 7},
                        "message": message(),
                        "data": "yes",
                    },
                )
            ]
        )
    )

    async def receive(event: ChannelMessage) -> None:
        raise AssertionError("Should not receive")

    task = asyncio.create_task(bot.listen(receive))
    await server.next_poll()
    await asyncio.wait_for(entered.wait(), HANG_GUARD)
    await cancel(task)
    task = asyncio.create_task(bot.listen(receive))
    try:
        assert (await server.next_poll())["offset"] == 0
    finally:
        await cancel(task)
