import asyncio
import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import cast

import pytest
from nagents import Agent
from nagents import Provider
from nagents import ProviderType
from nagents import SessionManager
from nagents.channels import ChannelEvent
from nagents.channels import ChannelValue
from nagents.events import DoneEvent
from nagents.events import Event
from nagents.types import Message
from nagents.types import ToolCall

from nagents_channel_telegram_bot import TelegramBot

from .conftest import TOKEN
from .conftest import TelegramServer
from .conftest import message
from .conftest import ok
from .conftest import update
from .test_admission import callback
from .test_admission import human
from .test_admission import private_message
from .test_receive import cancel


@pytest.mark.parametrize("explicit_send", [False, True])
async def test_shared_agent_session_local_final_and_durable_restart_dedup(
    server: TelegramServer, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, explicit_send: bool
) -> None:
    sessions: list[str] = []
    observed: list[ChannelEvent] = []
    finished = asyncio.Event()

    async def offline_run(self: Agent, user_message: Message, session_id: str, user_id: str) -> AsyncIterator[Event]:
        # Replace model execution only; use the real Agent listener, inbox and tools.
        assert isinstance(user_message.content, str)
        sessions.append(session_id)
        if explicit_send and len(sessions) == 1:
            envelope = cast("dict[str, ChannelValue]", json.loads(user_message.content.split("\n", 1)[1]))
            assert envelope["message_id"] == "100"
            assert envelope["reply_to"] == "10"
            metadata = envelope["metadata"]
            assert isinstance(metadata, dict) and metadata["in_reply_to"] == "9"
            result = await self.tool_executor.execute(
                ToolCall(
                    id="explicit-call",
                    name="channel_send",
                    arguments={
                        "channel": envelope["channel"],
                        "destination": envelope["conversation_id"],
                        "thread_id": envelope["thread_id"],
                        "reply_to": envelope["reply_to"],
                        "text": "Explicit tool reply",
                    },
                )
            )
            assert not result.error
            edited = await self.tool_executor.execute(
                ToolCall(
                    id="explicit-edit",
                    name="channel_action",
                    arguments={
                        "channel": "telegram",
                        "action": "edit_message",
                        "arguments": {"destination": "-100", "message_id": "900", "text": "Explicit edit"},
                    },
                )
            )
            assert not edited.error
        yield DoneEvent(final_text="This final stays local", session_id=session_id)

    async def observe(event: ChannelEvent) -> None:
        observed.append(event)
        if len(observed) == 2:
            finished.set()

    monkeypatch.setattr(Agent, "run", offline_run)
    first = {**message(), "message_thread_id": 42, "reply_to_message": message(9)}
    batch: list[ChannelValue] = [update(100, body=first), update(101, body=message(20, chat_id=200))]
    for restart in (False, True):
        server.responses["getUpdates"].append(ok(batch))
        agent = Agent(
            provider=Provider(ProviderType.OPENAI_COMPATIBLE, "offline", "offline-model"),
            session_manager=SessionManager(tmp_path / "shared.db"),
        ).add_channel(TelegramBot(TOKEN, base_url=server.origin, poll_timeout=1))
        task = asyncio.create_task(agent.listen(session_id="personal-agent", on_event=observe))
        try:
            assert (await server.next_poll())["offset"] == 0
            assert (await server.next_poll())["offset"] == 102
            if not restart:
                await asyncio.wait_for(finished.wait(), timeout=2)
        finally:
            try:
                await cancel(task)
            finally:
                await agent.close()
        # Restarted transport replay reaches the real durable inbox and is deduped.
        assert sessions == ["personal-agent", "personal-agent"]
        assert {event.session_id for event in observed} == {"personal-agent"}
        assert {event.message_id for event in observed} == {"100", "101"}
        assert len(server.calls("sendMessage")) == int(explicit_send)
        assert len(server.calls("editMessageText")) == int(explicit_send)
        if explicit_send:
            assert server.calls("sendMessage") == [
                {
                    "chat_id": "-100",
                    "text": "Explicit tool reply",
                    "message_thread_id": 42,
                    "reply_parameters": {"message_id": 10, "allow_sending_without_reply": False},
                }
            ]


async def test_user_private_filters_gate_real_agent_inbox_and_model_execution(
    server: TelegramServer,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    envelopes: list[dict[str, ChannelValue]] = []
    observed: list[ChannelEvent] = []
    finished = asyncio.Event()

    async def offline_run(self: Agent, user_message: Message, session_id: str, user_id: str) -> AsyncIterator[Event]:
        assert isinstance(user_message.content, str)
        envelopes.append(cast("dict[str, ChannelValue]", json.loads(user_message.content.split("\n", 1)[1])))
        yield DoneEvent(final_text="Local result", session_id=session_id)

    async def observe(event: ChannelEvent) -> None:
        observed.append(event)
        if len(observed) == 3:
            finished.set()

    monkeypatch.setattr(Agent, "run", offline_run)
    stranger = {**private_message(8, "other_user"), "text": "/sessions", "forward_from": human()}
    batch: list[ChannelValue] = [
        update(100, body=stranger),
        update(101, kind="callback_query", body=callback(human(8, "other_user"), private_message(), "denied")),
        update(102, body={**private_message(), "chat": {"id": -100, "type": "supergroup"}}),
        update(103, body=private_message()),
        update(104, kind="edited_message", body=private_message(9, "OFFLINE_PEER")),
        update(105, kind="callback_query", body=callback(human(), private_message(), "accepted")),
    ]
    server.responses["getUpdates"].append(ok(batch))
    agent = Agent(
        provider=Provider(ProviderType.OPENAI_COMPATIBLE, "offline", "offline-model"),
        session_manager=SessionManager(tmp_path / "admission.db"),
    ).add_channel(
        TelegramBot(
            TOKEN,
            base_url=server.origin,
            poll_timeout=1,
            allowed_user_ids=["7"],
            allowed_usernames=["@offline_peer"],
            private_chats_only=True,
        )
    )
    task = asyncio.create_task(agent.listen(session_id="offline-admission", on_event=observe))
    try:
        assert (await server.next_poll())["offset"] == 0
        assert (await server.next_poll())["offset"] == 106
        await asyncio.wait_for(finished.wait(), timeout=2)
        assert {event.message_id for event in observed} == {"103", "104", "105"}
        assert {envelope["message_id"] for envelope in envelopes} == {"103", "104", "105"}
        assert [envelope["conversation_id"] for envelope in envelopes] == ["7", "9", "7"]
        assert server.calls("answerCallbackQuery") == [{"callback_query_id": "accepted"}]
        assert server.calls("sendMessage") == []
    finally:
        try:
            await cancel(task)
        finally:
            await agent.close()
