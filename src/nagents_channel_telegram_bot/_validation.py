"""Strict boundary validation without reflecting configuration or server data."""

import ipaddress
import math
import re
from contextlib import suppress
from urllib.parse import urlsplit

from nagents.channels import ChannelError
from nagents.channels import ChannelValue

MAX_ID = 2**63 - 1
CHAT_PATTERN = r"^-?[1-9][0-9]{0,18}$"
MESSAGE_PATTERN = r"^[1-9][0-9]{0,18}$"
USERNAME_PATTERN = r"^@?[A-Za-z0-9_]{5,32}$"


def identifier(value: object, field: str, *, positive: bool = False) -> str:
    pattern = MESSAGE_PATTERN if positive else CHAT_PATTERN
    if not isinstance(value, str) or re.fullmatch(pattern, value) is None or abs(int(value)) > MAX_ID:
        raise ChannelError(f"{field} must be a canonical {'positive ' if positive else 'nonzero '}decimal ID string")
    return value


def integer(value: object, *, minimum: int = 1, maximum: int = MAX_ID) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        raise ChannelError("Telegram returned an invalid integer identifier or parameter")
    return value


def username(value: object, *, allow_prefix: bool = True) -> str:
    if (
        not isinstance(value, str)
        or re.fullmatch(USERNAME_PATTERN, value) is None
        or (not allow_prefix and value.startswith("@"))
    ):
        raise ChannelError(
            "username must contain 5-32 ASCII letters, digits or underscores; config permits one leading @"
        )
    return value.removeprefix("@").lower()


def text(value: object) -> str:
    if not isinstance(value, str) or not value:
        raise ChannelError("text must be a nonempty string")
    try:
        length = len(value.encode("utf-16-le")) // 2
    except UnicodeEncodeError:
        raise ChannelError("text must contain valid Unicode") from None
    if length > 4096:
        raise ChannelError("text exceeds the 4096 UTF-16 code-unit limit; split it explicitly before sending")
    return value


def json_value(value: object) -> ChannelValue:
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float) and math.isfinite(value):
        return value
    if isinstance(value, list):
        return [json_value(item) for item in value]
    if isinstance(value, dict):
        result: dict[str, ChannelValue] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise ValueError("Invalid JSON key")
            result[key] = json_value(item)
        return result
    raise ValueError("Invalid Telegram JSON value")


def object_value(value: ChannelValue) -> dict[str, ChannelValue]:
    if not isinstance(value, dict):
        raise ChannelError("Telegram returned an invalid object")
    return value


def base_origin(value: str) -> str:
    valid = False
    try:
        url = urlsplit(value)
        host = url.hostname or ""
        loopback = host == "localhost"
        if not loopback:
            with suppress(ValueError):
                loopback = ipaddress.ip_address(host).is_loopback
        valid = (
            bool(host)
            and (url.scheme == "https" or (url.scheme == "http" and loopback))
            and url.username is None
            and url.password is None
            and url.path in ("", "/")
            and not url.query
            and not url.fragment
            and not any(c.isspace() for c in value)
            and "?" not in value
            and "#" not in value
            and (url.port is None or url.port > 0)
        )
    except (TypeError, ValueError, AttributeError):
        pass
    if not valid:
        raise ChannelError("base_url must be an HTTPS origin, or an HTTP loopback origin for local tests")
    return value.rstrip("/")
