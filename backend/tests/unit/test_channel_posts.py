"""Чтение и правка постов канала на стороне воркера (app/telegram/channel_posts.py).

Реальный Telegram в тестах запрещён (ТЗ §37). Большая часть тестов идёт на
управляемой подделке клиента, которая повторяет семантику Telethon там, где на
неё опирается код (порядок iter_messages «от нового к старому», offset_id как
верхняя граница, None для отсутствующих id в get_messages). Поведение, которое
подделка доказать не может (кеш сущностей, сон на FloodWait внутри Telethon),
проверяется на настоящем TelegramClient с подменённым MTProto-отправителем.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import pytest
from structlog.testing import capture_logs
from telethon import TelegramClient, errors, functions, types
from telethon.client import users as telethon_users
from telethon.sessions import StringSession
from telethon.tl.types import contacts as tl_contacts
from telethon.tl.types import messages as tl_messages

from app.bus.messages import Command, CommandType
from app.core.errors import InvalidInputError, TelegramError, TelegramFloodWaitError
from app.telegram.channel_posts import (
    CAPTION_LIMIT,
    FLOOD_RETRY_MAX_SECONDS,
    POST_HTML,
    READ_LIMIT_MAX,
    TEXT_LIMIT,
    ChannelPostError,
    EditRequest,
    ReadRequest,
    edit_post,
    entity_signature,
    html_is_lossless,
    media_type,
    normalize_chat_reference,
    parse_edit_payload,
    parse_html,
    parse_read_payload,
    read_posts,
    rendered_length,
    unparse_html,
)
from app.workers.command_handler import CommandHandler
from tests.conftest import make_settings

DATE = datetime(2026, 9, 1, 12, 30, tzinfo=UTC)
CHANNEL_ID = 1234567890
MARKED_ID = -1001234567890


# --- подделки ---------------------------------------------------------------
@dataclass
class Msg:
    id: int
    message: str = ""
    date: datetime | None = DATE
    entities: list[Any] | None = None
    media: Any = None
    action: Any = None
    grouped_id: int | None = None
    views: int | None = None
    out: bool = False
    edit_date: datetime | None = None
    sticker: Any = None
    video_note: Any = None
    gif: Any = None
    voice: Any = None
    audio: Any = None
    video: Any = None


@dataclass
class Channel:
    id: int = CHANNEL_ID
    title: str = "Шаблоны"
    username: str | None = "templates"
    broadcast: bool = True
    megagroup: bool = False
    creator: bool = True
    admin_rights: Any = None
    min: bool = False


@dataclass
class ChannelClient:
    entity: Any = field(default_factory=Channel)
    # Как у Telethon: история от нового к старому.
    history: list[Any] = field(default_factory=list)
    edit_errors: list[BaseException] = field(default_factory=list)
    get_messages_errors: list[BaseException] = field(default_factory=list)
    # Ошибка поиска чата: на любую ссылку, на конкретную, разовая (очередь).
    resolve_error: BaseException | None = None
    resolve_error_by_ref: dict[Any, BaseException] = field(default_factory=dict)
    resolve_errors: list[BaseException] = field(default_factory=list)
    broken_offsets: dict[int, BaseException] = field(default_factory=dict)
    dialogs: list[Any] = field(default_factory=list)
    flood_sleep_threshold: int = 60

    calls: list[tuple[Any, ...]] = field(default_factory=list)
    # Порог сна на FloodWait, который видел каждый запрос.
    thresholds: list[int] = field(default_factory=list)

    def _record(self, *call: Any) -> None:
        self.calls.append(call)
        self.thresholds.append(self.flood_sleep_threshold)

    async def get_input_entity(self, reference: Any) -> Any:
        self._record("get_input_entity", reference)
        if self.resolve_errors:
            raise self.resolve_errors.pop(0)
        error = self.resolve_error_by_ref.get(reference, self.resolve_error)
        if error is not None:
            raise error
        return ("peer", reference)

    async def get_entity(self, peer: Any) -> Any:
        self._record("get_entity", peer)
        return self.entity

    async def get_dialogs(self, limit: int | None = None) -> list[Any]:
        self._record("get_dialogs", limit)
        return self.dialogs

    def iter_messages(self, entity: Any, *, limit: int | None = None, offset_id: int = 0) -> Any:
        self._record("iter_messages", limit, offset_id)
        items = [m for m in self.history if not offset_id or m.id < offset_id]
        items = items[:limit] if limit else items
        broken = self.broken_offsets.get(offset_id)
        return self._iter(items, broken)

    @staticmethod
    async def _iter(items: list[Any], broken: BaseException | None) -> Any:
        if broken is not None:
            raise broken
        for item in items:
            yield item

    async def get_messages(self, entity: Any, *, ids: Any) -> Any:
        self._record("get_messages", ids)
        if self.get_messages_errors:
            raise self.get_messages_errors.pop(0)
        by_id = {m.id: m for m in self.history}
        if isinstance(ids, int):
            return by_id.get(ids)
        return [by_id.get(message_id) for message_id in ids]

    async def edit_message(
        self,
        entity: Any,
        message: int,
        text: str,
        *,
        parse_mode: Any = (),
        link_preview: bool = True,
    ) -> Any:
        self._record("edit_message", message, text, parse_mode, link_preview)
        if self.edit_errors:
            raise self.edit_errors.pop(0)
        return Msg(id=message, message=text, edit_date=DATE)

    def count(self, name: str) -> int:
        return sum(1 for call in self.calls if call[0] == name)


class RecordingSleep:
    def __init__(self) -> None:
        self.calls: list[float] = []

    async def __call__(self, seconds: float) -> None:
        self.calls.append(seconds)


def photo() -> Any:
    return types.MessageMediaPhoto()


def webpage() -> Any:
    return types.MessageMediaWebPage(webpage=types.WebPageEmpty(id=1))


def flood(seconds: int) -> errors.FloodWaitError:
    return errors.FloodWaitError(request=None, capture=seconds)


# --- ссылка на чат ------------------------------------------------------------
class TestNormalizeChatReference:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("@templates", "@templates"),
            ("templates_ch", "@templates_ch"),
            ("https://t.me/templates", "@templates"),
            ("t.me/templates/123", "@templates"),
            ("https://t.me/s/templates", "@templates"),
            ("https://t.me/c/1234567890/55", MARKED_ID),
            ("https://t.me/c/777/5", -1000000000777),
            ("https://t.me/+AbCdEf12345", "https://t.me/+AbCdEf12345"),
            ("t.me/joinchat/AbCdEf123", "https://t.me/+AbCdEf123"),
            ("+AbCdEf12345", "https://t.me/+AbCdEf12345"),
            ("1AbC-dEf_ghij", "https://t.me/+1AbC-dEf_ghij"),
            ("-1001234567890", MARKED_ID),
            ("1234567890", 1234567890),
            (" 42 ", 42),
            (777, 777),
            ("+79991234567", "+79991234567"),
        ],
    )
    def test_accepted_forms(self, raw: Any, expected: Any) -> None:
        assert normalize_chat_reference(raw) == expected

    @pytest.mark.parametrize(
        "raw", [None, "", "   ", True, "t.me/+", "joinchat/", "--123", "-", "-12a"]
    )
    def test_rejected(self, raw: Any) -> None:
        with pytest.raises(InvalidInputError):
            normalize_chat_reference(raw)


# --- payload ------------------------------------------------------------------
class TestParseReadPayload:
    def test_defaults(self) -> None:
        request = parse_read_payload({"chat": "@templates"})
        assert request == ReadRequest(chat="@templates", limit=100, offset_id=0, ids=None)

    def test_max_limit_is_accepted(self) -> None:
        assert parse_read_payload({"chat": "@t_chan", "limit": READ_LIMIT_MAX}).limit == 500

    @pytest.mark.parametrize("chat", [CHANNEL_ID, str(CHANNEL_ID)])
    def test_bare_positive_id_means_channel(self, chat: Any) -> None:
        # Положительный id по соглашению Telegram — пользователь; для команд
        # постов это id канала, и в кеш уходит помеченный -100….
        assert parse_read_payload({"chat": chat}).chat == MARKED_ID

    def test_marked_id_is_kept(self) -> None:
        assert parse_read_payload({"chat": MARKED_ID}).chat == MARKED_ID

    @pytest.mark.parametrize(
        "payload",
        [
            {"chat": "@templates", "limit": 0},
            {"chat": "@templates", "limit": READ_LIMIT_MAX + 1},
            {"chat": "@templates", "limit": True},
            {"chat": "@templates", "limit": "10"},
            {"chat": "@templates", "offset_id": -1},
            {"limit": 10},
            {"chat": "--5"},
            {"chat": "@templates", "ids": []},
            {"chat": "@templates", "ids": [1, 0]},
            {"chat": "@templates", "ids": "1,2"},
            {"chat": "@templates", "ids": list(range(1, READ_LIMIT_MAX + 2))},
        ],
    )
    def test_invalid(self, payload: dict[str, Any]) -> None:
        with pytest.raises(InvalidInputError):
            parse_read_payload(payload)

    def test_ids_are_deduplicated_keeping_order(self) -> None:
        request = parse_read_payload({"chat": "@templates", "ids": [5, 3, 5, 1]})
        assert request.ids == (5, 3, 1)


class TestParseEditPayload:
    def test_defaults(self) -> None:
        request = parse_edit_payload({"chat": "@templates", "message_id": 7, "text": "новый"})
        assert request == EditRequest(
            chat="@templates",
            message_id=7,
            text="новый",
            parse_mode=None,
            link_preview=False,
            expires_at=None,
        )

    @pytest.mark.parametrize("parse_mode", ["html", "md", None])
    def test_parse_modes(self, parse_mode: str | None) -> None:
        request = parse_edit_payload(
            {"chat": "@templates", "message_id": 7, "text": "x", "parse_mode": parse_mode}
        )
        assert request.parse_mode == parse_mode

    def test_expires_at(self) -> None:
        request = parse_edit_payload(
            {"chat": "@templates", "message_id": 7, "text": "x", "expires_at": 1790000000}
        )
        assert request.expires_at == 1790000000.0

    @pytest.mark.parametrize(
        "payload",
        [
            {"chat": "@templates", "message_id": 7, "text": "x", "parse_mode": "markdown"},
            {"chat": "@templates", "message_id": 0, "text": "x"},
            {"chat": "@templates", "message_id": "7", "text": "x"},
            {"chat": "@templates", "message_id": True, "text": "x"},
            {"chat": "@templates", "message_id": 7},
            {"chat": "@templates", "message_id": 7, "text": 5},
            {"chat": "@templates", "message_id": 7, "text": "x", "link_preview": "yes"},
            {"chat": "@templates", "message_id": 7, "text": "x", "expires_at": "soon"},
            {"chat": "@templates", "message_id": 7, "text": "x", "expires_at": True},
            {"chat": "@templates", "message_id": 7, "text": "x", "expires_at": -1},
            {"message_id": 7, "text": "x"},
        ],
    )
    def test_invalid(self, payload: dict[str, Any]) -> None:
        with pytest.raises(InvalidInputError):
            parse_edit_payload(payload)


# --- длина и медиа ------------------------------------------------------------
class TestRenderedLength:
    def test_html_tags_are_not_counted(self) -> None:
        assert rendered_length("<b>привет</b>", "html") == 6

    def test_spoiler_tag_is_not_counted(self) -> None:
        assert rendered_length("<tg-spoiler>тайна</tg-spoiler>", "html") == 5

    def test_markdown_markers_are_not_counted(self) -> None:
        assert rendered_length("**жирный**", "md") == 6

    def test_plain_text_counts_markup_literally(self) -> None:
        assert rendered_length("<b>x</b>", None) == 8

    def test_emoji_counts_as_two_utf16_units(self) -> None:
        assert rendered_length("😀", None) == 2


class TestMediaType:
    def test_no_media(self) -> None:
        assert media_type(Msg(id=1)) is None

    def test_photo(self) -> None:
        assert media_type(Msg(id=1, media=photo())) == "photo"

    def test_link_preview_is_webpage(self) -> None:
        assert media_type(Msg(id=1, media=webpage())) == "webpage"

    @pytest.mark.parametrize(
        ("flag", "expected"),
        [("gif", "animation"), ("video", "video"), ("voice", "voice"), ("sticker", "sticker")],
    )
    def test_document_kinds(self, flag: str, expected: str) -> None:
        message = Msg(id=1, media=types.MessageMediaDocument(), **{flag: object()})
        assert media_type(message) == expected

    def test_plain_document(self) -> None:
        assert media_type(Msg(id=1, media=types.MessageMediaDocument())) == "document"

    def test_unknown_media_class_gets_readable_name(self) -> None:
        class MessageMediaToDoList:
            pass

        assert media_type(Msg(id=1, media=MessageMediaToDoList())) == "to_do_list"


# --- HTML постов --------------------------------------------------------------
class TestPostHtml:
    TEXT = "Акция 😀 тайна и print(1) <b> & ok\nподробнее"

    def entities(self) -> list[Any]:
        return [
            types.MessageEntityBold(offset=0, length=5),
            types.MessageEntityItalic(offset=0, length=14),
            types.MessageEntityCustomEmoji(offset=6, length=2, document_id=555),
            types.MessageEntitySpoiler(offset=9, length=5),
            types.MessageEntityPre(offset=17, length=8, language="py"),
            types.MessageEntityBlockquote(offset=35, length=9, collapsed=True),
            types.MessageEntityTextUrl(offset=35, length=9, url='https://x.com/?a=1&b="2"'),
        ]

    def test_round_trip_keeps_spoiler_and_code_block(self) -> None:
        html = unparse_html(self.TEXT, self.entities())

        assert "<tg-spoiler>тайна</tg-spoiler>" in html
        # Без Telethon-овских переносов, отступов и «{}» внутри блока кода.
        assert '<pre><code class="language-py">print(1)</code></pre>' in html
        assert "&lt;b&gt; &amp; ok" in html
        plain, entities = parse_html(html)
        assert plain == self.TEXT
        assert entity_signature(entities) == entity_signature(self.entities())
        assert html_is_lossless(self.TEXT, self.entities(), html)

    def test_nested_entities_open_outer_first(self) -> None:
        html = unparse_html(
            "жирный курсив",
            [
                types.MessageEntityItalic(offset=7, length=6),
                types.MessageEntityBold(offset=0, length=13),
            ],
        )
        assert html == "<b>жирный <i>курсив</i></b>"

    def test_auto_entities_stay_plain_text(self) -> None:
        text = "https://x.com @chan"
        html = unparse_html(
            text,
            [
                types.MessageEntityUrl(offset=0, length=13),
                types.MessageEntityMention(offset=14, length=5),
            ],
        )
        assert html == text

    def test_span_spoiler_is_parsed(self) -> None:
        plain, entities = parse_html('<span class="tg-spoiler">x</span> y')
        assert plain == "x y"
        assert entity_signature(entities) == [("MessageEntitySpoiler", 0, 1, "")]

    def test_trailing_ampersand_is_not_lost(self) -> None:
        assert parse_html("<b>A</b>&B")[0] == "A&B"

    def test_unknown_entity_type_is_reported_as_lossy(self) -> None:
        entities = [types.MessageEntityFormattedDate(offset=0, length=4, date=DATE)]
        # Сущность, для которой нет тега, в HTML не попадает.
        assert not html_is_lossless("дата", entities, unparse_html("дата", entities))


# --- чтение -------------------------------------------------------------------
class TestReadPosts:
    async def test_posts_come_oldest_first_without_service_messages(self) -> None:
        bold = types.MessageEntityBold(offset=0, length=6)
        client = ChannelClient(
            history=[
                Msg(id=12, message="Третий", media=photo(), grouped_id=99, views=40),
                Msg(id=11, action=object()),
                Msg(id=10, message="Первый", entities=[bold], views=10),
            ]
        )

        result = await read_posts(client, ReadRequest(chat="@templates"))

        assert [post["id"] for post in result["posts"]] == [10, 12]
        assert result["skipped_service"] == 1
        assert result["fetched"] == 3
        assert result["next_offset_id"] is None  # история кончилась
        assert result["chat"] == {
            "id": MARKED_ID,
            "title": "Шаблоны",
            "username": "templates",
            "type": "channel",
        }
        first, second = result["posts"]
        assert first["text"] == "Первый"
        assert first["date"] == DATE.isoformat()
        assert first["entities_present"] is True
        assert first["formatting_present"] is True
        assert first["html"] == "<b>Первый</b>"
        assert first["html_lossless"] is True
        assert first["has_media"] is False
        assert first["length"] == 6
        assert second["media_type"] == "photo"
        assert second["has_media"] is True
        assert second["grouped_id"] == 99
        assert second["views"] == 40
        assert second["entities_present"] is False
        assert second["html"] is None
        assert second["html_lossless"] is None
        assert first["can_edit"] is True  # создатель канала

    async def test_auto_entities_are_not_author_formatting(self) -> None:
        url = types.MessageEntityUrl(offset=0, length=13)
        client = ChannelClient(history=[Msg(id=1, message="https://x.com", entities=[url])])

        post = (await read_posts(client, ReadRequest(chat="@templates")))["posts"][0]

        assert post["entities_present"] is True
        assert post["formatting_present"] is False
        assert post["html"] is None

    async def test_history_is_read_in_batches_of_100(self) -> None:
        client = ChannelClient(history=[Msg(id=i, message=f"p{i}") for i in range(250, 0, -1)])

        result = await read_posts(client, ReadRequest(chat="@templates", limit=300))

        batches = [call[1:] for call in client.calls if call[0] == "iter_messages"]
        assert batches == [(100, 0), (100, 151), (100, 51)]
        assert [post["id"] for post in result["posts"]] == list(range(1, 251))
        assert result["next_offset_id"] is None

    async def test_next_offset_points_deeper_when_limit_reached(self) -> None:
        client = ChannelClient(history=[Msg(id=i, message="x") for i in range(30, 0, -1)])

        result = await read_posts(client, ReadRequest(chat="@templates", limit=10, offset_id=25))

        assert [post["id"] for post in result["posts"]] == list(range(15, 25))
        assert result["next_offset_id"] == 15

    async def test_broken_batch_returns_what_was_read(self) -> None:
        client = ChannelClient(
            history=[Msg(id=i, message="x") for i in range(250, 0, -1)],
            broken_offsets={151: RuntimeError("unknown TL constructor")},
        )

        result = await read_posts(client, ReadRequest(chat="@templates", limit=300))

        assert result["partial"] is True
        assert result["error"]["offset_id"] == 151
        assert "RuntimeError" in result["error"]["detail"]
        assert len(result["posts"]) == 100
        assert result["next_offset_id"] is None

    async def test_long_flood_on_later_batch_is_partial_with_seconds(self) -> None:
        client = ChannelClient(
            history=[Msg(id=i, message="x") for i in range(250, 0, -1)],
            broken_offsets={151: flood(300)},
        )

        result = await read_posts(
            client, ReadRequest(chat="@templates", limit=300), sleep=RecordingSleep()
        )

        assert result["partial"] is True
        assert result["error"]["code"] == "telegram_flood_wait"
        assert result["error"]["seconds"] == 300
        assert len(result["posts"]) == 100

    async def test_short_flood_on_batch_is_waited_once(self) -> None:
        history = [Msg(id=i, message="x") for i in range(5, 0, -1)]
        client = ChannelClient(history=history, broken_offsets={0: flood(3)})
        sleep = RecordingSleep()

        async def heal(seconds: float) -> None:
            await sleep(seconds)
            client.broken_offsets.clear()

        result = await read_posts(client, ReadRequest(chat="@templates"), sleep=heal)

        assert sleep.calls == [4]
        assert len(result["posts"]) == 5

    async def test_first_batch_rpc_error_is_mapped(self) -> None:
        client = ChannelClient(
            history=[Msg(id=1)], broken_offsets={0: errors.ChannelPrivateError(request=None)}
        )

        with pytest.raises(ChannelPostError) as caught:
            await read_posts(client, ReadRequest(chat="@templates"))
        assert caught.value.code == "channel_private"

    async def test_first_batch_parse_error_is_telegram_error(self) -> None:
        client = ChannelClient(history=[Msg(id=1)], broken_offsets={0: RuntimeError("bad TL")})

        with pytest.raises(TelegramError):
            await read_posts(client, ReadRequest(chat="@templates"))

    async def test_read_by_ids_reports_missing(self) -> None:
        client = ChannelClient(
            history=[Msg(id=7, message="семь"), Msg(id=5, message="пять"), Msg(id=4, action=1)]
        )

        result = await read_posts(client, ReadRequest(chat="@templates", ids=(7, 6, 5, 4)))

        assert [post["id"] for post in result["posts"]] == [5, 7]
        assert result["missing_ids"] == [6]
        assert result["skipped_service"] == 1
        assert client.count("iter_messages") == 0

    @pytest.mark.parametrize(
        ("entity", "message", "expected"),
        [
            (Channel(creator=False, admin_rights=None), Msg(id=1), False),
            (
                Channel(creator=False, admin_rights=SimpleNamespace(edit_messages=True)),
                Msg(id=1),
                True,
            ),
            (
                Channel(
                    creator=False,
                    admin_rights=SimpleNamespace(edit_messages=False, post_messages=True),
                ),
                Msg(id=1, out=True),
                True,
            ),
            (
                Channel(
                    creator=False,
                    admin_rights=SimpleNamespace(edit_messages=False, post_messages=True),
                ),
                Msg(id=1, out=False),
                False,
            ),
            (Channel(min=True), Msg(id=1), None),
        ],
    )
    async def test_can_edit(self, entity: Any, message: Msg, expected: bool | None) -> None:
        client = ChannelClient(entity=entity, history=[message])

        post = (await read_posts(client, ReadRequest(chat="@templates")))["posts"][0]

        assert post["can_edit"] is expected

    @pytest.mark.parametrize(
        ("entity", "kind"),
        [
            (Channel(broadcast=False, megagroup=True), "supergroup"),
            (SimpleNamespace(id=5, first_name="Иван", username=None), "user"),
        ],
    )
    async def test_only_channels_are_read(self, entity: Any, kind: str) -> None:
        client = ChannelClient(entity=entity, history=[Msg(id=1, message="x")])

        with pytest.raises(ChannelPostError) as caught:
            await read_posts(client, ReadRequest(chat="@somebody"))

        assert caught.value.code == "not_a_channel"
        assert caught.value.details["chat_type"] == kind
        assert client.count("iter_messages") == 0

    async def test_flood_threshold_is_zero_during_the_command_and_restored(self) -> None:
        client = ChannelClient(history=[Msg(id=1, message="x")])

        await read_posts(client, ReadRequest(chat="@templates"))

        assert client.thresholds and set(client.thresholds) == {0}
        assert client.flood_sleep_threshold == 60


class TestResolve:
    async def test_resolves_through_session_cache(self) -> None:
        client = ChannelClient(history=[Msg(id=1, message="x")])

        await read_posts(client, ReadRequest(chat="@templates"))

        # get_input_entity смотрит в кеш сессии; get_entity получает InputPeer,
        # а не строку — иначе Telethon слал бы ResolveUsername на каждый вызов.
        assert ("get_input_entity", "@templates") in client.calls
        assert ("get_entity", ("peer", "@templates")) in client.calls

    async def test_marked_id_matches_channel_dialog_not_private_chat(self) -> None:
        channel = Channel(id=555)
        client = ChannelClient(
            entity=channel,
            resolve_error=ValueError("Could not find the input entity"),
            dialogs=[
                SimpleNamespace(id=555, entity=SimpleNamespace(id=555, first_name="Личка")),
                SimpleNamespace(id=-1000000000555, entity=channel),
            ],
            history=[Msg(id=1, message="x")],
        )

        result = await read_posts(client, ReadRequest(chat=-1000000000555))

        assert result["chat"]["id"] == -1000000000555
        assert client.count("get_dialogs") == 1

    async def test_bare_id_matches_channel_entity_id(self) -> None:
        channel = Channel(id=555)
        client = ChannelClient(
            entity=channel,
            resolve_error=ValueError("nope"),
            dialogs=[
                SimpleNamespace(id=777, entity=SimpleNamespace(id=777, first_name="Личка")),
                SimpleNamespace(id=-1000000000555, entity=channel),
            ],
            history=[Msg(id=1, message="x")],
        )

        result = await read_posts(client, ReadRequest(chat=555))

        assert result["chat"]["type"] == "channel"

    async def test_unknown_numeric_id_is_chat_not_found(self) -> None:
        client = ChannelClient(resolve_error=ValueError("nope"), dialogs=[])

        with pytest.raises(ChannelPostError) as caught:
            await read_posts(client, ReadRequest(chat=-100999))
        assert caught.value.code == "chat_not_found"

    async def test_unknown_username_does_not_scan_dialogs(self) -> None:
        client = ChannelClient(resolve_error=ValueError("No user has 'nobody' as username"))

        with pytest.raises(ChannelPostError) as caught:
            await read_posts(client, ReadRequest(chat="@nobody"))
        assert caught.value.code == "chat_not_found"
        assert client.count("get_dialogs") == 0

    async def test_username_rpc_error_is_mapped(self) -> None:
        client = ChannelClient(resolve_error=errors.UsernameNotOccupiedError(request=None))

        with pytest.raises(ChannelPostError) as caught:
            await read_posts(client, ReadRequest(chat="@nobody"))
        assert caught.value.code == "chat_not_found"

    async def test_bare_invite_hash_is_retried_as_invite_link(self) -> None:
        client = ChannelClient(
            resolve_error_by_ref={"@AbCdEfGhIjKlMnOp": ValueError("No user has that username")},
            history=[Msg(id=1, message="x")],
        )
        request = parse_read_payload({"chat": "AbCdEfGhIjKlMnOp"})

        result = await read_posts(client, request)

        assert result["chat"]["id"] == MARKED_ID
        assert ("get_input_entity", "https://t.me/+AbCdEfGhIjKlMnOp") in client.calls

    async def test_retries_exhausted_is_telegram_unavailable(self) -> None:
        client = ChannelClient(resolve_error=ValueError("Request was unsuccessful 5 time(s)"))

        with pytest.raises(ChannelPostError) as caught:
            await read_posts(client, ReadRequest(chat="@templates"))
        assert caught.value.code == "telegram_unavailable"
        assert client.count("get_dialogs") == 0


# --- правка -------------------------------------------------------------------
def edit_request(message_id: int = 10, text: str = "новый текст", **kwargs: Any) -> EditRequest:
    return EditRequest(chat="@templates", message_id=message_id, text=text, **kwargs)


class TestEditPost:
    async def test_edits_with_plain_text_by_default(self) -> None:
        client = ChannelClient(history=[Msg(id=10, message="старый")])

        result = await edit_post(client, edit_request())

        edits = [call for call in client.calls if call[0] == "edit_message"]
        # parse_mode=None передаётся явно: иначе Telethon включил бы markdown.
        assert edits == [("edit_message", 10, "новый текст", None, False)]
        assert result["edited"] is True
        assert result["no_change"] is False
        assert result["old_length"] == 6
        assert result["new_length"] == 11
        assert result["has_media"] is False
        assert result["edit_date"] == DATE.isoformat()

    async def test_html_uses_the_same_parser_as_length_check(self) -> None:
        client = ChannelClient(history=[Msg(id=10, message="старый")])

        await edit_post(
            client, edit_request(text="<b>жирный</b>", parse_mode="html", link_preview=True)
        )

        assert ("edit_message", 10, "<b>жирный</b>", POST_HTML, True) in client.calls

    async def test_markdown_is_passed_by_name(self) -> None:
        client = ChannelClient(history=[Msg(id=10, message="старый")])

        await edit_post(client, edit_request(text="**x**", parse_mode="md"))

        assert ("edit_message", 10, "**x**", "md", False) in client.calls

    async def test_text_over_4096_is_rejected_before_any_request(self) -> None:
        client = ChannelClient(history=[Msg(id=10, message="старый")])

        with pytest.raises(ChannelPostError) as caught:
            await edit_post(client, edit_request(text="я" * (TEXT_LIMIT + 1)))

        assert caught.value.code == "text_too_long"
        assert caught.value.details["length"] == TEXT_LIMIT + 1
        assert client.calls == []

    async def test_caption_over_1024_is_rejected_before_edit(self) -> None:
        client = ChannelClient(history=[Msg(id=10, message="подпись", media=photo())])

        with pytest.raises(ChannelPostError) as caught:
            await edit_post(client, edit_request(text="я" * (CAPTION_LIMIT + 1)))

        assert caught.value.code == "caption_too_long"
        assert caught.value.details["limit"] == CAPTION_LIMIT
        assert client.count("edit_message") == 0

    async def test_caption_at_limit_is_accepted(self) -> None:
        client = ChannelClient(history=[Msg(id=10, message="подпись", media=photo())])

        result = await edit_post(client, edit_request(text="я" * CAPTION_LIMIT))

        assert result["edited"] is True
        assert result["has_media"] is True

    async def test_caption_limit_counts_rendered_text_not_markup(self) -> None:
        client = ChannelClient(history=[Msg(id=10, message="подпись", media=photo())])
        text = "<b>" + "я" * CAPTION_LIMIT + "</b>"

        result = await edit_post(client, edit_request(text=text, parse_mode="html"))

        assert result["new_length"] == CAPTION_LIMIT

    async def test_link_preview_post_uses_text_limit(self) -> None:
        client = ChannelClient(history=[Msg(id=10, message="см. ссылку", media=webpage())])

        result = await edit_post(client, edit_request(text="я" * 2000))

        assert result["edited"] is True
        assert result["has_media"] is False

    async def test_empty_text_is_rejected_for_text_post(self) -> None:
        client = ChannelClient(history=[Msg(id=10, message="старый")])

        with pytest.raises(ChannelPostError) as caught:
            await edit_post(client, edit_request(text=""))
        assert caught.value.code == "empty_text"

    async def test_empty_caption_is_allowed_for_media(self) -> None:
        client = ChannelClient(history=[Msg(id=10, message="подпись", media=photo())])

        result = await edit_post(client, edit_request(text=""))

        assert result["edited"] is True

    async def test_missing_message(self) -> None:
        client = ChannelClient(history=[])

        with pytest.raises(ChannelPostError) as caught:
            await edit_post(client, edit_request())
        assert caught.value.code == "message_not_found"

    async def test_service_message(self) -> None:
        client = ChannelClient(history=[Msg(id=10, action=object())])

        with pytest.raises(ChannelPostError) as caught:
            await edit_post(client, edit_request())
        assert caught.value.code == "service_message"

    @pytest.mark.parametrize(
        "entity",
        [
            Channel(broadcast=False, megagroup=True),
            SimpleNamespace(id=10, first_name="Иван", username="ivan"),
        ],
    )
    async def test_refuses_to_edit_outside_channels(self, entity: Any) -> None:
        client = ChannelClient(entity=entity, history=[Msg(id=10, message="личное", out=True)])

        with pytest.raises(ChannelPostError) as caught:
            await edit_post(client, edit_request())

        assert caught.value.code == "not_a_channel"
        assert client.count("get_messages") == 0
        assert client.count("edit_message") == 0

    async def test_not_modified_is_success_without_change(self) -> None:
        client = ChannelClient(
            history=[Msg(id=10, message="тот же")],
            edit_errors=[errors.MessageNotModifiedError(request=None)],
        )

        result = await edit_post(client, edit_request(text="тот же"))

        assert result["edited"] is False
        assert result["no_change"] is True

    async def test_short_flood_wait_is_waited_and_retried_once(self) -> None:
        client = ChannelClient(history=[Msg(id=10, message="старый")], edit_errors=[flood(5)])
        sleep = RecordingSleep()

        result = await edit_post(client, edit_request(), sleep=sleep)

        assert sleep.calls == [6]
        assert client.count("edit_message") == 2
        assert result["edited"] is True
        assert result["flood_waited"] == 5

    async def test_flood_wait_at_threshold_is_still_waited(self) -> None:
        client = ChannelClient(
            history=[Msg(id=10, message="старый")], edit_errors=[flood(FLOOD_RETRY_MAX_SECONDS)]
        )
        sleep = RecordingSleep()

        await edit_post(client, edit_request(), sleep=sleep)

        assert sleep.calls == [FLOOD_RETRY_MAX_SECONDS + 1]

    async def test_long_flood_wait_is_returned_with_seconds(self) -> None:
        client = ChannelClient(history=[Msg(id=10, message="старый")], edit_errors=[flood(31)])
        sleep = RecordingSleep()

        with pytest.raises(TelegramFloodWaitError) as caught:
            await edit_post(client, edit_request(), sleep=sleep)

        assert caught.value.seconds == 31
        assert "31" in caught.value.message
        assert sleep.calls == []
        assert client.count("edit_message") == 1

    async def test_second_flood_wait_is_not_retried_again(self) -> None:
        client = ChannelClient(
            history=[Msg(id=10, message="старый")], edit_errors=[flood(3), flood(3)]
        )
        sleep = RecordingSleep()

        with pytest.raises(TelegramFloodWaitError):
            await edit_post(client, edit_request(), sleep=sleep)

        assert sleep.calls == [4]
        assert client.count("edit_message") == 2

    async def test_one_retry_per_command_across_all_requests(self) -> None:
        # Короткий FloodWait на чтении поста уже истратил единственный повтор.
        client = ChannelClient(
            history=[Msg(id=10, message="старый")],
            get_messages_errors=[flood(2)],
            edit_errors=[flood(2)],
        )
        sleep = RecordingSleep()

        with pytest.raises(TelegramFloodWaitError):
            await edit_post(client, edit_request(), sleep=sleep)

        assert sleep.calls == [3]
        assert client.count("edit_message") == 1

    async def test_flood_on_resolve_is_waited_once(self) -> None:
        client = ChannelClient(history=[Msg(id=10, message="старый")], resolve_errors=[flood(4)])
        sleep = RecordingSleep()

        result = await edit_post(client, edit_request(), sleep=sleep)

        assert sleep.calls == [5]
        assert result["edited"] is True

    async def test_flood_threshold_is_zero_during_edit_and_restored(self) -> None:
        client = ChannelClient(history=[Msg(id=10, message="старый")], edit_errors=[flood(45)])

        with pytest.raises(TelegramFloodWaitError):
            await edit_post(client, edit_request(), sleep=RecordingSleep())

        assert set(client.thresholds) == {0}
        assert client.flood_sleep_threshold == 60

    async def test_retries_exhausted_value_error_is_mapped(self) -> None:
        client = ChannelClient(
            history=[Msg(id=10, message="старый")],
            edit_errors=[ValueError("Request was unsuccessful 5 time(s)")],
        )

        with pytest.raises(ChannelPostError) as caught:
            await edit_post(client, edit_request())
        assert caught.value.code == "telegram_unavailable"

    async def test_expired_command_is_not_applied(self) -> None:
        client = ChannelClient(history=[Msg(id=10, message="старый")])
        request = edit_request(expires_at=DATE.timestamp() - 1)

        with pytest.raises(ChannelPostError) as caught:
            await edit_post(client, request, now=lambda: DATE)

        assert caught.value.code == "command_expired"
        assert client.calls == []

    async def test_expiry_is_rechecked_after_flood_wait(self) -> None:
        moment = [DATE.timestamp()]
        client = ChannelClient(history=[Msg(id=10, message="старый")], edit_errors=[flood(20)])

        async def slow_sleep(seconds: float) -> None:
            moment[0] += seconds

        request = edit_request(expires_at=DATE.timestamp() + 10)
        with pytest.raises(ChannelPostError) as caught:
            await edit_post(
                client,
                request,
                sleep=slow_sleep,
                now=lambda: datetime.fromtimestamp(moment[0], tz=UTC),
            )

        assert caught.value.code == "command_expired"
        assert client.count("edit_message") == 1

    @pytest.mark.parametrize(
        ("error", "code"),
        [
            (errors.ChatAdminRequiredError(request=None), "chat_admin_required"),
            (errors.MessageAuthorRequiredError(request=None), "message_author_required"),
            (errors.MessageEditTimeExpiredError(request=None), "message_edit_time_expired"),
            (errors.MessageIdInvalidError(request=None), "message_not_found"),
            (errors.MediaCaptionTooLongError(request=None), "caption_too_long"),
        ],
    )
    async def test_rpc_errors_get_clear_codes(self, error: BaseException, code: str) -> None:
        client = ChannelClient(history=[Msg(id=10, message="старый")], edit_errors=[error])

        with pytest.raises(ChannelPostError) as caught:
            await edit_post(client, edit_request())

        assert caught.value.code == code
        assert caught.value.details["telegram"] == type(error).__name__

    async def test_unknown_rpc_error_is_telegram_error(self) -> None:
        weird = errors.RPCError(request=None, message="SOMETHING_WEIRD", code=400)
        client = ChannelClient(history=[Msg(id=10, message="старый")], edit_errors=[weird])

        with pytest.raises(TelegramError) as caught:
            await edit_post(client, edit_request())
        assert "SOMETHING_WEIRD" in caught.value.message

    async def test_post_texts_do_not_reach_logs(self) -> None:
        client = ChannelClient(history=[Msg(id=10, message="СТАРЫЙ-СЕКРЕТ")])

        with capture_logs() as logs:
            await edit_post(client, edit_request(text="НОВЫЙ-СЕКРЕТ"))

        dumped = repr(logs)
        assert "channel_post_edited" in dumped
        assert "СЕКРЕТ" not in dumped


# --- обработчик команд --------------------------------------------------------
def make_handler(client: Any) -> CommandHandler:
    # Посты канала трогают только ClientManager.get — остальное не нужно.
    clients = SimpleNamespace(get=lambda _account_id: client)
    return CommandHandler(
        make_settings(),
        database=None,
        accounts=None,
        clients=clients,
        auth=None,
        sessions=None,
        sender=None,
    )


def command(kind: CommandType, **payload: Any) -> Command:
    return Command(type=kind, account_id=uuid.uuid4(), payload=payload)


class TestCommandHandler:
    async def test_read_channel_posts_command(self) -> None:
        client = ChannelClient(history=[Msg(id=2, message="b"), Msg(id=1, message="a")])

        result = await make_handler(client).handle(
            command(CommandType.READ_CHANNEL_POSTS, chat="https://t.me/templates", limit=50)
        )

        assert result.ok
        assert [post["id"] for post in result.data["posts"]] == [1, 2]
        assert ("get_input_entity", "@templates") in client.calls

    async def test_edit_message_command(self) -> None:
        client = ChannelClient(history=[Msg(id=1, message="a")])

        result = await make_handler(client).handle(
            command(CommandType.EDIT_MESSAGE, chat="@templates", message_id=1, text="b")
        )

        assert result.ok
        assert result.data["edited"] is True

    async def test_flood_wait_seconds_reach_the_caller(self) -> None:
        client = ChannelClient(history=[Msg(id=1, message="a")], edit_errors=[flood(120)])

        result = await make_handler(client).handle(
            command(CommandType.EDIT_MESSAGE, chat="@templates", message_id=1, text="b")
        )

        assert not result.ok
        assert result.error_code == "telegram_flood_wait"
        assert result.data["seconds"] == 120

    async def test_domain_error_code_reaches_the_caller(self) -> None:
        client = ChannelClient(
            history=[Msg(id=1, message="a")],
            edit_errors=[errors.ChatAdminRequiredError(request=None)],
        )

        result = await make_handler(client).handle(
            command(CommandType.EDIT_MESSAGE, chat="@templates", message_id=1, text="b")
        )

        assert result.error_code == "chat_admin_required"
        assert result.data == {"telegram": "ChatAdminRequiredError"}

    async def test_payload_is_validated_before_client_lookup(self) -> None:
        result = await make_handler(None).handle(
            command(
                CommandType.EDIT_MESSAGE,
                chat="@templates",
                message_id=1,
                text="x",
                parse_mode="bb",
            )
        )

        assert result.error_code == "invalid_input"
        assert "parse_mode" in (result.error_message or "")

    async def test_resolve_chat_uses_shared_resolver(self) -> None:
        client = ChannelClient()

        result = await make_handler(client).handle(
            command(CommandType.RESOLVE_CHAT, reference="t.me/templates/5")
        )

        assert result.ok
        assert result.data["username"] == "templates"
        assert ("get_input_entity", "@templates") in client.calls


# --- настоящий TelegramClient ------------------------------------------------
class FakeSender:
    """Вместо MTProtoSender: отвечает на TL-запросы по таблице, в сеть не ходит."""

    def __init__(self, handlers: dict[type, Callable[[Any], Any]]) -> None:
        self.handlers = handlers
        self.requests: list[Any] = []

    def send(self, request: Any, ordered: bool = False) -> asyncio.Future[Any]:
        self.requests.append(request)
        future: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
        try:
            future.set_result(self.handlers[type(request)](request))
        except Exception as exc:  # noqa: BLE001 — ошибку отдаём так же, как Telegram
            future.set_exception(exc)
        return future

    def count(self, request_type: type) -> int:
        return sum(1 for request in self.requests if isinstance(request, request_type))


class FakeTime:
    """time.time для Telethon: сон теста двигает часы, которыми Telethon
    меряет, прошёл ли уже FloodWait."""

    def __init__(self) -> None:
        self.now = 1_790_000_000.0

    def time(self) -> float:
        return self.now


def real_channel() -> Any:
    return types.Channel(
        id=CHANNEL_ID,
        title="Шаблоны",
        photo=types.ChatPhotoEmpty(),
        date=DATE,
        creator=True,
        broadcast=True,
        access_hash=987654321,
        username="templates",
    )


def real_post(text: str = "старый") -> Any:
    return types.Message(
        id=10, peer_id=types.PeerChannel(CHANNEL_ID), date=DATE, message=text, out=True, post=True
    )


def telegram_handlers(edit: Callable[[Any], Any] | None = None) -> dict[type, Any]:
    channel = real_channel()

    def edited(request: Any) -> Any:
        message = types.Message(
            id=request.id,
            peer_id=types.PeerChannel(CHANNEL_ID),
            date=DATE,
            message=request.message,
            edit_date=DATE,
        )
        return types.Updates(
            updates=[types.UpdateEditChannelMessage(message=message, pts=2, pts_count=1)],
            users=[],
            chats=[channel],
            date=DATE,
            seq=0,
        )

    return {
        functions.contacts.ResolveUsernameRequest: lambda _r: tl_contacts.ResolvedPeer(
            peer=types.PeerChannel(CHANNEL_ID), chats=[channel], users=[]
        ),
        functions.channels.GetChannelsRequest: lambda _r: tl_messages.Chats(chats=[channel]),
        functions.channels.GetMessagesRequest: lambda _r: tl_messages.ChannelMessages(
            pts=1, count=1, messages=[real_post()], topics=[], chats=[channel], users=[]
        ),
        functions.messages.EditMessageRequest: edit or edited,
    }


def real_client(sender: FakeSender) -> Any:
    client = TelegramClient(StringSession(), 12345, "0" * 32)
    client._sender = sender
    return client


class TestRealTelethonClient:
    async def test_username_is_resolved_once_across_commands(self) -> None:
        sender = FakeSender(telegram_handlers())
        handler = make_handler(real_client(sender))

        read = await handler.handle(
            command(CommandType.READ_CHANNEL_POSTS, chat="@templates", ids=[10])
        )
        assert read.ok, read.error_message
        for index in range(5):
            result = await handler.handle(
                command(
                    CommandType.EDIT_MESSAGE,
                    chat="@templates",
                    message_id=10,
                    text=f"новый {index}",
                )
            )
            assert result.ok, result.error_message

        # Одна команда — один GetChannels, но ResolveUsername — один на все:
        # дальше username находится в кеше сессии.
        assert sender.count(functions.contacts.ResolveUsernameRequest) == 1
        assert sender.count(functions.channels.GetChannelsRequest) == 6
        assert sender.count(functions.messages.EditMessageRequest) == 5

    async def test_flood_wait_45_reaches_the_caller_instead_of_sleeping(self) -> None:
        def flood_45(request: Any) -> Any:
            raise errors.FloodWaitError(request=request, capture=45)

        sender = FakeSender(telegram_handlers(edit=flood_45))
        client = real_client(sender)

        # С порогом Telethon по умолчанию (60 с) он проспал бы 45 с сам и
        # повторил запрос; wait_for роняет тест, а не вешает его.
        result = await asyncio.wait_for(
            make_handler(client).handle(
                command(CommandType.EDIT_MESSAGE, chat="@templates", message_id=10, text="новый")
            ),
            timeout=5,
        )

        assert result.error_code == "telegram_flood_wait"
        assert result.data["seconds"] == 45
        assert sender.count(functions.messages.EditMessageRequest) == 1
        assert client.flood_sleep_threshold == 60

    async def test_short_flood_wait_is_waited_once_by_us(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        clock = FakeTime()
        monkeypatch.setattr(telethon_users, "time", clock)
        answers: list[Any] = [errors.FloodWaitError(request=None, capture=20)]
        success = telegram_handlers()[functions.messages.EditMessageRequest]

        def edit(request: Any) -> Any:
            if answers:
                raise answers.pop(0)
            return success(request)

        sender = FakeSender(telegram_handlers(edit=edit))
        slept: list[float] = []

        async def sleep(seconds: float) -> None:
            slept.append(seconds)
            clock.now += seconds

        result = await asyncio.wait_for(
            edit_post(real_client(sender), edit_request(), sleep=sleep), timeout=5
        )

        assert slept == [21]
        assert result["edited"] is True
        assert result["flood_waited"] == 20
        assert sender.count(functions.messages.EditMessageRequest) == 2
