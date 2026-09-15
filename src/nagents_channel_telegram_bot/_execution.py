"""Telegram-specific, bounded execution rendering over the shared Channel hook."""

from __future__ import annotations

import asyncio
import math
import re
from dataclasses import dataclass
from dataclasses import field
from time import monotonic
from typing import TYPE_CHECKING

from nagents.channels import ChannelError

from ._validation import identifier

if TYPE_CHECKING:
    from nagents.channels import ChannelExecutionEvent
    from nagents.channels import ChannelValue

    from ._activity import Typing
    from ._transport import Transport

MAX_RUNS = 64
MAX_RETIRED = 256
MAX_TOOL_NOTICES = 8
MAX_APPROVAL_NOTICES = 8
MAX_SEEN = 128
TOOL_INTERVAL = 2.0
SEND_TIMEOUT = 1.0
_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_.:-]{0,63}")
_KEY = re.compile(r"[A-Za-z_][A-Za-z0-9_.-]{0,63}")
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
    def __init__(self, transport: Transport, typing: Typing, token: str) -> None:
        self._transport = transport
        self._typing = typing
        self._token = token
        self._runs: dict[str, _Run] = {}
        self._latest: dict[tuple[str, str], str] = {}
        self._retired: dict[str, None] = {}
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
            if phase == "waiting_for_approval":
                approval = (event.activation_id, event.call_id)
                if approval in run.waiting or run.approvals >= MAX_APPROVAL_NOTICES:
                    return
                run.waiting.add(approval)
                run.approvals += 1
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
            await self._notify(run, text)

    async def _notify(self, run: _Run, text: str) -> None:
        if monotonic() < self._retry_at or not self._accepting:
            return
        payload: dict[str, ChannelValue] = {"chat_id": run.chat, "text": text}
        if run.thread:
            payload["message_thread_id"] = int(run.thread)
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
