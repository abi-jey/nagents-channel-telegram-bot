"""Map supported updates to references and provenance, never download files."""

from nagents.channels import ChannelAttachment
from nagents.channels import ChannelError
from nagents.channels import ChannelMessage
from nagents.channels import ChannelValue

from ._validation import integer
from ._validation import object_value

UPDATE_TYPES = ("message", "edited_message", "channel_post", "edited_channel_post", "callback_query")
_MEDIA_TYPES = {
    "photo": "image/jpeg",
    "animation": "video/mp4",
    "audio": "audio/mpeg",
    "document": "application/octet-stream",
    "video": "video/mp4",
    "video_note": "video/mp4",
    "voice": "audio/ogg",
    "sticker": "image/webp",
}
_FILE_FIELDS = ("file_id", "file_unique_id", "file_name", "file_size", "mime_type", "width", "height", "duration")
_PROVENANCE_FIELDS = (
    "chat",
    "from",
    "sender_chat",
    "date",
    "edit_date",
    "message_thread_id",
    "media_group_id",
    "author_signature",
    "forward_origin",
    "forward_date",
    "forward_from",
    "forward_from_chat",
    "forward_sender_name",
    "external_reply",
    "quote",
    "is_automatic_forward",
)


def _string(value: ChannelValue) -> str:
    return value if isinstance(value, str) else ""


def _photo_area(value: ChannelValue) -> int:
    photo = object_value(value)
    return integer(photo.get("width")) * integer(photo.get("height"))


def _entities(value: ChannelValue) -> ChannelValue:
    if not isinstance(value, list):
        return None
    result: list[ChannelValue] = []
    for entity in value:
        if not isinstance(entity, dict):
            result.append(None)
            continue
        kind, offset, length = entity.get("type"), entity.get("offset"), entity.get("length")
        result.append(
            {
                "type": kind if isinstance(kind, str) else None,
                "offset": offset if type(offset) is int else None,
                "length": length if type(length) is int else None,
            }
        )
    return result


def _attachments(message: dict[str, ChannelValue]) -> tuple[tuple[ChannelAttachment, ...], list[ChannelValue]]:
    attachments: list[ChannelAttachment] = []
    metadata: list[ChannelValue] = []
    for kind, default_type in _MEDIA_TYPES.items():
        file = message.get(kind)
        if kind == "document" and "animation" in message:
            continue  # Telegram also exposes animations as documents.
        if kind == "photo" and isinstance(file, list) and file:
            file = max(file, key=_photo_area)
        if not isinstance(file, dict):
            continue
        file_id = _string(file.get("file_id"))
        if not file_id:
            continue
        if kind == "sticker":
            if file.get("is_video") is True:
                default_type = "video/webm"
            elif file.get("is_animated") is True:
                default_type = "application/x-tgsticker"
        size = file.get("file_size", 0)
        attachments.append(
            ChannelAttachment(
                reference=f"telegram:file:{file_id}",
                media_type=_string(file.get("mime_type")) or default_type,
                filename=_string(file.get("file_name")),
                size=integer(size, minimum=0),
            )
        )
        metadata.append({"kind": kind, **{key: file[key] for key in _FILE_FIELDS if key in file}})
    return tuple(attachments), metadata


def map_update(
    update: dict[str, ChannelValue], *, bot_id: int, allowed_chats: frozenset[str], callback_acknowledged: bool
) -> tuple[ChannelMessage, ...]:
    """An empty tuple deliberately filters an update; malformed supported data raises."""
    kinds = [kind for kind in UPDATE_TYPES if kind in update]
    if not kinds:
        return ()
    if len(kinds) != 1:
        raise ChannelError("Telegram returned an ambiguous update")
    kind = kinds[0]
    payload = object_value(update[kind])
    is_callback = kind == "callback_query"
    if is_callback:
        if "message" not in payload:  # Inline callbacks have no addressable chat.
            return ()
        message = object_value(payload["message"])
    else:
        message = payload
    # These contexts need destination fields not supported by this connector.
    if any(key in message for key in ("business_connection_id", "direct_messages_topic", "guest_query_id")):
        return ()
    chat = object_value(message.get("chat"))
    chat_id = str(integer(chat.get("id"), minimum=-(2**63 - 1)))
    if chat_id == "0" or (allowed_chats and chat_id not in allowed_chats):
        return ()
    telegram_id = integer(message.get("message_id"), minimum=0)
    if telegram_id == 0:
        return ()  # Ephemeral/scheduled messages cannot be addressed yet.
    sender = payload.get("from") if is_callback else message.get("from")
    if isinstance(sender, dict) and sender.get("id") == bot_id:
        return ()
    sender_chat = message.get("sender_chat") if not is_callback else None
    sender_id = ""
    if isinstance(sender_chat, dict):
        sender_id = str(integer(sender_chat.get("id"), minimum=-(2**63 - 1)))
    elif isinstance(sender, dict):
        sender_id = str(integer(sender.get("id")))
    elif kind in ("channel_post", "edited_channel_post"):
        sender_id = chat_id
    if not sender_id or sender_id == "0":
        raise ChannelError("Telegram returned an invalid sender")
    metadata: dict[str, ChannelValue] = {
        "update_id": integer(update.get("update_id"), minimum=0),
        "telegram_message_id": str(telegram_id),
        **{key: message[key] for key in _PROVENANCE_FIELDS if key in message},
    }
    reply_to = str(telegram_id)
    reply = message.get("reply_to_message")
    if isinstance(reply, dict):
        metadata["in_reply_to"] = str(integer(reply.get("message_id")))
        metadata["reply_to_message"] = {
            key: reply[key]
            for key in ("message_id", "chat", "from", "sender_chat", "message_thread_id", "date")
            if key in reply
        }
    thread_id = ""
    if "message_thread_id" in message:
        thread_id = str(integer(message["message_thread_id"]))
    if is_callback:
        content = _string(payload.get("data")) or _string(payload.get("game_short_name"))
        metadata["callback_query"] = {
            key: payload[key] for key in ("id", "from", "data", "game_short_name", "chat_instance") if key in payload
        }
        metadata["callback_acknowledged"] = callback_acknowledged
        # The button's source message is reply provenance, not new inbound media.
        attachments: tuple[ChannelAttachment, ...] = ()
        reply_to = str(telegram_id)
    else:
        content = _string(message.get("text")) or _string(message.get("caption"))
        metadata["text_source"] = "text" if _string(message.get("text")) else "caption"
        for field in ("entities", "caption_entities"):
            if field in message:
                # Keep only entity type/range, never arbitrary URL/user/credential fields.
                # Retain malformed entries as null so command parsing fails closed.
                metadata[field] = _entities(message[field])
        attachments, files = _attachments(message)
        if not content and not attachments:
            return ()
        if files:
            metadata["files"] = files
    return (
        ChannelMessage(
            message_id=str(integer(update.get("update_id"), minimum=0)),
            conversation_id=chat_id,
            sender_id=sender_id,
            text=content,
            thread_id=thread_id,
            reply_to=reply_to,
            event_type=kind,
            attachments=attachments,
            metadata=metadata,
        ),
    )
