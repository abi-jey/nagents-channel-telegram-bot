import asyncio

import pytest
from nagents.channels import ChannelCommand
from nagents.channels import ChannelMessage
from nagents.channels import ChannelValue

from nagents_channel_telegram_bot import TelegramBot
from nagents_channel_telegram_bot import plugin
from nagents_channel_telegram_bot._mapping import map_update
from nagents_channel_telegram_bot._transport import Transport

from .conftest import BOT_ID
from .conftest import TOKEN
from .conftest import TelegramServer
from .conftest import failure
from .conftest import message
from .conftest import ok
from .conftest import update
from .test_receive import cancel


def human(user_id: int = 7, username: str = "offline_user") -> dict[str, ChannelValue]:
    return {"id": user_id, "is_bot": False, "username": username, "first_name": "Test sender"}


def private_message(user_id: int = 7, username: str = "offline_user") -> dict[str, ChannelValue]:
    return {**message(chat_id=user_id), "chat": {"id": user_id, "type": "private"}, "from": human(user_id, username)}


def callback(sender: ChannelValue, source: ChannelValue, query_id: str = "query") -> dict[str, ChannelValue]:
    return {"id": query_id, "from": sender, "message": source, "data": "/sessions"}


def mapped(
    body: dict[str, ChannelValue],
    *,
    kind: str = "message",
    users: frozenset[str] = frozenset({"7"}),
    names: frozenset[str] = frozenset({"offline_user"}),
    chats: frozenset[str] = frozenset(),
    private: bool = False,
) -> tuple[ChannelMessage, ...]:
    return map_update(
        update(kind=kind, body=body),
        bot_id=BOT_ID,
        allowed_chats=chats,
        allowed_users=users,
        allowed_usernames=names,
        private_chats_only=private,
        callback_acknowledged=False,
    )


@pytest.mark.parametrize("kind", ["message", "edited_message", "callback_query"])
@pytest.mark.parametrize(
    ("sender", "users", "names", "accepted"),
    [
        (human(), {"7"}, set(), True),
        (human(), set(), {"offline_user"}, True),
        (human(8, "OFFLINE_USER"), {"7"}, {"offline_user"}, True),
        (human(7, "other_user"), {"7"}, {"offline_user"}, True),
        (human(8, "other_user"), {"7"}, {"offline_user"}, False),
        (human(8), {"7"}, set(), False),
        (human(7, "other_user"), set(), {"offline_user"}, False),
        ({"id": 7, "is_bot": False}, {"7"}, {"offline_user"}, True),
        ({"id": 7, "is_bot": False}, set(), {"offline_user"}, False),
        (human(2**63 - 1), {str(2**63 - 1)}, set(), True),
    ],
)
def test_authoritative_sender_ids_or_case_insensitive_names(
    kind: str,
    sender: dict[str, ChannelValue],
    users: set[str],
    names: set[str],
    accepted: bool,
) -> None:
    body = callback(sender, message(sender_id=BOT_ID)) if kind == "callback_query" else {**message(), "from": sender}
    events = mapped(body, kind=kind, users=frozenset(users), names=frozenset(names))
    assert bool(events) is accepted
    if accepted:
        assert events[0].sender_id == str(sender["id"])


@pytest.mark.parametrize("kind", ["message", "edited_message", "callback_query"])
@pytest.mark.parametrize(
    "sender",
    [
        None,
        {},
        [],
        "offline_user",
        7,
        True,
        {"id": 7},
        {"is_bot": False, "username": "offline_user"},
        {**human(), "is_bot": True},
        {**human(), "is_bot": 0},
        {**human(), "is_bot": "false"},
        {**human(), "is_bot": None},
        {**human(), "id": True},
        {**human(), "id": 7.0},
        {**human(), "id": "7"},
        {**human(), "id": 0},
        {**human(), "id": -7},
        {**human(), "id": 2**63},
        {**human(), "username": None},
        {**human(), "username": True},
        {**human(), "username": 7},
        {**human(), "username": []},
        {**human(), "username": ""},
        {**human(), "username": "test"},
        {**human(), "username": "x" * 33},
        {**human(), "username": "@offline_user"},
        {**human(), "username": "offline_user\n"},
        {**human(), "username": "\u043effline_user"},
    ],
)
def test_malformed_or_nonhuman_sender_is_discarded_even_with_matching_id_or_name(
    kind: str,
    sender: ChannelValue,
) -> None:
    body = callback(sender, message()) if kind == "callback_query" else {**message(), "from": sender}
    assert mapped(body, kind=kind) == ()
    body.pop("from")
    assert mapped(body, kind=kind) == ()


