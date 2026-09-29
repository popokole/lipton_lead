"""Чтение и правка постов канала на стороне воркера (app/telegram/channel_posts.py).

Реальный Telegram в тестах запрещён (ТЗ §37): клиент — управляемая подделка,
которая повторяет семантику Telethon там, где на неё опирается код (порядок
iter_messages «от нового к старому», offset_id как верхняя граница, None для
отсутствующих id в get_messages).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import pytest
from structlog.testing import capture_logs
from telethon import errors, types

from app.bus.messages import Command, CommandType
from app.core.errors import InvalidInputError, TelegramError, TelegramFloodWaitError
from app.telegram.channel_posts import (
    CAPTION_LIMIT,
    FLOOD_RETRY_MAX_SECONDS,
    READ_LIMIT_MAX,
    TEXT_LIMIT,
    ChannelPostError,
    EditRequest,
    ReadRequest,
    edit_post,
    media_type,
    normalize_chat_reference,
    parse_edit_payload,
    parse_read_payload,
    read_posts,
    rendered_length,
)
from app.workers.command_handler import CommandHandler
from tests.conftest import make_settings

DATE = datetime(2026, 9, 1, 12, 30, tzinfo=UTC)


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
    id: int = 1234567890
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
    get_entity_error: BaseException | None = None
    broken_offsets: dict[int, BaseException] = field(default_factory=dict)
    dialogs: list[Any] = field(default_factory=list)

    calls: list[tuple[Any, ...]] = field(default_factory=list)

    async def get_entity(self, reference: Any) -> Any:
        self.calls.append(("get_entity", reference))
        if self.get_entity_error is not None:
            raise self.get_entity_error
        return self.entity

    async def get_dialogs(self, limit: int | None = None) -> list[Any]:
        self.calls.append(("get_dialogs", limit))
        return self.dialogs

    def iter_messages(self, entity: Any, *, limit: int | None = None, offset_id: int = 0) -> Any:
        self.calls.append(("iter_messages", limit, offset_id))
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
        self.calls.append(("get_messages", ids))
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
        self.calls.append(("edit_message", message, text, parse_mode, link_preview))
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
            ("https://t.me/c/1234567890/55", -1001234567890),
            ("https://t.me/+AbCdEf12345", "https://t.me/+AbCdEf12345"),
            ("t.me/joinchat/AbCdEf123", "https://t.me/+AbCdEf123"),
            ("+AbCdEf12345", "https://t.me/+AbCdEf12345"),
            ("1AbC-dEf_ghij", "https://t.me/+1AbC-dEf_ghij"),
            ("-1001234567890", -1001234567890),
            ("1234567890", 1234567890),
            (" 42 ", 42),
            (777, 777),
            ("+79991234567", "+79991234567"),
        ],
    )
    def test_accepted_forms(self, raw: Any, expected: Any) -> None:
        assert normalize_chat_reference(raw) == expected

    @pytest.mark.parametrize("raw", [None, "", "   ", True, "t.me/+", "joinchat/"])
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

    @pytest.mark.parametrize(
        "payload",
        [
            {"chat": "@templates", "limit": 0},
            {"chat": "@templates", "limit": READ_LIMIT_MAX + 1},
            {"chat": "@templates", "limit": True},
            {"chat": "@templates", "limit": "10"},
            {"chat": "@templates", "offset_id": -1},
            {"limit": 10},
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
            chat="@templates", message_id=7, text="новый", parse_mode=None, link_preview=False
        )

    @pytest.mark.parametrize("parse_mode", ["html", "md", None])
    def test_parse_modes(self, parse_mode: str | None) -> None:
        request = parse_edit_payload(
            {"chat": "@templates", "message_id": 7, "text": "x", "parse_mode": parse_mode}
        )
        assert request.parse_mode == parse_mode

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
        media = types.MessageMediaWebPage(webpage=types.WebPageEmpty(id=1))
        assert media_type(Msg(id=1, media=media)) == "webpage"

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
            "id": 1234567890,
            "title": "Шаблоны",
            "username": "templates",
            "type": "channel",
        }
        first, second = result["posts"]
        assert first["text"] == "Первый"
        assert first["date"] == DATE.isoformat()
        assert first["entities_present"] is True
        assert first["formatting_present"] is True
        assert first["html"] == "<strong>Первый</strong>"
        assert first["has_media"] is False
        assert first["length"] == 6
        assert second["media_type"] == "photo"
        assert second["has_media"] is True
        assert second["grouped_id"] == 99
        assert second["views"] == 40
        assert second["entities_present"] is False
        assert second["html"] is None
        assert first["can_edit"] is True  # создатель канала

    async def test_auto_entities_are_not_author_formatting(self) -> None:
        url = types.MessageEntityUrl(offset=0, length=13)
        client = ChannelClient(history=[Msg(id=1, message="https://x.com", entities=[url])])

        post = (await read_posts(client, ReadRequest(chat="@templates")))["posts"][0]

        assert post["entities_present"] is True
        assert post["formatting_present"] is False

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
            (Channel(broadcast=False, megagroup=True, creator=False), Msg(id=1, out=True), None),
            (Channel(broadcast=False, megagroup=True, creator=False), Msg(id=1), False),
            (Channel(min=True), Msg(id=1), None),
        ],
    )
    async def test_can_edit(self, entity: Any, message: Msg, expected: bool | None) -> None:
        client = ChannelClient(entity=entity, history=[message])

        post = (await read_posts(client, ReadRequest(chat="@templates")))["posts"][0]

        assert post["can_edit"] is expected


class TestResolve:
    async def test_numeric_id_falls_back_to_dialogs_by_bare_id(self) -> None:
        channel = Channel(id=555)
        client = ChannelClient(
            get_entity_error=ValueError("Could not find the input entity"),
            dialogs=[SimpleNamespace(id=-100555, entity=channel)],
            history=[Msg(id=1, message="x")],
        )

        result = await read_posts(client, ReadRequest(chat=555))

        assert result["chat"]["id"] == 555
        assert client.count("get_dialogs") == 1

    async def test_marked_id_matches_dialog_id(self) -> None:
        channel = Channel(id=555)
        client = ChannelClient(
            get_entity_error=ValueError("nope"),
            dialogs=[SimpleNamespace(id=-100555, entity=channel)],
            history=[Msg(id=1, message="x")],
        )

        result = await read_posts(client, ReadRequest(chat=-100555))

        assert result["chat"]["id"] == 555

    async def test_unknown_numeric_id_is_chat_not_found(self) -> None:
        client = ChannelClient(get_entity_error=ValueError("nope"), dialogs=[])

        with pytest.raises(ChannelPostError) as caught:
            await read_posts(client, ReadRequest(chat=999))
        assert caught.value.code == "chat_not_found"

    async def test_unknown_username_does_not_scan_dialogs(self) -> None:
        client = ChannelClient(get_entity_error=ValueError("No user has 'nobody' as username"))

        with pytest.raises(ChannelPostError) as caught:
            await read_posts(client, ReadRequest(chat="@nobody"))
        assert caught.value.code == "chat_not_found"
        assert client.count("get_dialogs") == 0

    async def test_username_rpc_error_is_mapped(self) -> None:
        client = ChannelClient(get_entity_error=errors.UsernameNotOccupiedError(request=None))

        with pytest.raises(ChannelPostError) as caught:
            await read_posts(client, ReadRequest(chat="@nobody"))
        assert caught.value.code == "chat_not_found"


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

    async def test_parse_mode_and_link_preview_are_passed_through(self) -> None:
        client = ChannelClient(history=[Msg(id=10, message="старый")])

        await edit_post(
            client, edit_request(text="<b>жирный</b>", parse_mode="html", link_preview=True)
        )

        assert ("edit_message", 10, "<b>жирный</b>", "html", True) in client.calls

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
        webpage = types.MessageMediaWebPage(webpage=types.WebPageEmpty(id=1))
        client = ChannelClient(history=[Msg(id=10, message="см. ссылку", media=webpage)])

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


class TestCommandHandler:
    async def test_read_channel_posts_command(self) -> None:
        client = ChannelClient(history=[Msg(id=2, message="b"), Msg(id=1, message="a")])
        command = Command(
            type=CommandType.READ_CHANNEL_POSTS,
            account_id=uuid.uuid4(),
            payload={"chat": "https://t.me/templates", "limit": 50},
        )

        result = await make_handler(client).handle(command)

        assert result.ok
        assert [post["id"] for post in result.data["posts"]] == [1, 2]
        assert ("get_entity", "@templates") in client.calls

    async def test_edit_message_command(self) -> None:
        client = ChannelClient(history=[Msg(id=1, message="a")])
        command = Command(
            type=CommandType.EDIT_MESSAGE,
            account_id=uuid.uuid4(),
            payload={"chat": "@templates", "message_id": 1, "text": "b"},
        )

        result = await make_handler(client).handle(command)

        assert result.ok
        assert result.data["edited"] is True

    async def test_flood_wait_seconds_reach_the_caller(self) -> None:
        client = ChannelClient(history=[Msg(id=1, message="a")], edit_errors=[flood(120)])
        command = Command(
            type=CommandType.EDIT_MESSAGE,
            account_id=uuid.uuid4(),
            payload={"chat": "@templates", "message_id": 1, "text": "b"},
        )

        result = await make_handler(client).handle(command)

        assert not result.ok
        assert result.error_code == "telegram_flood_wait"
        assert result.data["seconds"] == 120

    async def test_domain_error_code_reaches_the_caller(self) -> None:
        client = ChannelClient(
            history=[Msg(id=1, message="a")],
            edit_errors=[errors.ChatAdminRequiredError(request=None)],
        )
        command = Command(
            type=CommandType.EDIT_MESSAGE,
            account_id=uuid.uuid4(),
            payload={"chat": "@templates", "message_id": 1, "text": "b"},
        )

        result = await make_handler(client).handle(command)

        assert result.error_code == "chat_admin_required"
        assert result.data == {"telegram": "ChatAdminRequiredError"}

    async def test_payload_is_validated_before_client_lookup(self) -> None:
        command = Command(
            type=CommandType.EDIT_MESSAGE,
            account_id=uuid.uuid4(),
            payload={"chat": "@templates", "message_id": 1, "text": "x", "parse_mode": "bb"},
        )

        result = await make_handler(None).handle(command)

        assert result.error_code == "invalid_input"
        assert "parse_mode" in (result.error_message or "")

    async def test_resolve_chat_uses_shared_resolver(self) -> None:
        client = ChannelClient()
        command = Command(
            type=CommandType.RESOLVE_CHAT,
            account_id=uuid.uuid4(),
            payload={"reference": "t.me/templates/5"},
        )

        result = await make_handler(client).handle(command)

        assert result.ok
        assert result.data["username"] == "templates"
        assert ("get_entity", "@templates") in client.calls
