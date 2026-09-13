"""Offline discovery metadata; credential values never enter the descriptor."""

from nagents.channels import ChannelPlugin

from ._validation import MESSAGE_PATTERN
from ._validation import USERNAME_PATTERN
from .bot import TelegramBot

plugin = ChannelPlugin(
    name="Telegram Bot",
    description="Telegram long polling, explicit text/edit/delete tools, host session commands and typing indicators.",
    config_schema={
        "type": "object",
        "properties": {
            "name": {
                "type": "string",
                "description": "Stable channel name; the web host injects its connection ID.",
                "pattern": "^[A-Za-z_][A-Za-z0-9_.-]{0,63}$",
                "readOnly": True,
            },
            "token": {
                "type": "string",
                "description": "Bot token, privately injected by the host. Omit when using token_env.",
                "writeOnly": True,
                "minLength": 1,
            },
            "token_env": {
                "type": "string",
                "description": "Environment variable containing the token. Omit when providing token directly.",
                "pattern": "^[A-Za-z_][A-Za-z0-9_]*$",
            },
            "allowed_chat_ids": {
                "type": "array",
                "description": (
                    "Inbound chat IDs (signed 64-bit); AND user/private filters. Empty leaves chats unconstrained."
                ),
                "items": {"type": "string", "pattern": "^-?[1-9][0-9]{0,18}$"},
                "default": [],
            },
            "allowed_user_ids": {
                "type": "array",
                "description": (
                    "Trusted positive user ID strings (max 2^63-1), OR allowed_usernames; AND chats. "
                    "Either nonempty user list requires a valid human Telegram sender; "
                    "both empty disable user filtering."
                ),
                "items": {"type": "string", "pattern": MESSAGE_PATTERN, "maxLength": 19},
                "default": [],
            },
            "allowed_usernames": {
                "type": "array",
                "description": (
                    "Trusted Telegram from.username values, case-insensitive, optional leading @ in config. "
                    "5-32 ASCII letters/digits/underscores; OR allowed_user_ids, AND chats. "
                    "Usernames can change owners; prefer IDs for stable identity. Both empty disable user filtering."
                ),
                "items": {"type": "string", "pattern": USERNAME_PATTERN, "minLength": 5, "maxLength": 33},
                "default": [],
            },
            "private_chats_only": {
                "type": "boolean",
                "description": (
                    "Only human senders in private chats whose chat ID equals the acting user's ID "
                    "(callback_query.from for buttons). Rejects shared group/channel ingress."
                ),
                "default": False,
            },
            "poll_timeout": {
                "type": "integer",
                "description": "Long-poll timeout in seconds.",
                "minimum": 1,
                "maximum": 50,
                "default": 30,
            },
            "execution_notifications": {
                "type": "boolean",
                "description": (
                    "Opt in to shared Channel execution hooks: working/approval/terminal states, compact tool "
                    "notices and typing. Host must authorize the owning chat; no final assistant text is broadcast."
                ),
                "default": False,
            },
        },
        "additionalProperties": False,
    },
    factory=TelegramBot.from_config,
)
