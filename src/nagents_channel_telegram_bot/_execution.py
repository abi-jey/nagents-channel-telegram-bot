"""Telegram-specific, bounded execution rendering over the shared Channel hook."""

from __future__ import annotations

import asyncio
import math
import re
import secrets
from collections import OrderedDict
from dataclasses import dataclass
from dataclasses import field
from time import monotonic
from typing import TYPE_CHECKING

from nagents.channels import ChannelApproval
from nagents.channels import ChannelError

from ._validation import identifier

if TYPE_CHECKING:
    from nagents.channels import ChannelExecutionEvent
    from nagents.channels import ChannelMessage
    from nagents.channels import ChannelValue

    from ._activity import Typing
    from ._transport import Transport

MAX_RUNS = 64
MAX_RETIRED = 256
MAX_TOOL_NOTICES = 8
MAX_APPROVAL_NOTICES = 8
MAX_SEEN = 128
MAX_HANDLES = 64
TOOL_INTERVAL = 2.0
SEND_TIMEOUT = 1.0
_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_.:-]{0,63}")
_KEY = re.compile(r"[A-Za-z_][A-Za-z0-9_.-]{0,63}")
_HANDLE = re.compile(r"[A-Za-z0-9_-]{8,32}")
_APPROVAL_PREFIX = "ngn:"
_APPROVE = "✅ Approve"
_DENY = "❌ Deny"
_PRIVATE = re.compile(
    r"auth|cookie|credential|secret|token|password|passwd|passphrase|api.?key|private.?key|"
    r"text|content|body|message|prompt|reasoning|thinking|result|output|headers|environment",
    re.I,
)
_CREDENTIAL = re.compile(
    r"\b(?:sk-|gh[pousr]_|github_pat_)[A-Za-z0-9_-]{8,}|\bAIza[A-Za-z0-9_-]{20,}|\b(?:AKIA|ASIA)[A-Z0-9]{16}\b|"
    r"\b[0-9]{5,}:[A-Za-z0-9_-]{10,}|\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+|"
    r"\b(?:bearer|basic)\s+\S+|-----BEGIN [A-Z ]*PRIVATE KEY-----|"
    r"\b[A-Za-z0-9_]*(?:token|secret|password|api[_-]?key|authorization)[\"']?\s*[=:]|"
    r"[a-z][a-z0-9+.-]*://[^\s/@]+:[^\s/@]+@",
    re.I,
)
_TRANSPORT = frozenset({"channel_send", "channel_list", "channel_action"})
_APPROVAL_NOTICE = "⏸ Waiting for approval"
_TERMINAL = frozenset({"completed", "failed", "cancelled"})
# Lifecycle phases drive the typing indicator only; no chat message is posted for
# them. Tool notices and the approval prompt are the only rendered execution text.
_PHASES = frozenset(
    {"run_started", "tool_requested", "tool_completed", "waiting_for_approval", "completed", "failed", "cancelled"}
)


def _units(value: str) -> int:
    return len(value.encode("utf-16-le")) // 2


def _credential_free(arguments: object, token: str) -> bool:
    """Check whole values, including excluded/nested fields, before preview selection."""
    nodes = 0
    size = 0

    def check(value: object, depth: int = 0) -> bool:
        nonlocal nodes, size
        nodes += 1
        if depth > 32 or nodes > 10000:
            return False
        if type(value) is str:
            size += len(value)
            return size <= 1024 * 1024 and token not in value
        if type(value) is dict:
            return all(check(key, depth + 1) and check(child, depth + 1) for key, child in value.items())
        if type(value) is list:
            return all(check(child, depth + 1) for child in value)
        return value is None or type(value) in (bool, int, float)

    return check(arguments)


