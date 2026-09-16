"""Pure parsing of intentional Telegram host commands; never select sessions here."""

import re

from nagents.channels import ChannelCommand
from nagents.channels import ChannelMessage

_COMMAND = re.compile(r"\A/(sessions|session|new|compact)(?:@([A-Za-z0-9_]{1,32}))?(?=$|[ \t\r\n])")
_FORWARD_FIELDS = ("forward_origin", "forward_date", "forward_from", "forward_from_chat", "forward_sender_name")


def parse_command(message: ChannelMessage, username: str) -> ChannelCommand | None:
    if message.event_type != "message" or not isinstance(message.text, str) or not isinstance(message.metadata, dict):
        return None
    metadata = message.metadata
    if any(key in metadata for key in (*_FORWARD_FIELDS, "edit_date")) or metadata.get("is_automatic_forward"):
        return None
    if metadata.get("text_source", "text") != "text":
        return None
    try:
        text_units = len(message.text.encode("utf-16-le")) // 2
    except UnicodeEncodeError:
        return None
    match = _COMMAND.match(message.text)
    if match is None:
        return None
    name, recipient = match.groups()
    if recipient and (not username or recipient.lower() != username.lower()):
        return None
    if "entities" in metadata:
        entities = metadata["entities"]
        if not isinstance(entities, list):
            return None
        matches = 0
        for entity in entities:
            if not isinstance(entity, dict):
                return None
            offset, length = entity.get("offset"), entity.get("length")
            if (
                not isinstance(entity.get("type"), str)
                or type(offset) is not int
                or type(length) is not int
                or offset < 0
                or length <= 0
                or offset + length > text_units
            ):
                return None
            if offset == 0:
                # Command tokens are ASCII, so their length is also their UTF-16 length.
                if entity.get("type") != "bot_command" or length != match.end():
                    return None
                matches += 1
        if matches != 1:
            return None
    # If entities are absent entirely, accept only the same strict literal token.
    # Present but empty/malformed/mismatching entities never trigger this fallback.
    arguments = message.text[match.end() :].strip()
    if name in {"sessions", "compact"} and arguments:
        return None
    return ChannelCommand(name=name, arguments=arguments)