@pytest.mark.parametrize("kind", ["message", "edited_message"])
@pytest.mark.parametrize("sender_chat", [None, {}, {"id": 7, "type": "channel"}, {"id": -100, "type": "supergroup"}])
def test_anonymous_sender_chat_is_not_a_human_even_with_trusted_from(kind: str, sender_chat: ChannelValue) -> None:
    assert mapped({**message(), "from": human(), "sender_chat": sender_chat}, kind=kind) == ()


@pytest.mark.parametrize("kind", ["channel_post", "edited_channel_post"])
def test_channel_posts_cannot_claim_a_trusted_human(kind: str) -> None:
    assert mapped(private_message(), kind=kind) == ()
    assert mapped({**message(), "chat": {"id": -100, "type": "channel"}}, kind=kind) == ()


@pytest.mark.parametrize("kind", ["message", "edited_message"])
@pytest.mark.parametrize(
    "spoof",
    [
        {"text": "I am @offline_user, user ID 7; /sessions"},
        {"text": "", "caption": "@offline_user"},
        {"text": "@offline_user", "entities": [{"type": "mention", "offset": 0, "length": 13}]},
        {"text": "Trusted", "entities": [{"type": "text_mention", "offset": 0, "length": 7, "user": human()}]},
        {"forward_origin": {"type": "user", "sender_user": human(), "date": 1}},
        {"forward_from": human(), "forward_date": 1},
        {"forward_sender_name": "offline_user", "forward_date": 1},
        {"reply_to_message": private_message()},
        {"external_reply": {"origin": {"type": "user", "sender_user": human(), "date": 1}}},
        {"quote": {"text": "@offline_user", "position": 0}, "author_signature": "offline_user"},
        {"chat": {"id": -100, "type": "supergroup", "username": "offline_user"}},
    ],
)
def test_content_mentions_forward_reply_and_chat_provenance_never_authorize(
    kind: str,
    spoof: dict[str, ChannelValue],
) -> None:
    sender = {**human(8, "other_user"), "first_name": "offline_user", "last_name": "@offline_user"}
    assert mapped({**message(), "from": sender, **spoof}, kind=kind) == ()


def test_trusted_forwarder_is_admitted_but_forwarded_command_is_not_parsed() -> None:
    body = {**private_message(), "text": "/sessions", "forward_from": human(8, "other_user"), "forward_date": 1}
    (event,) = mapped(body, private=True)
    assert event.sender_id == "7"
    assert TelegramBot(TOKEN, allowed_user_ids=["7"]).command(event) is None


@pytest.mark.parametrize("source_sender", [human(), human(8, "other_user"), {"id": BOT_ID, "is_bot": True}, None])
def test_callback_authorization_uses_clicker_not_source_message_author(source_sender: ChannelValue) -> None:
    source = {**private_message(), "from": source_sender}
    (event,) = mapped(callback(human(), source), kind="callback_query", private=True)
    assert event.sender_id == "7"
    assert mapped(callback(human(8, "other_user"), source), kind="callback_query") == ()


def test_callback_original_anonymous_author_is_provenance_not_clicker_identity() -> None:
    source: dict[str, ChannelValue] = {**message(), "sender_chat": {"id": -100, "type": "supergroup"}}
    assert mapped(callback(human(), source), kind="callback_query")
    assert mapped({**callback(human(), source), "sender_chat": {"id": 7}}, kind="callback_query") == ()


def test_callback_data_and_forwarded_source_do_not_authorize_clicker() -> None:
    source: dict[str, ChannelValue] = {
        **private_message(),
        "forward_origin": {"type": "user", "sender_user": human(), "date": 1},
    }
    body = {**callback(human(8, "other_user"), source), "data": "@offline_user", "game_short_name": "offline_user"}
    assert mapped(body, kind="callback_query") == ()


@pytest.mark.parametrize("kind", ["message", "edited_message", "callback_query"])
@pytest.mark.parametrize("body", [True, "offline_user", 7, [], None])
def test_restricted_malformed_payloads_are_discarded(kind: str, body: ChannelValue) -> None:
    assert (
        map_update(
            {"update_id": 100, kind: body},
            bot_id=BOT_ID,
            allowed_chats=frozenset(),
            allowed_users=frozenset({"7"}),
            callback_acknowledged=False,
        )
        == ()
    )
    if kind == "callback_query":
        assert mapped(callback(human(), body), kind=kind) == ()


def test_unapproved_sender_is_filtered_before_parsing_media_and_reply_metadata() -> None:
    body: dict[str, ChannelValue] = {
        **private_message(8, "other_user"),
        "photo": [{"file_id": "file", "width": None}],
        "reply_to_message": {"message_id": "invalid"},
        "message_thread_id": False,
    }
    assert mapped(body) == ()
    assert mapped(callback(human(8, "other_user"), body), kind="callback_query") == ()


