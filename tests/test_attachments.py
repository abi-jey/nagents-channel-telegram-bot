"""Attachment presentation fields and authenticated downloads, offline."""

import pytest
from nagents.channels import ChannelAttachment
from nagents.channels import ChannelError
from nagents.channels import ChannelMessage
from nagents.channels import ChannelValue

from nagents_channel_telegram_bot import TelegramBot
from nagents_channel_telegram_bot._mapping import map_update

from .conftest import TOKEN
from .conftest import TelegramServer
from .conftest import failure
from .conftest import message
from .conftest import ok
from .conftest import update

FILE_ID = "AgADoffline-file-id"
REFERENCE = f"telegram:file:{FILE_ID}"
PAYLOAD = b"offline-image-bytes"


def mapped(body: dict[str, ChannelValue] | None = None, update_id: int = 100) -> ChannelMessage:
    return map_update(
        update(update_id, body=message() if body is None else body),
        bot_id=0,
        allowed_chats=frozenset(),
        callback_acknowledged=False,
    )[0]


def test_capability_is_advertised() -> None:
    assert "fetch_attachment" in TelegramBot.capabilities


def test_mapping_exposes_presentation_fields() -> None:
    event = mapped()
    assert event.sent_at == 1700000000.0
    assert event.sender_name == "Test sender"
    assert event.sender_username == ""
    assert event.conversation_type == "supergroup"


def test_mapping_exposes_username_and_private_chat() -> None:
    body = message(chat_id=42, sender_id=42)
    body["from"] = {
        "id": 42,
        "is_bot": False,
        "first_name": "Abbas",
        "last_name": "Jafari",
        "username": "realabja",
    }
    body["chat"] = {"id": 42, "type": "private", "first_name": "Abbas", "username": "realabja"}
    event = mapped(body)
    assert event.sender_name == "Abbas Jafari"
    assert event.sender_username == "realabja"
    assert event.conversation_type == "private"


def test_mapping_uses_chat_title_for_channel_senders() -> None:
    body = message(chat_id=-100)
    body.pop("from")
    body["chat"] = {"id": -100, "type": "channel", "title": "Announcements"}
    mapped_event = map_update(
        update(100, kind="channel_post", body=body),
        bot_id=0,
        allowed_chats=frozenset(),
        callback_acknowledged=False,
    )[0]
    assert mapped_event.sender_name == "Announcements"
    assert mapped_event.sender_username == ""
    assert mapped_event.conversation_type == "channel"


async def test_fetch_attachment_downloads_within_the_limit(bot: TelegramBot, server: TelegramServer) -> None:
    server.responses["getFile"].append(ok({"file_path": "photos/file-1.jpg", "file_size": len(PAYLOAD)}))
    server.files["photos/file-1.jpg"] = PAYLOAD
    attachment = ChannelAttachment(REFERENCE, "image/jpeg", "picture.jpg", len(PAYLOAD))
    data, media_type = await bot.fetch_attachment(attachment)
    assert data == PAYLOAD
    assert media_type == "image/jpeg"
    assert server.calls("getFile") == [{"file_id": FILE_ID}]
    assert server.downloads == ["photos/file-1.jpg"]
    assert TOKEN not in str(server.calls("getFile"))


@pytest.mark.parametrize(
    "reference",
    [
        "",
        "file-id",
        "telegram:file:",
        "telegram:file:bad id",
        "telegram:file:" + "x" * 257,
        "other:file:abc",
    ],
)
async def test_fetch_attachment_rejects_foreign_references(
    bot: TelegramBot, server: TelegramServer, reference: str
) -> None:
    attachment = ChannelAttachment(reference, "image/jpeg", "picture.jpg", 1)
    with pytest.raises(ChannelError):
        await bot.fetch_attachment(attachment)
    assert server.calls("getFile") == []


async def test_fetch_attachment_rejects_declared_oversize(bot: TelegramBot, server: TelegramServer) -> None:
    attachment = ChannelAttachment(REFERENCE, "image/jpeg", "big.jpg", 21 * 1024 * 1024)
    with pytest.raises(ChannelError, match="download limit"):
        await bot.fetch_attachment(attachment)
    assert server.calls("getFile") == []


async def test_fetch_attachment_rejects_server_oversize(bot: TelegramBot, server: TelegramServer) -> None:
    server.responses["getFile"].append(ok({"file_path": "documents/big.bin", "file_size": 21 * 1024 * 1024}))
    attachment = ChannelAttachment(REFERENCE, "application/pdf", "big.pdf", 0)
    with pytest.raises(ChannelError, match="download limit"):
        await bot.fetch_attachment(attachment)
    assert server.downloads == []


async def test_fetch_attachment_sanitizes_get_file_failure(bot: TelegramBot, server: TelegramServer) -> None:
    server.responses["getFile"].append(failure(400))
    attachment = ChannelAttachment(REFERENCE, "image/jpeg", "picture.jpg", 1)
    with pytest.raises(ChannelError) as raised:
        await bot.fetch_attachment(attachment)
    assert TOKEN not in str(raised.value)
    assert "SECRET" not in str(raised.value)


async def test_fetch_attachment_reports_missing_file_path(bot: TelegramBot, server: TelegramServer) -> None:
    server.responses["getFile"].append(ok({"file_size": 1}))
    attachment = ChannelAttachment(REFERENCE, "image/jpeg", "picture.jpg", 1)
    with pytest.raises(ChannelError, match="file path"):
        await bot.fetch_attachment(attachment)


async def test_fetch_attachment_reports_failed_download(bot: TelegramBot, server: TelegramServer) -> None:
    server.responses["getFile"].append(ok({"file_path": "documents/missing.bin", "file_size": 1}))
    attachment = ChannelAttachment(REFERENCE, "application/pdf", "missing.pdf", 1)
    with pytest.raises(ChannelError, match="HTTP 404"):
        await bot.fetch_attachment(attachment)
    assert server.downloads == ["documents/missing.bin"]


async def test_fetch_attachment_rejects_traversal_paths(bot: TelegramBot, server: TelegramServer) -> None:
    server.responses["getFile"].append(ok({"file_path": "../secret", "file_size": 1}))
    attachment = ChannelAttachment(REFERENCE, "image/jpeg", "picture.jpg", 1)
    with pytest.raises(ChannelError, match="invalid file path"):
        await bot.fetch_attachment(attachment)
    assert server.downloads == []


async def test_fetch_attachment_returns_opaque_media_type_when_unspecified(
    bot: TelegramBot, server: TelegramServer
) -> None:
    server.responses["getFile"].append(ok({"file_path": "documents/file.bin", "file_size": len(PAYLOAD)}))
    server.files["documents/file.bin"] = PAYLOAD
    attachment = ChannelAttachment(REFERENCE, "", "", 0)
    data, media_type = await bot.fetch_attachment(attachment)
    assert data == PAYLOAD
    assert media_type == "application/octet-stream"


async def test_fetch_attachment_requires_an_open_channel(server: TelegramServer) -> None:
    channel = TelegramBot(TOKEN, base_url=server.origin, poll_timeout=1)
    with pytest.raises(ChannelError, match="not open"):
        await channel.fetch_attachment(ChannelAttachment(REFERENCE, "image/jpeg", "picture.jpg", 1))
