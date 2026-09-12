"""Offline discovery metadata; credential values never enter the descriptor."""

from nagents.channels import ChannelPlugin

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
                "description": "Admit these numeric chat IDs; empty admits all visible supported chats.",
                "items": {"type": "string", "pattern": "^-?[1-9][0-9]{0,18}$"},
                "default": [],
            },
            "poll_timeout": {
                "type": "integer",
                "description": "Long-poll timeout in seconds.",
                "minimum": 1,
                "maximum": 50,
                "default": 30,
            },
        },
        "additionalProperties": False,
    },
    factory=TelegramBot.from_config,
)
