"""Installable Telegram Bot API channel; importing never connects."""

from ._plugin import plugin
from .bot import TelegramBot

__all__ = ["TelegramBot", "plugin"]