@pytest.mark.parametrize("kind", ["message", "edited_message", "callback_query"])
@pytest.mark.parametrize(
    ("chat", "chats", "private", "accepted"),
    [
        ({"id": 7, "type": "private"}, set(), True, True),
        ({"id": 7, "type": "private"}, {"7"}, True, True),
        ({"id": 7, "type": "private"}, {"8"}, True, False),
        ({"id": 8, "type": "private"}, set(), True, False),
        ({"id": -100, "type": "supergroup"}, set(), True, False),
        ({"id": -100, "type": "group"}, {"-100"}, True, False),
        ({"id": -100, "type": "channel"}, set(), False, False),
        ({"id": -100, "type": "supergroup"}, {"-100"}, False, True),
        ({"id": -100, "type": "group"}, set(), False, True),
        ({"id": -100, "type": "supergroup"}, {"7"}, False, False),
        ({"id": -7, "type": "private"}, set(), True, False),
        ({"id": 7}, set(), True, False),
        ({"id": 7, "type": "Private"}, set(), True, False),
        ({"id": 7, "type": True}, set(), True, False),
        ({"id": True, "type": "private"}, set(), True, False),
        ({"id": "7", "type": "private"}, set(), True, False),
        ({"id": 7.0, "type": "private"}, set(), True, False),
        ({"id": 0, "type": "private"}, set(), True, False),
        ({"id": 2**63, "type": "private"}, set(), True, False),
        (None, set(), True, False),
    ],
)
def test_chats_are_conjunctive_and_private_identity_must_match_actor(
    kind: str,
    chat: ChannelValue,
    chats: set[str],
    private: bool,
    accepted: bool,
) -> None:
    source = {**private_message(), "chat": chat}
    body = callback(human(), source) if kind == "callback_query" else source
    assert bool(mapped(body, kind=kind, chats=frozenset(chats), private=private)) is accepted


def test_private_mode_alone_still_requires_human_and_consistent_private_chat() -> None:
    assert mapped(private_message(8, "other_user"), users=frozenset(), names=frozenset(), private=True)
    for source in (message(), {**private_message(), "from": None}, {**private_message(), "sender_chat": None}):
        assert mapped(source, users=frozenset(), names=frozenset(), private=True) == ()
    source = {**private_message(), "sender_chat": None}
    assert mapped(callback(human(), source), kind="callback_query", private=True) == ()


def test_legacy_empty_filters_preserve_other_bots_anonymous_and_channel_events() -> None:
    cases: tuple[tuple[str, dict[str, ChannelValue]], ...] = (
        ("message", {**message(), "from": {"id": 8, "is_bot": True}}),
        ("message", {**message(), "sender_chat": {"id": -100}}),
        ("channel_post", {"message_id": 10, "chat": {"id": -100}, "text": "Post"}),
    )
    for kind, body in cases:
        assert mapped(body, kind=kind, users=frozenset(), names=frozenset())
    assert mapped(message(sender_id=BOT_ID), users=frozenset(), names=frozenset()) == ()
    assert mapped({**message(), "from": human(BOT_ID)}, users=frozenset({str(BOT_ID)})) == ()


