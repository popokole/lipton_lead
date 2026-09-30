"""Файлы из чата (app/telegram/chat_media.py) и MP4-анимации в замене фото."""

from __future__ import annotations

import base64
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from telethon import types

from app.core.errors import InvalidInputError
from app.telegram.channel_posts import (
    ChannelPostError,
    MediaRequest,
    parse_media_payload,
    replace_photo,
    upload_kind,
)
from app.telegram.chat_media import ChatMediaRequest, list_chat_media, parse_chat_media_payload

DATE = datetime(2026, 9, 30, 12, 0, tzinfo=UTC)
MP4 = b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 32
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16


@dataclass
class Msg:
    id: int
    message: str = ""
    media: Any = None
    date: datetime = DATE
    grouped_id: int | None = None
    sender_id: int | None = 7
    file: Any = None
    action: Any = None
    entities: list[Any] | None = None
    sticker: Any = None
    video_note: Any = None
    gif: Any = None
    voice: Any = None
    audio: Any = None
    video: Any = None


@dataclass
class Group:
    id: int = 555
    title: str = "Работы"
    username: str | None = None
    broadcast: bool = False
    megagroup: bool = True


@dataclass
class Client:
    history: list[Msg] = field(default_factory=list)
    entity: Any = field(default_factory=Group)
    downloads: list[tuple[int, str]] = field(default_factory=list)
    edits: list[dict[str, Any]] = field(default_factory=list)
    flood_sleep_threshold: int = 60

    async def get_input_entity(self, reference: Any) -> Any:
        return ("peer", reference)

    async def get_entity(self, peer: Any) -> Any:
        return self.entity

    def iter_messages(self, entity: Any, *, limit: int | None = None, offset_id: int = 0) -> Any:
        items = sorted(self.history, key=lambda m: -m.id)[:limit]

        async def gen() -> Any:
            for item in items:
                yield item

        return gen()

    async def download_media(self, message: Any, file: str) -> str:
        self.downloads.append((message.id, file))
        return file + ".jpg"

    async def get_messages(self, entity: Any, *, ids: Any) -> Any:
        by_id = {m.id: m for m in self.history}
        return by_id.get(ids) if isinstance(ids, int) else [by_id.get(i) for i in ids]

    async def edit_message(self, entity: Any, message: int, text: str, **kwargs: Any) -> Any:
        self.edits.append({"id": message, "text": text, **kwargs})
        return SimpleNamespace(id=message)


def photo_msg(message_id: int, **kwargs: Any) -> Msg:
    return Msg(
        id=message_id,
        media=types.MessageMediaPhoto(),
        file=SimpleNamespace(name=None, size=1000),
        **kwargs,
    )


class TestParseChatMedia:
    def test_defaults(self) -> None:
        assert parse_chat_media_payload({"chat": "@works"}) == ChatMediaRequest(chat="@works")

    @pytest.mark.parametrize(
        "payload",
        [
            {"chat": "@w", "limit": 0},
            {"chat": "@w", "limit": 201},
            {"chat": "@w", "download": "yes"},
        ],
    )
    def test_bad_payload(self, payload: dict[str, Any]) -> None:
        with pytest.raises(InvalidInputError):
            parse_chat_media_payload(payload)


class TestListChatMedia:
    async def test_lists_media_only_old_to_new(self, tmp_path: Path) -> None:
        client = Client(
            history=[photo_msg(3, message="работа"), Msg(id=2, message="текст"), photo_msg(1)]
        )

        result = await list_chat_media(client, ChatMediaRequest(chat="@w"), media_dir=tmp_path)

        assert [item["id"] for item in result["items"]] == [1, 3]
        assert result["items"][1]["text"] == "работа"
        assert result["items"][0]["kind"] == "photo"
        assert client.downloads == []
        assert result["dir"] is None

    async def test_download_into_chat_folder(self, tmp_path: Path) -> None:
        client = Client(history=[photo_msg(5)])

        result = await list_chat_media(
            client, ChatMediaRequest(chat="@w", download=True), media_dir=tmp_path
        )

        folder = tmp_path / "1000000000555"  # помеченный id супергруппы без знака
        assert client.downloads == [(5, str(folder / "5"))]
        assert result["items"][0]["file"] == str(folder / "5") + ".jpg"
        assert result["dir"] == str(folder)

    async def test_min_id_and_too_large(self, tmp_path: Path) -> None:
        big = photo_msg(9)
        big.file = SimpleNamespace(name="v.mp4", size=10**10)
        client = Client(history=[photo_msg(4), big])

        result = await list_chat_media(
            client, ChatMediaRequest(chat="@w", download=True, min_id=4), media_dir=tmp_path
        )

        assert [item["id"] for item in result["items"]] == [9]
        assert result["items"][0]["skipped"] == "too_large"
        assert client.downloads == []


def b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


class TestAnimationPayload:
    def test_mp4_needs_animation_fields(self) -> None:
        with pytest.raises(InvalidInputError, match="animation"):
            parse_media_payload({"chat": "@c", "message_id": 1, "image_b64": b64(MP4)})

    def test_mp4_payload(self) -> None:
        request = parse_media_payload(
            {
                "chat": "@c",
                "message_id": 1,
                "image_b64": b64(MP4),
                "animation": {"w": 1080, "h": 1350, "duration": 6},
            }
        )
        assert request.file_name == "post_1.mp4"
        assert request.animation == (1080, 1350, 6.0)

    def test_kinds(self) -> None:
        assert upload_kind(PNG) == "png"
        assert upload_kind(MP4) == "mp4"
        assert upload_kind(b"GIF89a" + b"\x00" * 20) is None


class TestReplaceWithAnimation:
    async def test_sends_animated_document_and_keeps_caption(self) -> None:
        channel = SimpleNamespace(id=1, title="К", username="k", broadcast=True, megagroup=False)
        client = Client(history=[photo_msg(10, message="подпись")], entity=channel)
        request = MediaRequest(
            chat="@k", message_id=10, data=MP4, file_name="post_10.mp4", animation=(1080, 1350, 6.0)
        )

        await replace_photo(client, request)

        edit = client.edits[0]
        assert edit["text"] == "подпись"
        kinds = [type(attribute).__name__ for attribute in edit["attributes"]]
        assert kinds == ["DocumentAttributeVideo", "DocumentAttributeAnimated"]
        assert edit["attributes"][0].w == 1080

    async def test_album_post_is_refused(self) -> None:
        channel = SimpleNamespace(id=1, title="К", username="k", broadcast=True, megagroup=False)
        client = Client(history=[photo_msg(10, grouped_id=77)], entity=channel)
        request = MediaRequest(
            chat="@k", message_id=10, data=MP4, file_name="post_10.mp4", animation=(1080, 1350, 6.0)
        )

        with pytest.raises(ChannelPostError) as caught:
            await replace_photo(client, request)

        assert caught.value.code == "in_album"
        assert client.edits == []
