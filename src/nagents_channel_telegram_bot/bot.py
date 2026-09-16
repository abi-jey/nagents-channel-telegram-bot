"""Public Nagents Channel implementation for a single Telegram bot consumer."""

from __future__ import annotations

import asyncio
import json
import os
import re
from dataclasses import replace
from typing import TYPE_CHECKING
from typing import Self

from nagents.channels import Channel
from nagents.channels import ChannelAction
from nagents.channels import ChannelActivity
from nagents.channels import ChannelApproval
from nagents.channels import ChannelAttachment
from nagents.channels import ChannelCommand
from nagents.channels import ChannelDelivery
from nagents.channels import ChannelError
from nagents.channels import ChannelFile
from nagents.channels import ChannelMessage
from nagents.channels import ChannelReceiver
from nagents.channels import ChannelSend
from nagents.channels import ChannelValue

from ._activity import Typing
from ._activity import finish_cleanup
from ._commands import parse_command
from ._execution import ExecutionNotifications
from ._mapping import UPDATE_TYPES
from ._mapping import map_update
from ._transport import Transport
from ._transport import retryable
from ._validation import base_origin
from ._validation import identifier
from ._validation import integer
from ._validation import object_value
from ._validation import text
from ._validation import username

if TYPE_CHECKING:
    from collections.abc import Sequence

    from nagents.channels import ChannelExecutionEvent

_POLL_BACKOFF = 1.0
_POLL_BACKOFF_MAX = 60.0
_MAX_ATTACHMENT_BYTES = 20 * 1024 * 1024
_MAX_OUTBOUND_FILES = 3
_MAX_CAPTION_UNITS = 1024
_PHOTO_TYPES = frozenset({"image/jpeg", "image/png"})


