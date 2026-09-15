"""Connector rendering/lifecycle with a fake Transport and no Telegram/provider I/O."""

import asyncio
import subprocess
import sys
from collections.abc import AsyncIterator
from dataclasses import dataclass
from dataclasses import field
from dataclasses import replace
from typing import cast

import pytest
from nagents.channels import ChannelActivity
from nagents.channels import ChannelError
from nagents.channels import ChannelExecutionEvent
from nagents.channels import ChannelExecutionPhase
from nagents.channels import ChannelValue
from nagents.channels import dispatch_channel_execution_event

from nagents_channel_telegram_bot import TelegramBot
from nagents_channel_telegram_bot import _execution
from nagents_channel_telegram_bot import plugin
from nagents_channel_telegram_bot._execution import tool_text
from nagents_channel_telegram_bot._transport import Transport

from .conftest import BOT_ID
from .conftest import TOKEN
from .test_activity import Clock


@dataclass
class FakeTransport:
    calls: list[tuple[str, dict[str, ChannelValue]]] = field(default_factory=list)
    now: float = 100.0
    failure: str = ""
    block: bool = False
    entered: asyncio.Event = field(default_factory=asyncio.Event)
    release: asyncio.Event = field(default_factory=asyncio.Event)
    exited: asyncio.Event = field(default_factory=asyncio.Event)

    async def request(self, method: str, payload: dict[str, ChannelValue]) -> ChannelValue:
        self.calls.append((method, dict(payload)))
        if method == "getMe":
            return {"id": BOT_ID, "is_bot": True}
        if method == "sendMessage":
            self.entered.set()
            try:
                if self.block:
                    await self.release.wait()
                if self.failure == "cancel":
                    raise asyncio.CancelledError
                if self.failure:
                    raise ChannelError(
                        "opaque-test-error", outcome_unknown=True, retry_after=10 if self.failure == "rate" else 0
                    )
            finally:
                self.exited.set()
        return True

    def texts(self) -> list[str]:
        return [str(payload["text"]) for method, payload in self.calls if method == "sendMessage"]


