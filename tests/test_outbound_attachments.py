"""Outbound workspace-file uploads, offline."""

import pytest
from nagents.channels import ChannelAttachment
from nagents.channels import ChannelError
from nagents.channels import ChannelFile
from nagents.channels import ChannelSend

from nagents_channel_telegram_bot import TelegramBot

from .conftest import BOT_ID
from .conftest import TelegramServer

PDF = b"%PDF-1.7\noffline-document"
PNG = b"\x89PNG\r\n\x1a\noffline-photo"


def document(name: str = "report.pdf", data: bytes = PDF) -> ChannelFile:
    return ChannelFile(filename=name, media_type="application/pdf", data=data)


def photo(name: str = "photo.png", data: bytes = PNG) -> ChannelFile:
    return ChannelFile(filename=name, media_type="image/png", data=data)


def uploaded(server: TelegramServer) -> list[tuple[str, str, str, bytes]]:
    return [item for _, items in server.uploads for item in items]


async def test_document_upload_uses_send_document_with_caption(bot: TelegramBot, server: TelegramServer) -> None:
    delivery = await bot.send(ChannelSend(destination="-100", text="see attached", files=(document(),)))
    assert delivery.message_ids == ("900",)
    assert [method for method, _ in server.uploads] == ["sendDocument"]
    assert uploaded(server) == [("document", "report.pdf", "application/pdf", PDF)]
    assert server.calls("sendDocument") == [{"chat_id": "-100", "caption": "see attached"}]
    assert server.calls("sendMessage") == []


async def test_photo_upload_uses_send_photo_without_caption(bot: TelegramBot, server: TelegramServer) -> None:
    delivery = await bot.send(ChannelSend(destination="-100", text="", files=(photo(),)))
    assert delivery.message_ids == ("900",)
    assert [method for method, _ in server.uploads] == ["sendPhoto"]
    assert uploaded(server) == [("photo", "photo.png", "image/png", PNG)]
    assert server.calls("sendPhoto") == [{"chat_id": "-100"}]


async def test_multiple_files_upload_in_order_with_first_caption(bot: TelegramBot, server: TelegramServer) -> None:
    delivery = await bot.send(
        ChannelSend(
            destination="-100",
            text="two docs",
            files=(document("a.pdf"), document("b.pdf")),
        )
    )
    assert delivery.message_ids == ("900", "900")
    assert [method for method, _ in server.uploads] == ["sendDocument", "sendDocument"]
    assert [name for _, name, _, _ in uploaded(server)] == ["a.pdf", "b.pdf"]
    assert server.calls("sendDocument") == [
        {"chat_id": "-100", "caption": "two docs"},
        {"chat_id": "-100"},
    ]


async def test_long_text_with_a_file_sends_the_text_separately(bot: TelegramBot, server: TelegramServer) -> None:
    body = "x" * 2000
    delivery = await bot.send(ChannelSend(destination="-100", text=body, files=(document(),)))
    assert delivery.message_ids == ("900",)
    assert server.calls("sendMessage") == [{"chat_id": "-100", "text": body}]
    assert server.calls("sendDocument") == [{"chat_id": "-100"}]
    assert uploaded(server) == [("document", "report.pdf", "application/pdf", PDF)]


async def test_reply_parameters_are_preserved_on_uploads(bot: TelegramBot, server: TelegramServer) -> None:
    await bot.send(ChannelSend(destination="-100", text="", reply_to="42", files=(document(),)))
    assert server.calls("sendDocument") == [
        {"chat_id": "-100", "reply_parameters": '{"message_id": 42, "allow_sending_without_reply": false}'}
    ]


async def test_oversized_file_is_rejected_before_upload(bot: TelegramBot, server: TelegramServer) -> None:
    big = ChannelFile(filename="big.bin", media_type="application/octet-stream", data=b"x" * (20 * 1024 * 1024 + 1))
    with pytest.raises(ChannelError, match="upload limit"):
        await bot.send(ChannelSend(destination="-100", text="", files=(big,)))
    assert server.uploads == []


async def test_empty_file_is_rejected(bot: TelegramBot, server: TelegramServer) -> None:
    with pytest.raises(ChannelError, match="empty"):
        await bot.send(ChannelSend(destination="-100", text="", files=(document(data=b""),)))
    assert server.uploads == []


async def test_transport_references_remain_unsupported(bot: TelegramBot, server: TelegramServer) -> None:
    send = ChannelSend(destination="-100", text="x", attachments=(ChannelAttachment("telegram:file:1"),))
    with pytest.raises(ChannelError, match="references"):
        await bot.send(send)


async def test_too_many_files_are_rejected(bot: TelegramBot, server: TelegramServer) -> None:
    files = (document("a"), document("b"), document("c"), document("d"))
    with pytest.raises(ChannelError, match="Too many"):
        await bot.send(ChannelSend(destination="-100", text="", files=files))
    assert server.uploads == []


async def test_empty_text_and_no_files_is_rejected(bot: TelegramBot, server: TelegramServer) -> None:
    with pytest.raises(ChannelError, match="needs text or an attachment"):
        await bot.send(ChannelSend(destination="-100", text=""))
    assert server.calls("sendMessage") == [] and server.uploads == []


async def test_text_only_send_still_works(bot: TelegramBot, server: TelegramServer) -> None:
    delivery = await bot.send(ChannelSend(destination="-100", text="plain"))
    assert delivery.message_ids == ("900",)
    assert server.calls("sendMessage") == [{"chat_id": "-100", "text": "plain"}]
    assert server.uploads == []


def test_bot_id_fixture_matches_server() -> None:
    assert BOT_ID == 123456