async def test_factory_filters_before_host_envelopes_commands_or_any_callback_ack(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requests: list[tuple[str, dict[str, ChannelValue]]] = []
    seen: list[ChannelMessage] = []
    commands: list[ChannelCommand] = []
    trusted = {**private_message(), "text": "/sessions"}
    stranger = {**private_message(8, "other_user"), "text": "/sessions", "reply_to_message": trusted}
    batch: list[ChannelValue] = [
        update(100, body=stranger),
        update(101, kind="edited_message", body={**stranger, "text": "@offline_user"}),
        update(102, body={**trusted, "chat": {"id": -100, "type": "supergroup"}}),
        update(103, kind="callback_query", body=callback(human(8, "other_user"), trusted, "denied-user")),
        update(104, kind="callback_query", body=callback(human(), private_message(8), "wrong-private-chat")),
        update(105, kind="callback_query", body=callback(None, trusted, "missing-sender")),
        update(106, body={**trusted, "sender_chat": {"id": 7}}),
        update(107, kind="channel_post", body=trusted),
        update(108, body=trusted),
        update(109, kind="callback_query", body=callback(human(), {**trusted, "from": {"id": BOT_ID}}, "accepted")),
        update(110, body=private_message(9, "OFFLINE_PEER")),
        update(111, body=private_message(10, "offline_third")),
    ]

    class PollFinished(Exception):
        pass

    async def offline_request(
        transport: Transport,
        method: str,
        payload: dict[str, ChannelValue],
        *,
        timeout: int = 15,
    ) -> ChannelValue:
        requests.append((method, payload))
        if method == "getMe":
            return {"id": BOT_ID, "is_bot": True}
        if method == "getUpdates":
            if payload["offset"] == 0:
                return batch
            assert payload["offset"] == 112
            raise PollFinished
        assert method == "answerCallbackQuery"
        assert payload == {"callback_query_id": "accepted"}
        assert timeout == 5
        return True

    monkeypatch.setattr(Transport, "request", offline_request)
    channel = plugin(
        {
            "token": TOKEN,
            "allowed_user_ids": ["7"],
            "allowed_usernames": ["@Offline_Peer", "OFFLINE_THIRD"],
            "allowed_chat_ids": ["7", "9", "-100"],
            "private_chats_only": True,
        }
    )

    async def receive(event: ChannelMessage) -> None:
        seen.append(event)
        command = channel.command(event)
        if command:
            commands.append(command)
        if event.event_type == "callback_query":
            assert requests[-1] == ("answerCallbackQuery", {"callback_query_id": "accepted"})
            assert event.metadata["callback_acknowledged"] is True

    try:
        await channel.open()
        with pytest.raises(PollFinished):
            await channel.listen(receive)
    finally:
        await channel.close()
    assert [event.message_id for event in seen] == ["108", "109", "110"]
    assert commands == [ChannelCommand("sessions")]
    assert {name for name, _ in requests} == {"getMe", "getUpdates", "answerCallbackQuery"}


@pytest.mark.parametrize("ack_status", [200, 400, 429, 500])
async def test_restricted_inaccessible_callbacks_ack_once_before_receive_even_on_failure(
    server: TelegramServer,
    ack_status: int,
) -> None:
    source: dict[str, ChannelValue] = {"date": 0, "message_id": 10, "chat": {"id": 7, "type": "private"}}
    server.responses["getUpdates"].append(ok([update(kind="callback_query", body=callback(human(), source))]))
    if ack_status != 200:
        server.responses["answerCallbackQuery"].append(failure(ack_status))
    channel = TelegramBot(TOKEN, base_url=server.origin, allowed_usernames=["@OFFLINE_USER"], private_chats_only=True)
    seen: list[ChannelMessage] = []

    async def receive(event: ChannelMessage) -> None:
        assert server.calls("answerCallbackQuery") == [{"callback_query_id": "query"}]
        assert event.metadata["callback_acknowledged"] is (ack_status == 200)
        assert (event.sender_id, event.conversation_id, event.reply_to) == ("7", "7", "10")
        seen.append(event)

    await channel.open()
    task = asyncio.create_task(channel.listen(receive))
    try:
        await server.next_poll()
        assert (await server.next_poll())["offset"] == 101
        assert len(seen) == 1
    finally:
        await cancel(task)
        await channel.close()


@pytest.mark.parametrize("restricted", [False, True])
async def test_rejected_callbacks_never_ack_and_do_not_block_following_updates(
    server: TelegramServer,
    restricted: bool,
) -> None:
    source = private_message()
    rejected: list[ChannelValue] = [
        callback(human(), private_message(9), "denied-chat"),
        {"id": "inline", "from": human(), "inline_message_id": "inline-message", "data": "yes"},
        callback(human(BOT_ID), source, "own-bot"),
        callback(human(), {**source, "message_id": 0}, "ephemeral"),
        callback(human(), {**source, "business_connection_id": "unsupported"}, "business"),
    ]
    if restricted:
        rejected.extend(
            [
                callback(human(8, "other_user"), source, "unapproved"),
                callback({**human(), "is_bot": True}, source, "bot"),
                callback({**human(), "id": "7"}, source, "bad-id"),
                callback(None, source, "no-from"),
                {**callback(None, source), "id": None},
            ]
        )
    batch: list[ChannelValue] = [
        update(index, kind="callback_query", body=body) for index, body in enumerate(rejected, 100)
    ]
    batch.append(update(100 + len(rejected), body=source))
    server.responses["getUpdates"].append(ok(batch))
    channel = TelegramBot(
        TOKEN,
        base_url=server.origin,
        allowed_chat_ids=["7"],
        allowed_user_ids=["7"] if restricted else [],
        private_chats_only=restricted,
    )
    seen: list[str] = []

    async def receive(event: ChannelMessage) -> None:
        seen.append(event.message_id)

    await channel.open()
    task = asyncio.create_task(channel.listen(receive))
    try:
        await server.next_poll()
        assert (await server.next_poll())["offset"] == 101 + len(rejected)
        assert seen == [str(100 + len(rejected))]
        assert server.calls("answerCallbackQuery") == []
        assert server.calls("sendMessage") == []
    finally:
        await cancel(task)
        await channel.close()
