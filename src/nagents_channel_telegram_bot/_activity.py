"""Bounded, owned Telegram typing keepalives with cancellation-safe cleanup."""

import asyncio
import math
from collections.abc import Awaitable
from collections.abc import Callable
from collections.abc import Coroutine
from dataclasses import dataclass
from time import monotonic

from nagents.channels import ChannelError
from nagents.channels import ChannelValue

from ._transport import Transport

MAX_TYPING_TARGETS = 64
TYPING_INTERVAL = 4.0


async def finish_cleanup(operation: Coroutine[object, object, None]) -> None:
    """Join owned cleanup even under repeated cancellation, then propagate it."""
    task = asyncio.create_task(operation)
    cancelled = False
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            cancelled = True
    task.result()
    if cancelled:
        raise asyncio.CancelledError


async def _drain(tasks: tuple[asyncio.Task[None], ...]) -> None:
    await asyncio.gather(*tasks, return_exceptions=True)


@dataclass(frozen=True)
class _Indicator:
    session_id: str
    task: asyncio.Task[None]
    ready: asyncio.Event


class Typing:
    def __init__(self, transport: Transport) -> None:
        self._transport = transport
        self._entries: dict[tuple[str, str], _Indicator] = {}
        self._lock = asyncio.Lock()
        self._accepting = False
        self._retry_at = 0.0
        self._sleep: Callable[[float], Awaitable[None]] = asyncio.sleep
        self._clock: Callable[[], float] = monotonic

    def open(self) -> None:
        self._accepting = True

    def disable(self) -> None:
        # Synchronous admission barrier, before any shutdown await.
        self._accepting = False

    async def update(self, key: tuple[str, str], *, active: bool, session_id: str) -> None:
        if not self._accepting:
            return
        created = False
        async with self._lock:
            if not self._accepting:
                return
            current = self._entries.get(key)
            if not active:
                if current is not None and current.session_id == session_id:
                    await self._retire(key, current)
                return
            if current is not None and (current.session_id != session_id or current.task.done()):
                await self._retire(key, current)
                current = None
            if current is None:
                if not self._accepting or len(self._entries) >= MAX_TYPING_TARGETS:
                    return
                ready = asyncio.Event()
                task = asyncio.create_task(self._keepalive(key, ready), name="telegram-typing")
                task.add_done_callback(lambda _: ready.set())
                current = _Indicator(session_id, task, ready)
                self._entries[key] = current
                created = True
        try:
            # The first attempt is immediate (or deferred by a known rate limit).
            # Do not hold the mutation lock during HTTP: stop/close can cancel it.
            await current.ready.wait()
        except asyncio.CancelledError:
            if created:
                await finish_cleanup(self._stop_current(key, current))
            raise

    async def _retire(self, key: tuple[str, str], current: _Indicator) -> None:
        current.task.cancel()
        try:
            await finish_cleanup(_drain((current.task,)))
        finally:
            self._entries.pop(key, None)

    async def _stop_current(self, key: tuple[str, str], current: _Indicator) -> None:
        async with self._lock:
            if self._entries.get(key) is current:
                await self._retire(key, current)

    async def _keepalive(self, key: tuple[str, str], ready: asyncio.Event) -> None:
        chat, thread = key
        payload: dict[str, ChannelValue] = {"chat_id": chat, "action": "typing"}
        if thread:
            payload["message_thread_id"] = int(thread)
        try:
            while True:
                cooldown = self._retry_at - self._clock()
                if cooldown > 0:
                    ready.set()
                    await self._sleep(cooldown)
                    continue
                try:
                    await self._transport.request("sendChatAction", payload, timeout=5)
                except ChannelError as error:
                    delay = error.retry_after
                    if math.isfinite(delay) and delay > 0:
                        self._retry_at = max(self._retry_at, self._clock() + delay)
                except Exception:
                    # Indicators are advisory. Never leak background exception text
                    # or fail a model run, even if an unexpected transport error occurs.
                    pass
                ready.set()
                await self._sleep(max(TYPING_INTERVAL, self._retry_at - self._clock()))
        finally:
            ready.set()

    async def close(self) -> None:
        self.disable()
        async with self._lock:
            tasks = tuple(entry.task for entry in self._entries.values())
            for task in tasks:
                task.cancel()
            try:
                await finish_cleanup(_drain(tasks))
            finally:
                self._entries.clear()