def tool_text(name: object, arguments: object, token: str, *, completed: bool = False, failed: bool = False) -> str:
    """Plain text only: four short scalars, no truncation, results or raw exceptions."""
    try:
        if (
            type(name) is not str
            or token in name
            or _CREDENTIAL.search(name)
            or _NAME.fullmatch(name) is None
            or name.casefold() in _TRANSPORT
        ):
            return ""
        if completed:
            return f"🔧 {name} ({'failed' if failed else 'done'})"
        lines = [f"🔧 {name} (requested)"]
        if not _credential_free(arguments, token) or type(arguments) is not dict:
            return lines[0]
        for key, value in arguments.items():
            if type(key) is not str or _KEY.fullmatch(key) is None or _PRIVATE.search(key) or _CREDENTIAL.search(key):
                continue
            if type(value) is str:
                if (
                    not value
                    or not value.isprintable()
                    or _units(value) > 80
                    or value in ("[redacted]", "[truncated]")
                    or _CREDENTIAL.search(value)
                ):
                    continue
                rendered = value
            elif type(value) is bool:
                rendered = "true" if value else "false"
            elif (type(value) is int and -(2**63) <= value < 2**63) or (type(value) is float and math.isfinite(value)):
                rendered = str(value)
            else:
                continue
            line = f"  {key}: {rendered}"
            if len(lines) > 4 or _units("\n".join([*lines, line])) > 400:
                break
            lines.append(line)
        return "\n".join(lines)
    except Exception:
        return ""  # Malformed plugin values never acquire a repr in Telegram/logs.


def approval_text(name: object, token: str) -> str:
    """Name the pending tool in an approval prompt, or fall back to a generic prompt."""
    try:
        if (
            type(name) is str
            and token not in name
            and _CREDENTIAL.search(name) is None
            and _NAME.fullmatch(name) is not None
        ):
            return f"⏸ Approve {name}?"
    except Exception:
        pass
    return _APPROVAL_NOTICE


@dataclass
class _Run:
    session: str
    id: str
    chat: str
    thread: str
    seen: set[tuple[str, str, str]] = field(default_factory=set)
    waiting: set[tuple[str, str]] = field(default_factory=set)
    tools: int = 0
    approvals: int = 0
    next_tool: float = 0.0