@pytest.fixture
async def execution(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[tuple[TelegramBot, FakeTransport]]:
    fake = FakeTransport()

    async def request(
        transport: Transport, method: str, payload: dict[str, ChannelValue], *, timeout: int = 15
    ) -> ChannelValue:
        return await fake.request(method, payload)

    monkeypatch.setattr(Transport, "request", request)
    monkeypatch.setattr(_execution, "monotonic", lambda: fake.now)
    bot = TelegramBot(TOKEN, execution_notifications=True)
    await bot.open()
    try:
        yield bot, fake
    finally:
        await bot.close()


def event(
    phase: ChannelExecutionPhase,
    *,
    run: str = "run-a",
    session: str = "session-a",
    chat: str = "-100",
    thread: str = "",
    call: str = "call-a",
    activation: str = "root",
) -> ChannelExecutionEvent:
    return ChannelExecutionEvent(
        chat,
        session,
        phase,
        thread_id=thread,
        run_id=run,
        activation_id=activation,
        call_id=call,
        tool_name="read_file",
        tool_arguments={"path": "README.md", "command": "ls -l"},
    )


@pytest.mark.parametrize("value", [None, 0, 1, "true", "false", [], {}])
def test_execution_notification_config_strict_boolean(value: ChannelValue) -> None:
    with pytest.raises(ChannelError, match="execution_notifications"):
        plugin({"token": TOKEN, "execution_notifications": value})
    with pytest.raises(ChannelError, match="execution_notifications"):
        TelegramBot(TOKEN, execution_notifications=cast("bool", value))


def test_config_default_and_schema_are_opt_in() -> None:
    channel = plugin({"token": TOKEN})
    assert isinstance(channel, TelegramBot) and not channel._execution_notifications
    enabled = plugin({"token": TOKEN, "execution_notifications": True})
    assert isinstance(enabled, TelegramBot) and enabled._execution_notifications
    properties = plugin.config_schema["properties"]
    assert isinstance(properties, dict)
    schema = properties["execution_notifications"]
    assert isinstance(schema, dict) and schema["type"] == "boolean" and schema["default"] is False


def test_old_sdk_import_has_no_runtime_dependency_on_new_execution_symbols() -> None:
    script = """
import nagents.channels as sdk
for name in (
    "ChannelExecutionEvent", "ChannelExecutionPhase",
    "dispatch_channel_execution_event", "sanitize_channel_tool_arguments",
):
    delattr(sdk, name)
delattr(sdk.Channel, "on_event")
from nagents_channel_telegram_bot import TelegramBot, plugin
assert TelegramBot("123456:OFFLINE_TEST_TOKEN").name == "telegram"
assert callable(plugin)
"""
    result = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr


async def test_default_closed_and_private_chat_gate(execution: tuple[TelegramBot, FakeTransport]) -> None:
    bot, fake = execution
    bot._execution_notifications = False
    await bot.on_event(event("run_started"))
    await bot.on_event(event("waiting_for_approval"))
    assert not fake.texts()
    bot._execution_notifications = True
    bot._allowed_chats = frozenset({"7"})
    await bot.on_event(event("run_started"))
    await bot.on_event(event("waiting_for_approval"))
    bot._allowed_chats = frozenset()
    bot._private_chats_only = True
    await bot.on_event(event("run_started"))
    await bot.on_event(event("waiting_for_approval"))
    assert not fake.texts()
    await bot.on_event(event("run_started", chat="7"))
    await bot.on_event(event("waiting_for_approval", chat="7"))
    assert fake.texts() == ["⏸ Waiting for approval"]
    await bot.close()
    await bot.on_event(event("run_started", chat="7", run="closed"))
    await bot.on_event(event("waiting_for_approval", chat="7", run="closed"))
    assert fake.texts() == ["⏸ Waiting for approval"]


async def test_lifecycle_states_never_post_text_and_only_tool_and_approval_do(
    execution: tuple[TelegramBot, FakeTransport],
) -> None:
    bot, fake = execution
    for phase in ("run_started", "tool_requested", "waiting_for_approval", "completed"):
        await dispatch_channel_execution_event(bot, event(phase, thread="42"))
    assert fake.texts() == [
        "🔧 read_file (requested)\n  path: README.md\n  command: ls -l",
        "⏸ Waiting for approval",
    ]
    assert not bot._typing._entries
    for method, payload in fake.calls:
        if method == "sendMessage":
            assert set(payload) == {"chat_id", "text", "message_thread_id"}
            assert payload["chat_id"] == "-100" and payload["message_thread_id"] == 42


async def test_lifecycle_reuses_existing_four_second_typing_and_preserves_approval(
    execution: tuple[TelegramBot, FakeTransport],
) -> None:
    bot, fake = execution
    clock = Clock()
    bot._typing._sleep = clock.sleep
    bot._typing._clock = clock.time
    await bot.activity(ChannelActivity("-100", True, session_id="session-a"))
    first = await clock.next_sleep()
    await bot.on_event(event("run_started"))
    await bot.on_event(event("waiting_for_approval"))
    assert first.delay == 4 and len(bot._typing._entries) == 1
    assert len([method for method, _ in fake.calls if method == "sendChatAction"]) == 1
    clock.advance(first)
    second = await clock.next_sleep()
    assert second.delay == 4
    await bot.on_event(event("completed"))
    assert second.cancelled.is_set() and not bot._typing._entries


@pytest.mark.parametrize("phase", ["completed", "failed", "cancelled"])
async def test_terminal_status_no_output_blobs_and_stale_terminal_cannot_stop_new_run(
    execution: tuple[TelegramBot, FakeTransport], phase: ChannelExecutionPhase
) -> None:
    bot, fake = execution
    await bot.on_event(event("run_started", run="old"))
    await bot.on_event(event("run_started", run="new"))
    await bot.on_event(event(phase, run="old"))
    assert ("-100", "") in bot._typing._entries and not fake.texts()
    await bot.on_event(event(phase, run="new"))
    await bot.on_event(event(phase, run="new"))
    assert not fake.texts() and not bot._typing._entries


async def test_run_owner_correlation_never_retargets_or_broadcasts(
    execution: tuple[TelegramBot, FakeTransport],
) -> None:
    bot, fake = execution
    await bot.on_event(event("run_started"))
    for altered in (
        event("tool_requested", chat="200"),
        event("tool_requested", thread="1"),
        event("tool_requested", session="other"),
        event("tool_requested", run="unknown"),
        event("run_started", chat="200"),
    ):
        await bot.on_event(altered)
    assert fake.texts() == []
    await bot.on_event(event("tool_requested"))
    fake.now += 3
    await bot.on_event(event("tool_requested", activation="child"))
    assert len(fake.texts()) == 2
    await bot.on_event(event("tool_requested", activation="child"))
    assert len(fake.texts()) == 2


async def test_tool_rate_budget_and_dedup_are_bounded_but_approval_terminal_reserved(
    execution: tuple[TelegramBot, FakeTransport],
) -> None:
    bot, fake = execution
    await bot.on_event(event("run_started"))
    for index in range(_execution.MAX_SEEN + 5):
        await bot.on_event(event("tool_requested", call=f"call-{index}"))
        fake.now += 3
    run = bot._execution._runs["run-a"]
    assert len(run.seen) == _execution.MAX_SEEN and run.tools == _execution.MAX_TOOL_NOTICES
    await bot.on_event(event("waiting_for_approval"))
    await bot.on_event(event("completed"))
    assert len(fake.texts()) == _execution.MAX_TOOL_NOTICES + 1
    assert fake.texts()[-1] == "⏸ Waiting for approval"


async def test_immediate_tool_suppression_not_replayed_and_optional_status_only(
    execution: tuple[TelegramBot, FakeTransport],
) -> None:
    bot, fake = execution
    await bot.on_event(event("run_started"))
    await bot.on_event(event("tool_requested"))
    await bot.on_event(event("tool_completed"))
    fake.now += 3
    await bot.on_event(event("tool_completed"))
    assert len(fake.texts()) == 1
    await bot.on_event(replace(event("tool_completed", call="other"), tool_failed=True))
    assert fake.texts()[-1] == "🔧 read_file (failed)"


@pytest.mark.parametrize("tool", ["channel_send", "channel_list", "channel_action"])
async def test_transport_tools_do_not_recursively_notify(
    execution: tuple[TelegramBot, FakeTransport], tool: str
) -> None:
    bot, fake = execution
    await bot.on_event(event("run_started"))
    await bot.on_event(replace(event("tool_requested"), tool_name=tool))
    await bot.on_event(replace(event("tool_completed"), tool_name=tool))
    assert fake.texts() == [] and bot._typing._entries


def test_clean_short_paths_commands_unicode_and_no_parse_markup() -> None:
    assert (
        tool_text("shell", {"path": "README.md", "command": "ls -l", "label": "café ☕", "limit": 3}, TOKEN)
        == "🔧 shell (requested)\n  path: README.md\n  command: ls -l\n  label: café ☕\n  limit: 3"
    )
    assert tool_text("inspect", {"value": "*literal*"}, TOKEN).endswith("value: *literal*")


@pytest.mark.parametrize(
    "value",
    [
        "x" * 81,
        "x" * 10000,
        "line\nnext",
        "tab\tvalue",
        "\u202ehidden",
        "\x1b[31m",
        "\ud800",
        [1, 2],
        {"key": "value"},
        "[redacted]",
        "[truncated]",
        float("nan"),
        float("inf"),
        2**100,
    ],
)
def test_long_nested_control_values_never_truncated_or_rendered(value: object) -> None:
    assert tool_text("inspect", {"value": value}, TOKEN) == "🔧 inspect (requested)"


@pytest.mark.parametrize(
    "key",
    ["token", "apiKey", "PASSWORD", "client_secret", "authorization", "private_key", "content", "result", "prompt"],
)
def test_secret_and_content_fields_are_excluded(key: str) -> None:
    assert tool_text("inspect", {key: "private-value", "limit": 2}, TOKEN) == "🔧 inspect (requested)\n  limit: 2"


@pytest.mark.parametrize(
    "secret",
    [
        "sk-" + "x" * 25,
        "ghp_" + "x" * 25,
        "github_pat_" + "x" * 25,
        "AKIA" + "X" * 16,
        "123456:" + "X" * 24,
        "Bearer fixture-value",
        "token=fixture-value",
        "eyJabc.abc.abc",
        "https://fixture:credential@example.invalid",
    ],
)
def test_known_credential_patterns_omitted_even_in_short_commands(secret: str) -> None:
    assert tool_text("shell", {"command": secret}, TOKEN) == "🔧 shell (requested)"


def test_own_token_checked_in_full_nested_excluded_values_before_preview() -> None:
    for arguments in (
        {"command": TOKEN},
        {"nested": {"value": "x" * 10000 + TOKEN}, "path": "README.md"},
        {"token": TOKEN, "limit": 3},
    ):
        assert tool_text("shell", arguments, TOKEN) == "🔧 shell (requested)"
    assert tool_text(TOKEN, {}, TOKEN) == ""


def test_credential_patterns_in_keys_and_short_environment_commands_are_omitted() -> None:
    assert tool_text("inspect", {"ghp_" + "x" * 24: "value"}, TOKEN) == "🔧 inspect (requested)"
    for command in (
        "SERVICE_TOKEN=fixture-value ls",
        "APP_API_KEY=fixture-value ls",
        'show {"api_key":"fixture-value"}',
        "AIza" + "x" * 24,
    ):
        assert tool_text("shell", {"command": command}, TOKEN) == "🔧 shell (requested)"


async def test_direct_hook_checks_own_token_even_if_sdk_event_dict_was_mutated(
    execution: tuple[TelegramBot, FakeTransport],
) -> None:
    bot, fake = execution
    await bot.on_event(event("run_started"))
    request = event("tool_requested")
    request.tool_arguments["nested"] = {"value": TOKEN}
    await bot.on_event(request)
    assert fake.texts()[-1] == "🔧 read_file (requested)"


@pytest.mark.parametrize("failure", ["opaque", "cancel", "timeout"])
async def test_hook_failure_does_not_retry_or_break_typing(
    execution: tuple[TelegramBot, FakeTransport],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    failure: str,
) -> None:
    bot, fake = execution
    monkeypatch.setattr(_execution, "SEND_TIMEOUT", 0.03)
    await bot.on_event(event("run_started"))
    fake.failure = failure
    fake.block = failure == "timeout"
    await dispatch_channel_execution_event(bot, event("tool_requested"))
    assert fake.exited.is_set() and bot._typing._entries
    await bot.on_event(event("tool_requested"))
    assert len(fake.texts()) == 1
    await dispatch_channel_execution_event(bot, event("failed"))
    assert len(fake.texts()) == 1 and not bot._typing._entries
    assert "opaque-test-error" not in caplog.text


async def test_rate_limit_cools_notifications_without_stopping_typing(
    execution: tuple[TelegramBot, FakeTransport],
) -> None:
    bot, fake = execution
    await bot.on_event(event("run_started"))
    fake.failure = "rate"
    await bot.on_event(event("tool_requested", call="call-rate"))
    fake.failure = ""
    await bot.on_event(event("tool_requested", call="call-cooled"))
    assert len(fake.texts()) == 1 and bot._typing._entries
    fake.now += 11
    await bot.on_event(event("tool_requested", call="call-warm"))
    assert len(fake.texts()) == 2 and fake.texts()[-1].startswith("🔧 read_file (requested)")
    await bot.on_event(event("completed"))
    assert not bot._typing._entries


async def test_close_joins_inflight_hook_and_terminal_cancellation_cleans_typing(
    execution: tuple[TelegramBot, FakeTransport],
) -> None:
    bot, fake = execution
    await bot.on_event(event("run_started"))
    assert bot._typing._entries
    fake.entered.clear()
    fake.exited.clear()
    fake.block = True
    task = asyncio.create_task(bot.on_event(event("tool_requested")))
    await asyncio.wait_for(fake.entered.wait(), 2)
    assert bot._typing._entries
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert fake.exited.is_set()
    fake.block = False
    await bot.on_event(event("cancelled"))
    assert not bot._typing._entries
    await bot.close()
    assert not bot._execution._runs and not bot._typing._entries


async def test_close_waits_for_inflight_event_before_releasing_transport(
    execution: tuple[TelegramBot, FakeTransport],
) -> None:
    bot, fake = execution
    session = bot._transport._session
    assert session is not None
    await bot.on_event(event("run_started"))
    fake.block = True
    producer = asyncio.create_task(bot.on_event(event("tool_requested")))
    await asyncio.wait_for(fake.entered.wait(), 2)
    closing = asyncio.create_task(bot.close())
    await asyncio.sleep(0)
    closing.cancel()
    await asyncio.sleep(0)
    assert not closing.done() and not session.closed
    fake.release.set()
    await producer
    with pytest.raises(asyncio.CancelledError):
        await closing
    assert session.closed and not bot._typing._entries and not bot._execution._runs