class TelegramBot(Channel):
    """Long-poll Telegram into a host; session routing is the host's policy.

    Construction is offline. The runtime owns cancellation of ``listen`` and
    calls ``close`` after that task stops. Each attached channel needs a distinct,
    stable ``name``. Only one process/consumer may poll a given bot token.
    """

    description = "Telegram bot: receive chat events; explicitly send, edit, or delete plain-text messages."
    capabilities = ("receive", "send_text", "send_files", "commands", "typing", "fetch_attachment", "approvals")
    actions = (
        ChannelAction(
            name="edit_message",
            description="Edit a Telegram message's plain text using its chat and Telegram message ID (not update ID).",
            parameters={
                "type": "object",
                "properties": {
                    "destination": {"type": "string", "description": "Nonzero decimal chat ID string; no usernames."},
                    "message_id": {"type": "string", "description": "Positive decimal Telegram message ID string."},
                    "text": {"type": "string", "description": "Nonempty plain text, at most 4096 UTF-16 code units."},
                },
                "required": ["destination", "message_id", "text"],
                "additionalProperties": False,
            },
        ),
        ChannelAction(
            name="delete_message",
            description="Delete a Telegram message by chat and Telegram message ID, subject to Telegram permissions.",
            parameters={
                "type": "object",
                "properties": {
                    "destination": {"type": "string", "description": "Nonzero decimal chat ID string; no usernames."},
                    "message_id": {"type": "string", "description": "Positive decimal Telegram message ID string."},
                },
                "required": ["destination", "message_id"],
                "additionalProperties": False,
            },
        ),
    )

    def __init__(
        self,
        token: str,
        *,
        name: str = "telegram",
        allowed_chat_ids: Sequence[str] = (),
        allowed_user_ids: Sequence[str] = (),
        allowed_usernames: Sequence[str] = (),
        private_chats_only: bool = False,
        execution_notifications: bool = False,
        chat_approvals: bool = False,
        poll_timeout: int = 30,
        base_url: str = "https://api.telegram.org",
    ) -> None:
        if not isinstance(token, str) or re.fullmatch(r"[0-9]+:[A-Za-z0-9_-]+", token) is None:
            raise ChannelError("Telegram bot token is missing or malformed")
        if not isinstance(name, str) or re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.-]{0,63}", name) is None:
            raise ChannelError("name must match [A-Za-z_][A-Za-z0-9_.-]{0,63}")
        if not isinstance(allowed_chat_ids, (list, tuple)):
            raise ChannelError("allowed_chat_ids must be a list or tuple of decimal chat ID strings")
        if not isinstance(allowed_user_ids, (list, tuple)):
            raise ChannelError("allowed_user_ids must be a list or tuple of positive decimal user ID strings")
        if not isinstance(allowed_usernames, (list, tuple)):
            raise ChannelError("allowed_usernames must be a list or tuple of username strings")
        if type(private_chats_only) is not bool:
            raise ChannelError("private_chats_only must be a boolean")
        if type(execution_notifications) is not bool:
            raise ChannelError("execution_notifications must be a boolean")
        if type(chat_approvals) is not bool:
            raise ChannelError("chat_approvals must be a boolean")
        if type(poll_timeout) is not int or not 1 <= poll_timeout <= 50:
            raise ChannelError("poll_timeout must be an integer from 1 to 50")
        self.name = name
        self._allowed_chats = frozenset(identifier(chat, "allowed_chat_ids entry") for chat in allowed_chat_ids)
        self._allowed_users = frozenset(
            identifier(user, "allowed_user_ids entry", positive=True) for user in allowed_user_ids
        )
        self._allowed_usernames = frozenset(username(user) for user in allowed_usernames)
        self._private_chats_only = private_chats_only
        self._poll_timeout = poll_timeout
        self._transport = Transport(token, base_origin(base_url))
        self._typing = Typing(self._transport)
        self._execution_notifications = execution_notifications
        self._chat_approvals = chat_approvals
        self._execution = ExecutionNotifications(self._transport, self._typing, token, approvals=chat_approvals)
        self._lifecycle_lock = asyncio.Lock()
        self._bot_id = 0
        self._bot_username = ""
        self._offset = 0
        self._opened = False
        self._opening = False
        self._closing = 0
        self._listening = False

    @classmethod
    def from_config(cls, config: dict[str, ChannelValue]) -> Self:
        """Accept a private host-injected token or an environment reference, never both."""
        keys = {
            "token",
            "token_env",
            "name",
            "allowed_chat_ids",
            "allowed_user_ids",
            "allowed_usernames",
            "private_chats_only",
            "execution_notifications",
            "chat_approvals",
            "poll_timeout",
        }
        if not isinstance(config, dict) or set(config) - keys:
            raise ChannelError("Unsupported Telegram configuration keys")
        if "token" in config and "token_env" in config:
            raise ChannelError("Provide token or token_env, not both")
        if "token" in config:
            token = config["token"]
            if not isinstance(token, str):
                raise ChannelError("token must be a string")
        else:
            token_env = config.get("token_env", "TELEGRAM_BOT_TOKEN")
            if not isinstance(token_env, str) or re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", token_env) is None:
                raise ChannelError("token_env must be an environment variable name")
            token = os.environ.get(token_env, "")
        name = config.get("name", "telegram")
        allowed = config.get("allowed_chat_ids", [])
        users = config.get("allowed_user_ids", [])
        usernames = config.get("allowed_usernames", [])
        private = config.get("private_chats_only", False)
        notifications = config.get("execution_notifications", False)
        approvals = config.get("chat_approvals", False)
        timeout = config.get("poll_timeout", 30)
        if not isinstance(name, str):
            raise ChannelError("name must be a string")
        if not isinstance(allowed, list) or any(not isinstance(chat, str) for chat in allowed):
            raise ChannelError("allowed_chat_ids must be a list of decimal chat ID strings")
        allowed_ids = [identifier(chat, "allowed_chat_ids entry") for chat in allowed]
        if not isinstance(users, list):
            raise ChannelError("allowed_user_ids must be a list of positive decimal user ID strings")
        user_ids = [identifier(user, "allowed_user_ids entry", positive=True) for user in users]
        if not isinstance(usernames, list):
            raise ChannelError("allowed_usernames must be a list of username strings")
        user_names = [username(user) for user in usernames]
        if type(private) is not bool:
            raise ChannelError("private_chats_only must be a boolean")
        if type(notifications) is not bool:
            raise ChannelError("execution_notifications must be a boolean")
        if type(approvals) is not bool:
            raise ChannelError("chat_approvals must be a boolean")
        if type(timeout) is not int:
            raise ChannelError("poll_timeout must be an integer from 1 to 50")
        return cls(
            token,
            name=name,
            allowed_chat_ids=allowed_ids,
            allowed_user_ids=user_ids,
            allowed_usernames=user_names,
            private_chats_only=private,
            execution_notifications=notifications,
            chat_approvals=approvals,
            poll_timeout=timeout,
        )

    async def open(self) -> None:
        if self._closing:
            raise ChannelError("Telegram channel is closing")
        if self._opening:
            raise ChannelError("Telegram channel is already opening")
        if self._opened:
            return
        self._opening = True
        try:
            async with self._lifecycle_lock:
                try:
                    if self._closing:
                        raise ChannelError("Telegram channel is closing")
                    self._transport.open()
                    me = object_value(await self._transport.request("getMe", {}))
                    self._bot_id = integer(me.get("id"))
                    if me.get("is_bot") is not True:
                        raise ChannelError("Telegram getMe did not identify a bot")
                    username = me.get("username", "")
                    if not isinstance(username, str) or (
                        username and not re.fullmatch(r"[A-Za-z0-9_]{1,32}", username)
                    ):
                        raise ChannelError("Telegram getMe returned an invalid bot username")
                    self._bot_username = username
                    if self._closing:
                        raise ChannelError("Telegram channel is closing")
                    self._typing.open()
                    self._execution.open()
                    self._opened = True
                except BaseException:
                    await finish_cleanup(self._transport.close())
                    raise
        finally:
            self._opening = False

    def command(self, message: ChannelMessage) -> ChannelCommand | None:
        """Recognize an explicit new-message command; the host owns its execution."""
        return parse_command(message, self._bot_username)

    def approval(self, message: ChannelMessage) -> ChannelApproval | None:
        """Resolve an inline Approve/Deny tap into a host-validated decision.

        Only active when ``chat_approvals`` rendered button prompts. A tap that is
        recognized but no longer matches a live prompt returns an empty decision,
        which the host consumes without enqueuing model input.
        """
        if not self._chat_approvals or not self._opened or self._closing:
            return None
        try:
            return self._execution.approval(message)
        except asyncio.CancelledError:
            raise
        except Exception:
            return None

    async def activity(self, event: ChannelActivity) -> None:
        """Best-effort typing, with local stop and session-scoped ownership."""
        chat = identifier(event.conversation_id, "conversation_id")
        if not isinstance(event.thread_id, str):
            raise ChannelError("thread_id must be a decimal ID string or an empty string")
        thread = identifier(event.thread_id, "thread_id", positive=True) if event.thread_id else ""
        if type(event.active) is not bool:
            raise ChannelError("active must be a boolean")
        if not isinstance(event.session_id, str):
            raise ChannelError("session_id must be a string")
        if self._allowed_chats and chat not in self._allowed_chats:
            return
        await self._typing.update((chat, thread), active=event.active, session_id=event.session_id)

    def _require_open(self) -> None:
        if not self._opened:
            raise ChannelError("Telegram channel is not open")

    async def on_event(self, event: ChannelExecutionEvent) -> None:
        """Render shared SDK execution events when opted in; the host owns authorization."""
        if not self._execution_notifications or not self._opened or self._closing:
            return
        try:
            chat = identifier(event.conversation_id, "execution conversation")
            if (self._allowed_chats and chat not in self._allowed_chats) or (
                self._private_chats_only and int(chat) < 0
            ):
                return
            await self._execution.handle(event)
        except asyncio.CancelledError:
            task = asyncio.current_task()
            if task is not None and task.cancelling():
                raise
        except Exception:
            pass  # Optional protocol hook: never fail model/tools or log plugin data.

    async def listen(self, receive: ChannelReceiver) -> None:
        self._require_open()
        if self._listening:
            raise ChannelError("Telegram channel already has a polling consumer")
        self._listening = True
        backoff = _POLL_BACKOFF
        try:
            while self._opened:
                try:
                    result = await self._transport.request(
                        "getUpdates",
                        {
                            "offset": self._offset,
                            "limit": 20,
                            "timeout": self._poll_timeout,
                            "allowed_updates": list(UPDATE_TYPES),
                        },
                        timeout=self._poll_timeout + 10,
                    )
                except ChannelError as error:
                    # A long-running bot must outlast a dropped long poll or a brief
                    # Telegram outage instead of stopping forever. Permanent failures
                    # (bad token, conflict) still stop the connector for the host.
                    if not retryable(error) or not self._opened:
                        raise
                    await asyncio.sleep(backoff)
                    backoff = min(backoff * 2, _POLL_BACKOFF_MAX)
                    continue
                backoff = _POLL_BACKOFF
                if not isinstance(result, list) or len(result) > 20:
                    raise ChannelError("Telegram returned an invalid update batch")
                updates = [object_value(item) for item in result]
                ids = [integer(update.get("update_id"), minimum=0) for update in updates]
                if ids != sorted(ids):
                    raise ChannelError("Telegram returned an unordered update batch")
                for update, update_id in zip(updates, ids, strict=True):
                    if update_id < self._offset:
                        continue
                    events = map_update(
                        update,
                        bot_id=self._bot_id,
                        allowed_chats=self._allowed_chats,
                        allowed_users=self._allowed_users,
                        allowed_usernames=self._allowed_usernames,
                        private_chats_only=self._private_chats_only,
                        callback_acknowledged=False,
                    )
                    for event in events:
                        # Authorize and validate before any protocol acknowledgement
                        # or host delivery (including host-command parsing).
                        if event.event_type == "callback_query":
                            acknowledged = await self._ack_callback(update)
                            event = replace(event, metadata={**event.metadata, "callback_acknowledged": acknowledged})
                        await receive(event)
                    # No await between durable acceptance and local offset advancement.
                    # The next getUpdates confirms this event remotely. Filtered events
                    # are intentionally discarded; receiver exceptions leave it pending.
                    self._offset = update_id + 1
        finally:
            self._listening = False

    async def _ack_callback(self, update: dict[str, ChannelValue]) -> bool:
        callback = update.get("callback_query")
        if not isinstance(callback, dict):
            return False
        callback_id = callback.get("id")
        if not isinstance(callback_id, str) or not callback_id:
            raise ChannelError("Telegram returned an invalid callback query ID")
        try:
            result = await self._transport.request("answerCallbackQuery", {"callback_query_id": callback_id}, timeout=5)
        except ChannelError:
            # Expired queries or an uncertain protocol acknowledgement must not
            # prevent durable admission of the underlying event. Never retry it.
            return False
        return result is True

    async def send(self, message: ChannelSend) -> ChannelDelivery:
        self._require_open()
        destination = identifier(message.destination, "destination")
        if not isinstance(message.text, str):
            raise ChannelError("text must be a string")
        content = text(message.text) if message.text else ""
        if not isinstance(message.attachments, tuple) or message.attachments:
            raise ChannelError("Outbound transport references are unsupported; send workspace files instead")
        if not isinstance(message.files, tuple):
            raise ChannelError("files must be a tuple of ChannelFile")
        if not content and not message.files:
            raise ChannelError("A channel message needs text or an attachment")
        if len(message.files) > _MAX_OUTBOUND_FILES:
            raise ChannelError("Too many outbound attachments")
        if not isinstance(message.metadata, dict) or message.metadata:
            raise ChannelError("Outbound metadata options are unsupported")
        payload: dict[str, ChannelValue] = {"chat_id": destination}
        for value, field in ((message.thread_id, "thread_id"), (message.reply_to, "reply_to")):
            if not isinstance(value, str):
                raise ChannelError(f"{field} must be a decimal ID string or an empty string")
            if value:
                number = int(identifier(value, field, positive=True))
                if field == "thread_id":
                    payload["message_thread_id"] = number
                else:
                    payload["reply_parameters"] = {"message_id": number, "allow_sending_without_reply": False}
        if not message.files:
            result = await self._transport.request("sendMessage", {**payload, "text": content})
            remote_id = self._confirmed_message(result, destination)
            return ChannelDelivery(
                message_ids=(remote_id,),
                metadata={"destination": destination, "thread_id": message.thread_id, "reply_to": message.reply_to},
            )
        return await self._send_files(destination, content, payload, message.files)

    async def _send_files(
        self, destination: str, content: str, payload: dict[str, ChannelValue], files: tuple[ChannelFile, ...]
    ) -> ChannelDelivery:
        """Upload files once each; photo-capable images use sendPhoto, everything else sendDocument.

        Telegram captions are limited to 1024 UTF-16 code units, so longer text is sent
        as its own message first and the files follow without a caption.
        """
        fields = {key: str(value) for key, value in payload.items() if key != "chat_id" and key != "reply_parameters"}
        reply = payload.get("reply_parameters")
        if isinstance(reply, dict):
            fields["reply_parameters"] = json.dumps(reply)
        caption = content if len(content.encode("utf-16-le")) // 2 <= _MAX_CAPTION_UNITS else ""
        if content and not caption:
            result = await self._transport.request("sendMessage", {**payload, "text": content})
            self._confirmed_message(result, destination)
        message_ids: list[str] = []
        for index, file in enumerate(files):
            if not isinstance(file, ChannelFile):
                raise ChannelError("files must be a tuple of ChannelFile")
            if not isinstance(file.data, bytes) or not file.data:
                raise ChannelError("Outbound attachment is empty")
            if len(file.data) > _MAX_ATTACHMENT_BYTES:
                raise ChannelError("Telegram attachment exceeds the upload limit")
            name = file.filename or "file"
            media_type = file.media_type or "application/octet-stream"
            method, field = ("sendPhoto", "photo") if media_type in _PHOTO_TYPES else ("sendDocument", "document")
            request_fields = dict(fields)
            request_fields["chat_id"] = destination
            if index == 0 and caption:
                request_fields["caption"] = caption
            result = await self._transport.request_multipart(
                method, request_fields, ((field, name, media_type, file.data),)
            )
            message_ids.append(self._confirmed_message(result, destination))
        return ChannelDelivery(
            message_ids=tuple(message_ids),
            metadata={
                "destination": destination,
                "thread_id": str(payload.get("message_thread_id", "")),
                "reply_to": "",
            },
        )

    @staticmethod
    def _confirmed_message(result: ChannelValue, destination: str) -> str:
        try:
            message = object_value(result)
            remote_id = str(integer(message.get("message_id")))
            chat = object_value(message.get("chat"))
            if str(integer(chat.get("id"), minimum=-(2**63 - 1))) != destination:
                raise ChannelError("Invalid destination")
        except ChannelError:
            raise ChannelError("Telegram returned an invalid delivery confirmation", outcome_unknown=True) from None
        return remote_id

    async def action(self, name: str, arguments: dict[str, ChannelValue]) -> dict[str, ChannelValue]:
        self._require_open()
        if name not in ("edit_message", "delete_message"):
            raise ChannelError("Unsupported Telegram action")
        required = {"destination", "message_id"}
        if name == "edit_message":
            required.add("text")
        if not isinstance(arguments, dict) or set(arguments) != required:
            raise ChannelError("Action arguments must contain exactly the advertised required fields")
        destination = identifier(arguments["destination"], "destination")
        message_id = identifier(arguments["message_id"], "message_id", positive=True)
        payload: dict[str, ChannelValue] = {"chat_id": destination, "message_id": int(message_id)}
        if name == "edit_message":
            payload["text"] = text(arguments["text"])
            result = await self._transport.request("editMessageText", payload)
            if self._confirmed_message(result, destination) != message_id:
                raise ChannelError("Telegram returned an unexpected edited message ID", outcome_unknown=True)
        else:
            result = await self._transport.request("deleteMessage", payload)
            if result is not True:
                raise ChannelError("Telegram did not confirm deletion", outcome_unknown=True)
        return {"message_id": message_id, "destination": destination, "ok": True}

    async def fetch_attachment(self, attachment: ChannelAttachment) -> tuple[bytes, str]:
        """Download one referenced Telegram file for model input.

        The host caps and allowlists what reaches the model; this method only
        performs the authenticated ``getFile`` and byte download within Telegram's
        20 MiB bot limit and refuses references it did not issue.
        """
        self._require_open()
        if not isinstance(attachment, ChannelAttachment):
            raise ChannelError("fetch_attachment requires a ChannelAttachment")
        prefix = "telegram:file:"
        file_id = attachment.reference.removeprefix(prefix) if isinstance(attachment.reference, str) else ""
        if not attachment.reference.startswith(prefix) or re.fullmatch(r"[A-Za-z0-9_-]{1,256}", file_id) is None:
            raise ChannelError("Unsupported Telegram attachment reference")
        if attachment.size > _MAX_ATTACHMENT_BYTES:
            raise ChannelError("Telegram attachment exceeds the download limit")
        file = object_value(await self._transport.request("getFile", {"file_id": file_id}))
        file_path = file.get("file_path")
        if not isinstance(file_path, str) or not file_path:
            raise ChannelError("Telegram did not return a downloadable file path")
        size = file.get("file_size")
        if type(size) is int and size > _MAX_ATTACHMENT_BYTES:
            raise ChannelError("Telegram attachment exceeds the download limit")
        data = await self._transport.download(file_path, limit=_MAX_ATTACHMENT_BYTES)
        media_type = attachment.media_type
        return data, media_type if isinstance(media_type, str) and media_type else "application/octet-stream"

    async def close(self) -> None:
        """Stop all typing and release HTTP after the host stops its listen task."""
        self._closing += 1
        self._opened = False
        self._typing.disable()
        self._execution.disable()
        try:
            await finish_cleanup(self._close_resources())
        finally:
            self._closing -= 1

    async def _close_resources(self) -> None:
        async with self._lifecycle_lock:
            try:
                await self._execution.close()
                await self._typing.close()
            finally:
                await self._transport.close()