class ExecutionNotifications:
    def __init__(self, transport: Transport, typing: Typing, token: str, *, approvals: bool = False) -> None:
        self._transport = transport
        self._typing = typing
        self._token = token
        self._approvals = approvals
        self._runs: dict[str, _Run] = {}
        self._latest: dict[tuple[str, str], str] = {}
        self._retired: dict[str, None] = {}
        self._handles: OrderedDict[str, tuple[str, str, str, str]] = OrderedDict()
        self._accepting = False
        self._lock = asyncio.Lock()
        self._retry_at = 0.0

    def open(self) -> None:
        self._accepting = True

    def disable(self) -> None:
        self._accepting = False

    def _retire(self, run: _Run) -> None:
        self._runs.pop(run.id, None)
        if self._latest.get((run.chat, run.thread)) == run.id:
            self._latest.pop((run.chat, run.thread))
        self._retired[run.id] = None
        while len(self._retired) > MAX_RETIRED:
            del self._retired[next(iter(self._retired))]

    async def handle(self, event: ChannelExecutionEvent) -> None:
        if not self._accepting:
            return
        async with self._lock:
            if not self._accepting:
                return
            phase = event.phase
            if phase not in _PHASES:
                return
            if (
                any(
                    type(value) is not str or len(value) > 256 or (value and not value.isprintable())
                    for value in (
                        event.session_id,
                        event.run_id,
                        event.activation_id,
                        event.call_id,
                        event.thread_id,
                    )
                )
                or not event.session_id
                or not event.run_id
            ):
                return
            chat = identifier(event.conversation_id, "execution conversation")
            thread = identifier(event.thread_id, "execution thread", positive=True) if event.thread_id else ""
            if event.run_id in self._retired:
                return
            run = self._runs.get(event.run_id)
            if phase == "run_started":
                if run is not None:
                    return  # A run ID is pinned to one session/chat/thread, never broadcast.
                previous = self._runs.get(self._latest.get((chat, thread), ""))
                if previous is not None:
                    self._retire(previous)
                if len(self._runs) >= MAX_RUNS:
                    return
                run = _Run(event.session_id, event.run_id, chat, thread)
                self._runs[run.id] = run
                self._latest[(chat, thread)] = run.id
            elif run is None or (run.session, run.chat, run.thread) != (event.session_id, chat, thread):
                return
            if run is None or self._latest.get((chat, thread)) != run.id:
                return
            terminal = phase in _TERMINAL
            if terminal:
                self._retire(run)  # Commit completion before cancellable control/send.
            await self._typing.update((chat, thread), active=not terminal, session_id=run.session)
            reply_markup: dict[str, ChannelValue] | None = None
            if phase == "waiting_for_approval":
                approval = (event.activation_id, event.call_id)
                if approval in run.waiting or run.approvals >= MAX_APPROVAL_NOTICES:
                    return
                run.waiting.add(approval)
                run.approvals += 1
                if self._approvals:
                    reply_markup = self._buttons(run, event)
                    text = approval_text(event.tool_name, self._token)
                else:
                    text = _APPROVAL_NOTICE
            elif phase in ("tool_requested", "tool_completed"):
                key = (phase, event.activation_id, event.call_id)
                if key in run.seen or len(run.seen) >= MAX_SEEN:
                    return
                run.seen.add(key)
                if run.tools >= MAX_TOOL_NOTICES or monotonic() < run.next_tool:
                    return
                text = tool_text(
                    event.tool_name,
                    event.tool_arguments,
                    self._token,
                    completed=phase == "tool_completed",
                    failed=event.tool_failed is True,
                )
                if not text:
                    return
                run.tools += 1
                run.next_tool = monotonic() + TOOL_INTERVAL
            else:
                return  # Lifecycle phases only move the typing indicator.
            await self._notify(run, text, reply_markup)

    def _buttons(self, run: _Run, event: ChannelExecutionEvent) -> dict[str, ChannelValue]:
        handle = secrets.token_urlsafe(12)
        self._handles[handle] = (run.chat, event.session_id, event.run_id, event.call_id)
        while len(self._handles) > MAX_HANDLES:
            self._handles.popitem(last=False)
        return {
            "inline_keyboard": [
                [
                    {"text": _APPROVE, "callback_data": f"{_APPROVAL_PREFIX}a:{handle}"},
                    {"text": _DENY, "callback_data": f"{_APPROVAL_PREFIX}d:{handle}"},
                ]
            ]
        }

    def approval(self, message: ChannelMessage) -> ChannelApproval | None:
        """Resolve one Approve/Deny tap; recognized-but-stale taps are consumed, never model input."""
        if not self._approvals:
            return None
        metadata = message.metadata
        if message.event_type != "callback_query" or not isinstance(metadata, dict):
            return None
        callback = metadata.get("callback_query")
        if not isinstance(callback, dict):
            return None
        data = callback.get("data")
        if not isinstance(data, str) or not data.startswith(_APPROVAL_PREFIX):
            return None
        parts = data.split(":")
        if len(parts) != 3 or parts[1] not in ("a", "d") or _HANDLE.fullmatch(parts[2]) is None:
            return ChannelApproval()
        identity = self._handles.get(parts[2])
        if identity is None or identity[0] != message.conversation_id:
            return ChannelApproval()
        del self._handles[parts[2]]
        return ChannelApproval(identity[0], identity[1], identity[2], identity[3], allow=parts[1] == "a")

    async def _notify(self, run: _Run, text: str, reply_markup: dict[str, ChannelValue] | None = None) -> None:
        if monotonic() < self._retry_at or not self._accepting:
            return
        payload: dict[str, ChannelValue] = {"chat_id": run.chat, "text": text}
        if run.thread:
            payload["message_thread_id"] = int(run.thread)
        if reply_markup is not None:
            payload["reply_markup"] = reply_markup
        try:
            async with asyncio.timeout(SEND_TIMEOUT):
                await self._transport.request("sendMessage", payload, timeout=1)
        except ChannelError as error:
            delay = error.retry_after
            if type(delay) in (int, float) and math.isfinite(delay) and delay > 0:
                self._retry_at = max(self._retry_at, monotonic() + delay)
        except Exception:
            pass  # Best effort, no retry, raw response, exception text or logging.

    async def close(self) -> None:
        self.disable()
        async with self._lock:
            self._runs.clear()
            self._latest.clear()
            self._retired.clear()
            self._handles.clear()
