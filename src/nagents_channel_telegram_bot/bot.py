"""Public Nagents Channel implementation for a single Telegram bot consumer."""

import os
import re
from collections.abc import Sequence
from typing import Self

from nagents.channels import Channel
from nagents.channels import ChannelAction
from nagents.channels import ChannelDelivery
from nagents.channels import ChannelError
from nagents.channels import ChannelReceiver
from nagents.channels import ChannelSend
from nagents.channels import ChannelValue

from ._mapping import UPDATE_TYPES
from ._mapping import map_update
from ._transport import Transport
from ._validation import base_origin
from ._validation import identifier
from ._validation import integer
from ._validation import object_value
from ._validation import text


class TelegramBot(Channel):
    """Long-poll Telegram into the Agent listener's single shared session.

    Construction is offline. The runtime owns cancellation of ``listen`` and
    calls ``close`` after that task stops. Each attached channel needs a distinct,
    stable ``name``. Only one process/consumer may poll a given bot token.
    """

    description = "Telegram bot: receive chat events; explicitly send, edit, or delete plain-text messages."
    capabilities = ("receive", "send_text")
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
        poll_timeout: int = 30,
        base_url: str = "https://api.telegram.org",
    ) -> None:
        if not isinstance(token, str) or re.fullmatch(r"[0-9]+:[A-Za-z0-9_-]+", token) is None:
            raise ChannelError("Telegram bot token is missing or malformed")
        if not isinstance(name, str) or re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.-]{0,63}", name) is None:
            raise ChannelError("name must match [A-Za-z_][A-Za-z0-9_.-]{0,63}")
        if not isinstance(allowed_chat_ids, (list, tuple)):
            raise ChannelError("allowed_chat_ids must be a list or tuple of decimal chat ID strings")
        if type(poll_timeout) is not int or not 1 <= poll_timeout <= 50:
            raise ChannelError("poll_timeout must be an integer from 1 to 50")
        self.name = name
        self._allowed_chats = frozenset(identifier(chat, "allowed_chat_ids entry") for chat in allowed_chat_ids)
        self._poll_timeout = poll_timeout
        self._transport = Transport(token, base_origin(base_url))
        self._bot_id = 0
        self._offset = 0
        self._opened = False
        self._opening = False
        self._listening = False

    @classmethod
    def from_config(cls, config: dict[str, ChannelValue]) -> Self:
        """Entry-point factory. Credentials are read only from the named environment variable."""
        if not isinstance(config, dict) or set(config) - {"token_env", "name", "allowed_chat_ids", "poll_timeout"}:
            raise ChannelError("Unsupported Telegram configuration keys")
        token_env = config.get("token_env", "TELEGRAM_BOT_TOKEN")
        name = config.get("name", "telegram")
        allowed = config.get("allowed_chat_ids", [])
        timeout = config.get("poll_timeout", 30)
        if not isinstance(token_env, str) or re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", token_env) is None:
            raise ChannelError("token_env must be an environment variable name")
        if not isinstance(name, str):
            raise ChannelError("name must be a string")
        if not isinstance(allowed, list) or any(not isinstance(chat, str) for chat in allowed):
            raise ChannelError("allowed_chat_ids must be a list of decimal chat ID strings")
        allowed_ids = [identifier(chat, "allowed_chat_ids entry") for chat in allowed]
        if type(timeout) is not int:
            raise ChannelError("poll_timeout must be an integer from 1 to 50")
        return cls(os.environ.get(token_env, ""), name=name, allowed_chat_ids=allowed_ids, poll_timeout=timeout)

    async def open(self) -> None:
        if self._opening:
            raise ChannelError("Telegram channel is already opening")
        if self._opened:
            return
        self._opening = True
        try:
            self._transport.open()
            me = object_value(await self._transport.request("getMe", {}))
            self._bot_id = integer(me.get("id"))
            if me.get("is_bot") is not True:
                raise ChannelError("Telegram getMe did not identify a bot")
            self._opened = True
        except BaseException:
            await self._transport.close()
            raise
        finally:
            self._opening = False

    def _require_open(self) -> None:
        if not self._opened:
            raise ChannelError("Telegram channel is not open")

    async def listen(self, receive: ChannelReceiver) -> None:
        self._require_open()
        if self._listening:
            raise ChannelError("Telegram channel already has a polling consumer")
        self._listening = True
        try:
            while self._opened:
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
                if not isinstance(result, list) or len(result) > 20:
                    raise ChannelError("Telegram returned an invalid update batch")
                updates = [object_value(item) for item in result]
                ids = [integer(update.get("update_id"), minimum=0) for update in updates]
                if ids != sorted(ids):
                    raise ChannelError("Telegram returned an unordered update batch")
                for update, update_id in zip(updates, ids, strict=True):
                    if update_id < self._offset:
                        continue
                    acknowledged = await self._ack_callback(update)
                    for event in map_update(
                        update,
                        bot_id=self._bot_id,
                        allowed_chats=self._allowed_chats,
                        callback_acknowledged=acknowledged,
                    ):
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
        content = text(message.text)
        if not isinstance(message.attachments, tuple) or message.attachments:
            raise ChannelError("Outbound attachments are unsupported; inbound files are metadata-only references")
        if not isinstance(message.metadata, dict) or message.metadata:
            raise ChannelError("Outbound metadata options are unsupported")
        payload: dict[str, ChannelValue] = {"chat_id": destination, "text": content}
        for value, field in ((message.thread_id, "thread_id"), (message.reply_to, "reply_to")):
            if not isinstance(value, str):
                raise ChannelError(f"{field} must be a decimal ID string or an empty string")
            if value:
                number = int(identifier(value, field, positive=True))
                if field == "thread_id":
                    payload["message_thread_id"] = number
                else:
                    payload["reply_parameters"] = {"message_id": number, "allow_sending_without_reply": False}
        result = await self._transport.request("sendMessage", payload)
        remote_id = self._confirmed_message(result, destination)
        return ChannelDelivery(
            message_ids=(remote_id,),
            metadata={"destination": destination, "thread_id": message.thread_id, "reply_to": message.reply_to},
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

    async def close(self) -> None:
        """Release HTTP resources after the runtime cancels and awaits its listen task."""
        self._opened = False
        await self._transport.close()
