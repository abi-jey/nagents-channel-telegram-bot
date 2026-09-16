"""Inline approval prompts: button rendering, tap parsing, and one-shot handles."""

from __future__ import annotations

from dataclasses import dataclass
from dataclasses import field
from typing import TYPE_CHECKING
from typing import cast

import pytest
from nagents.channels import ChannelApproval
from nagents.channels import ChannelError
from nagents.channels import ChannelExecutionEvent
from nagents.channels import ChannelExecutionPhase
from nagents.channels import ChannelMessage
from nagents.channels import ChannelValue
from nagents.channels import dispatch_channel_execution_event

from nagents_channel_telegram_bot import TelegramBot
from nagents_channel_telegram_bot import _execution
from nagents_channel_telegram_bot import plugin
from nagents_channel_telegram_bot._execution import approval_text
from nagents_channel_telegram_bot._transport import Transport

from .conftest import BOT_ID
from .conftest import TOKEN

if TYPE_CHECKING:
    from collections.abc import AsyncIterator


@dataclass
class FakeTransport:
    calls: list[tuple[str, dict[str, ChannelValue]]] = field(default_factory=list)

    async def request(self, method: str, payload: dict[str, ChannelValue]) -> ChannelValue:
        self.calls.append((method, dict(payload)))
        if method == "getMe":
            return {"id": BOT_ID, "is_bot": True}
        return True

    def sent(self) -> list[dict[str, ChannelValue]]:
        return [payload for method, payload in self.calls if method == "sendMessage"]


@pytest.fixture
async def bot(
    monkeypatch: pytest.MonkeyPatch,
) -> AsyncIterator[tuple[TelegramBot, FakeTransport]]:
    fake = FakeTransport()

    async def request(
        transport: Transport, method: str, payload: dict[str, ChannelValue], *, timeout: int = 15
    ) -> ChannelValue:
        return await fake.request(method, payload)

    monkeypatch.setattr(Transport, "request", request)
    channel = TelegramBot(TOKEN, execution_notifications=True, chat_approvals=True)
    await channel.open()
    try:
        yield channel, fake
    finally:
        await channel.close()


def event(
    phase: ChannelExecutionPhase,
    *,
    run: str = "run-a",
    session: str = "session-a",
    chat: str = "-100",
    call: str = "call-a",
) -> ChannelExecutionEvent:
    return ChannelExecutionEvent(chat, session, phase, run_id=run, call_id=call, tool_name="read_file")


def tap(data: ChannelValue, *, chat: str = "-100") -> ChannelMessage:
    return ChannelMessage(
        "600",
        chat,
        "7",
        event_type="callback_query",
        metadata={"callback_query": {"data": data}},
    )


async def prompt(channel: TelegramBot, fake: FakeTransport) -> list[dict[str, ChannelValue]]:
    await dispatch_channel_execution_event(channel, event("run_started"))
    await dispatch_channel_execution_event(channel, event("waiting_for_approval"))
    sent = fake.sent()
    markup = sent[-1]["reply_markup"]
    assert isinstance(markup, dict)
    keyboard = markup["inline_keyboard"]
    assert isinstance(keyboard, list)
    return cast("list[dict[str, ChannelValue]]", keyboard[0])


@pytest.mark.parametrize("value", [None, 0, 1, "true", [], {}])
def test_chat_approvals_config_is_a_strict_boolean(value: ChannelValue) -> None:
    with pytest.raises(ChannelError, match="chat_approvals"):
        plugin({"token": TOKEN, "chat_approvals": value})
    with pytest.raises(ChannelError, match="chat_approvals"):
        TelegramBot(TOKEN, chat_approvals=cast("bool", value))


def test_chat_approvals_defaults_off_and_is_schema_declared() -> None:
    channel = plugin({"token": TOKEN})
    assert isinstance(channel, TelegramBot) and not channel._chat_approvals
    properties = plugin.config_schema["properties"]
    assert isinstance(properties, dict)
    schema = properties["chat_approvals"]
    assert isinstance(schema, dict) and schema["type"] == "boolean" and schema["default"] is False


def test_approval_text_names_the_tool_or_falls_back() -> None:
    assert approval_text("read_file", TOKEN) == "⏸ Approve read_file?"
    for unsafe in (TOKEN, "", "bad name", "a" * 100, 7, None):
        assert approval_text(unsafe, TOKEN) == "⏸ Waiting for approval"


async def test_approve_button_is_one_shot_and_disables_deny(bot: tuple[TelegramBot, FakeTransport]) -> None:
    channel, fake = bot
    approve, deny = await prompt(channel, fake)
    assert approve["text"] == "✅ Approve" and deny["text"] == "❌ Deny"
    assert channel.approval(tap(approve["callback_data"])) == ChannelApproval(
        "-100", "session-a", "run-a", "call-a", allow=True
    )
    # Both buttons carry the same one-shot decision handle.
    assert channel.approval(tap(approve["callback_data"])) == ChannelApproval()
    assert channel.approval(tap(deny["callback_data"])) == ChannelApproval()


async def test_deny_button_resolves_the_same_prompt(bot: tuple[TelegramBot, FakeTransport]) -> None:
    channel, fake = bot
    _, deny = await prompt(channel, fake)
    assert channel.approval(tap(deny["callback_data"])) == ChannelApproval(
        "-100", "session-a", "run-a", "call-a", allow=False
    )


async def test_only_reserved_callbacks_are_claimed(bot: tuple[TelegramBot, FakeTransport]) -> None:
    channel, _ = bot
    assert channel.approval(tap("menu:1")) is None
    assert channel.approval(tap(None)) is None
    assert channel.approval(ChannelMessage("1", "-100", "7", "plain")) is None
    for malformed in ("ngn:", "ngn:a", "ngn:x:abc", "ngn:a:!!", "ngn:a:" + "x" * 40):
        assert channel.approval(tap(malformed)) == ChannelApproval()


async def test_foreign_chat_cannot_consume_the_handle(bot: tuple[TelegramBot, FakeTransport]) -> None:
    channel, fake = bot
    approve, _ = await prompt(channel, fake)
    assert channel.approval(tap(approve["callback_data"], chat="-200")) == ChannelApproval()
    assert channel.approval(tap(approve["callback_data"])) == ChannelApproval(
        "-100", "session-a", "run-a", "call-a", allow=True
    )


async def test_approval_is_inert_without_the_policy(bot: tuple[TelegramBot, FakeTransport]) -> None:
    channel, fake = bot
    approve, _ = await prompt(channel, fake)
    channel._chat_approvals = False
    assert channel.approval(tap(approve["callback_data"])) is None


async def test_prompt_omits_buttons_when_policy_is_off(bot: tuple[TelegramBot, FakeTransport]) -> None:
    channel, fake = bot
    channel._execution._approvals = False
    await dispatch_channel_execution_event(channel, event("run_started"))
    await dispatch_channel_execution_event(channel, event("waiting_for_approval"))
    sent = fake.sent()
    assert sent[-1]["text"] == "⏸ Waiting for approval" and "reply_markup" not in sent[-1]


async def test_handles_are_bounded_and_cleared_on_close(bot: tuple[TelegramBot, FakeTransport]) -> None:
    channel, _ = bot
    run = _execution._Run("session-a", "run-a", "-100", "")
    for index in range(_execution.MAX_HANDLES + 5):
        channel._execution._buttons(run, event("waiting_for_approval", call=f"call-{index}"))
    assert len(channel._execution._handles) == _execution.MAX_HANDLES
    await channel.close()
    assert not channel._execution._handles and not channel._execution._runs
