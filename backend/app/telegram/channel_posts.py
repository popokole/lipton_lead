"""Чтение, правка и удаление постов канала от имени аккаунта (не бота).

Задача владельца: в канале, где наш аккаунт — администратор, прочитать все
посты-шаблоны и потом заменить их тексты. Делает это живой Telethon-клиент
воркера: второе подключение той же сессии из другого процесса разлогинит
аккаунт (AuthKeyDuplicatedError), поэтому сюда приходят команды шины, а не
прямые вызовы из API или CLI.

Что здесь важно:
  - чат ищется через кеш сессии (get_input_entity), а не get_entity(строка):
    последний на каждый вызов шлёт ResolveUsername/CheckChatInvite — запросы
    с самыми жёсткими лимитами Telegram. Правка сотни постов подряд так
    заработала бы FloodWait на часы для всего аккаунта;
  - работаем только с каналами (broadcast): голый id или ссылка, случайно
    указывающие на личку или группу, не должны привести к правке чужих
    сообщений;
  - длина проверяется ДО запроса на правку — так, как её считает Telegram:
    после разбора разметки и в UTF-16 (эмодзи — две единицы). Лимит для
    подписи к медиа (1024) меньше, чем для текста (4096), поэтому перед
    правкой читаем сам пост и смотрим, есть ли у него медиа;
  - превью ссылки (MessageMediaWebPage) медиа не считается: это обычный
    текстовый пост, и лимит у него текстовый;
  - FloodWait: на время команды сон Telethon на FloodWait выключен (иначе он
    молча спит до 60 с и повторяет запрос до пяти раз), а сами мы один раз
    на команду пережидаем FloodWait до FLOOD_RETRY_MAX_SECONDS; дольше —
    отдаём вызывающему с числом секунд;
  - MessageNotModified — не ошибка: текст уже такой, какой просили;
  - удаление повторяемо: воркер смотрит, какие посты есть, удаляет их и
    перечитывает, так что повтор той же команды после таймаута ничего не
    ломает — удалённые придут в already_missing;
  - HTML постов — свой, без потерь (спойлеры, блоки кода с языком), и один и
    тот же для выгрузки, подсчёта длины и правки;
  - в лог уходят только id и длины, не тексты постов.
"""

from __future__ import annotations

import asyncio
import contextlib
import html as html_lib
import re
from collections.abc import Awaitable, Callable, Iterator, Sequence
from dataclasses import dataclass
from datetime import datetime
from functools import partial
from typing import Any

from telethon import helpers as tl_helpers
from telethon.extensions.html import HTMLToTelegramParser
from telethon.tl import types as tl_types

from app.core.clock import utcnow
from app.core.errors import AppError, InvalidInputError, TelegramError, TelegramFloodWaitError
from app.core.logging import get_logger

logger = get_logger(__name__)

TEXT_LIMIT = 4096
CAPTION_LIMIT = 1024
READ_LIMIT_MAX = 500
DEFAULT_READ_LIMIT = 100
# Telethon всё равно режет выгрузку истории на запросы по 100 сообщений, а
# ответ каждого разбирается атомарно (см. app/pipeline/reconcile.py): одна
# битая пачка не должна прятать уже прочитанные посты.
READ_BATCH_SIZE = 100
# channels.deleteMessages принимает до 100 id за запрос.
DELETE_IDS_MAX = 100
FLOOD_RETRY_MAX_SECONDS = 30
PARSE_MODES: tuple[str, ...] = ("html", "md")
_MAX_MESSAGE_ID = 2**31 - 1
# Помеченный id канала, как utils.get_peer_id в Telethon: -(10**12 + id).
_CHANNEL_ID_SHIFT = 10**12

Sleep = Callable[[float], Awaitable[None]]
Now = Callable[[], datetime]


class ChannelPostError(AppError):
    """Ожидаемая ошибка чтения/правки поста с машинным кодом причины.

    Код уходит в CommandResult.error_code: по нему CLI решает, стоит ли
    повторять, а человек сразу видит, что именно не так (нет прав, пост не
    найден, текст длинный), без разбора текста исключения Telethon.
    """

    code = "channel_post_error"
    message = "Channel post operation failed"

    def __init__(self, code: str, message: str, **details: Any) -> None:
        super().__init__(message, **details)
        self.code = code


# --- ссылка на чат ------------------------------------------------------------
_TME_RE = re.compile(
    r"^(?:https?://)?(?:www\.)?(?:t\.me|telegram\.me|telegram\.dog)/(?P<rest>.+)$",
    re.IGNORECASE,
)
_NUMERIC_RE = re.compile(r"-?\d+")
_USERNAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{3,31}$")
_INVITE_HASH_RE = re.compile(r"^[A-Za-z0-9_-]{8,}$")


def marked_channel_id(channel_id: int) -> int:
    return -(_CHANNEL_ID_SHIFT + channel_id)


def normalize_chat_reference(raw: Any) -> str | int:
    """Приводит то, чем человек обозначил чат, к виду, который понимает Telethon.

    Принимает: числовой id (голый или с -100), @username, username,
    ссылку t.me/<name>[/<пост>], t.me/c/<id>/<пост>, ссылку-приглашение
    t.me/+hash или t.me/joinchat/hash и сам хеш приглашения. Хеш без «+»,
    похожий на username, сначала ищется как username, а если такого нет —
    как приглашение (см. resolve_chat_entity); надёжнее передавать его с «+».
    """
    if isinstance(raw, bool):
        raise InvalidInputError("Field 'chat' must be a chat reference, not a boolean")
    if isinstance(raw, int):
        return raw
    if not isinstance(raw, str) or not raw.strip():
        raise InvalidInputError("Field 'chat' is required")

    ref = raw.strip()
    if _NUMERIC_RE.fullmatch(ref):
        return int(ref)
    if ref.startswith("-"):
        raise InvalidInputError(f"Cannot parse chat id: {ref}")

    match = _TME_RE.match(ref)
    if match:
        rest = match.group("rest").split("?", 1)[0].strip("/")
        parts = [part for part in rest.split("/") if part]
        if not parts:
            raise InvalidInputError(f"Cannot parse chat link: {ref}")
        head = parts[0]
        if head == "c" and len(parts) >= 2 and parts[1].isdigit():
            # Ссылка на пост приватного канала: t.me/c/<id>/<пост>.
            return marked_channel_id(int(parts[1]))
        if head.startswith("+"):
            return _invite_link(head[1:], ref)
        if head == "joinchat":
            return _invite_link(parts[1] if len(parts) > 1 else "", ref)
        if head == "s" and len(parts) >= 2:
            # Веб-превью канала: t.me/s/<name>.
            head = parts[1]
        return "@" + head.lstrip("@")

    if ref.startswith("@"):
        return ref
    if ref.startswith("+"):
        body = ref[1:]
        # «+79991234567» — телефон, get_entity понимает его сам.
        return ref if body.isdigit() else _invite_link(body, ref)
    if ref.startswith("joinchat/"):
        return _invite_link(ref.removeprefix("joinchat/"), ref)
    if _USERNAME_RE.match(ref):
        return "@" + ref
    if _INVITE_HASH_RE.match(ref):
        return _invite_link(ref, ref)
    return ref


def channel_reference(raw: Any) -> str | int:
    """Ссылка на канал для команд постов.

    Голый положительный id здесь — id канала (так его показывают многие
    инструменты), а не пользователя: по соглашению Telegram положительный id
    означает пользователя, и кеш Telethon ищет его неточно — мог бы найти
    личку с тем же номером. Поэтому переводим его в помеченный id канала.
    """
    ref = normalize_chat_reference(raw)
    if isinstance(ref, int) and ref > 0:
        return marked_channel_id(ref)
    return ref


def _invite_link(invite_hash: str, original: str) -> str:
    invite_hash = invite_hash.strip("/")
    if not invite_hash:
        raise InvalidInputError(f"Invite link has no hash: {original}")
    return f"https://t.me/+{invite_hash}"


async def resolve_chat_entity(client: Any, reference: str | int) -> Any:
    """Находит чат по нормализованной ссылке.

    get_input_entity сначала смотрит в кеш сессии (по username, id и хешу
    приглашения) и идёт в сеть только при промахе, поэтому ResolveUsername
    уходит один раз на чат, а не на каждую команду. Полную сущность (с
    правами администратора) даёт get_entity(InputPeer) — дешёвый GetChannels.

    Сессия, собранная из tdata, приходит без кеша сущностей: по числовому id
    чат не найдётся, пока аккаунт его не «увидит». Для числового id делаем
    одну запасную попытку через список диалогов — он заодно наполняет кеш.
    """
    from telethon.errors import RPCError, UsernameInvalidError, UsernameNotOccupiedError

    try:
        return await _entity_via_cache(client, reference)
    except (UsernameInvalidError, UsernameNotOccupiedError, ValueError) as exc:
        if _retries_exhausted(exc):
            raise telegram_unavailable(exc) from None
        entity = await _retry_as_invite(client, reference)
        if entity is not None:
            return entity
        if isinstance(exc, RPCError):
            raise telegram_error(exc) from None
        detail = str(exc)[:200]
        if isinstance(reference, int):
            entity = await _find_in_dialogs(client, reference)
            if entity is not None:
                return entity
            raise ChannelPostError(
                "chat_not_found",
                f"Аккаунт не видит чат {reference}: откройте его в аккаунте "
                "или укажите @username / ссылку",
                detail=detail,
            ) from None
        raise ChannelPostError(
            "chat_not_found", f"Чат {reference} не найден: {detail}", detail=detail
        ) from None
    except RPCError as exc:
        raise telegram_error(exc) from None


async def _entity_via_cache(client: Any, reference: str | int) -> Any:
    peer = await client.get_input_entity(reference)
    return await client.get_entity(peer)


async def _retry_as_invite(client: Any, reference: str | int) -> Any | None:
    """«@AbCdEfGhIjKlMnOp» мог быть хешем приглашения без «+»: одна попытка."""
    from telethon.errors import FloodWaitError, RPCError

    if not isinstance(reference, str) or not reference.startswith("@"):
        return None
    name = reference[1:]
    if not _INVITE_HASH_RE.match(name):
        return None
    try:
        return await _entity_via_cache(client, _invite_link(name, reference))
    except FloodWaitError as exc:
        raise telegram_error(exc) from None
    except (RPCError, ValueError):
        return None


async def _find_in_dialogs(client: Any, chat_id: int) -> Any | None:
    try:
        dialogs = list(await client.get_dialogs(limit=None) or [])
    except Exception as exc:  # noqa: BLE001 — запасной путь, исходная ошибка важнее
        logger.warning("channel_dialogs_fallback_failed", detail=str(exc)[:200])
        return None
    for dialog in dialogs:
        if getattr(dialog, "id", None) == chat_id:
            return getattr(dialog, "entity", None)
    if chat_id > 0:
        # Голый id канала (без -100) совпадает с entity.id, а не с dialog.id.
        for dialog in dialogs:
            entity = getattr(dialog, "entity", None)
            if _is_channel_like(entity) and getattr(entity, "id", None) == chat_id:
                return entity
    return None


def _is_channel_like(entity: Any) -> bool:
    return bool(getattr(entity, "broadcast", False) or getattr(entity, "megagroup", False))


def describe_chat(entity: Any) -> dict[str, Any]:
    """Сводка о чате. id — помеченный (-100… у каналов): им чат однозначно
    адресуется в следующих командах, без повторного поиска по username."""
    kind = "user"
    if getattr(entity, "broadcast", False):
        kind = "channel"
    elif getattr(entity, "megagroup", False):
        kind = "supergroup"
    elif getattr(entity, "title", None) is not None:
        kind = "group"
    raw_id = _int_or_none(getattr(entity, "id", None))
    return {
        "id": _marked_id(raw_id, kind),
        "title": getattr(entity, "title", None),
        "username": getattr(entity, "username", None),
        "type": kind,
    }


def _marked_id(raw_id: int | None, kind: str) -> int | None:
    if raw_id is None:
        return None
    if kind in ("channel", "supergroup"):
        return marked_channel_id(raw_id)
    if kind == "group":
        return -raw_id
    return raw_id


def require_channel(entity: Any) -> None:
    """Посты читаем и правим только в каналах."""
    if getattr(entity, "broadcast", False):
        return
    chat = describe_chat(entity)
    name = chat["title"] or chat["username"] or getattr(entity, "first_name", None) or chat["id"]
    raise ChannelPostError(
        "not_a_channel",
        f"«{name}» — не канал (тип: {chat['type']}); посты читаются и правятся только в каналах",
        chat_type=chat["type"],
        tg_chat_id=chat["id"],
    )


# --- разбор payload ----------------------------------------------------------
@dataclass(frozen=True, slots=True)
class ReadRequest:
    chat: str | int
    limit: int = DEFAULT_READ_LIMIT
    offset_id: int = 0
    ids: tuple[int, ...] | None = None


@dataclass(frozen=True, slots=True)
class EditRequest:
    chat: str | int
    message_id: int
    text: str
    parse_mode: str | None = None
    link_preview: bool = False
    # Unix-время, после которого правку применять нельзя: вызывающий уже
    # перестал ждать ответа и не должен получить «тихую» правку потом.
    expires_at: float | None = None


@dataclass(frozen=True, slots=True)
class DeleteRequest:
    chat: str | int
    ids: tuple[int, ...]
    expires_at: float | None = None


def parse_read_payload(payload: dict[str, Any]) -> ReadRequest:
    """READ_CHANNEL_POSTS: {chat, limit ≤ 500, offset_id = 0, ids?}.

    offset_id — как в Telegram: читаются посты с id < offset_id, 0 — с самого
    нового. ids — точечное чтение конкретных постов (для бэкапа перед правкой).
    """
    chat = channel_reference(payload.get("chat"))
    limit = _int_field(payload, "limit", DEFAULT_READ_LIMIT, low=1, high=READ_LIMIT_MAX)
    offset_id = _int_field(payload, "offset_id", 0, low=0, high=_MAX_MESSAGE_ID)

    ids: tuple[int, ...] | None = None
    raw_ids = payload.get("ids")
    if raw_ids is not None:
        if not isinstance(raw_ids, list) or not raw_ids:
            raise InvalidInputError("Field 'ids' must be a non-empty list of message ids")
        if len(raw_ids) > READ_LIMIT_MAX:
            raise InvalidInputError(f"Field 'ids' accepts at most {READ_LIMIT_MAX} ids")
        parsed = [_positive_id(value, "ids[]") for value in raw_ids]
        ids = tuple(dict.fromkeys(parsed))
    return ReadRequest(chat=chat, limit=limit, offset_id=offset_id, ids=ids)


def parse_edit_payload(payload: dict[str, Any]) -> EditRequest:
    """EDIT_MESSAGE: {chat, message_id, text, parse_mode: html|md|None,
    link_preview=False, expires_at?}."""
    chat = channel_reference(payload.get("chat"))
    message_id = _positive_id(payload.get("message_id"), "message_id")

    text = payload.get("text")
    if not isinstance(text, str):
        raise InvalidInputError("Field 'text' must be a string")

    parse_mode = payload.get("parse_mode")
    if parse_mode is not None and parse_mode not in PARSE_MODES:
        raise InvalidInputError("Field 'parse_mode' must be 'html', 'md' or null")

    link_preview = payload.get("link_preview", False)
    if not isinstance(link_preview, bool):
        raise InvalidInputError("Field 'link_preview' must be a boolean")

    return EditRequest(
        chat=chat,
        message_id=message_id,
        text=text,
        parse_mode=parse_mode,
        link_preview=link_preview,
        expires_at=_expires_at(payload),
    )


def parse_delete_payload(payload: dict[str, Any]) -> DeleteRequest:
    """DELETE_MESSAGES: {chat, ids (1..100), expires_at?}."""
    chat = channel_reference(payload.get("chat"))
    raw_ids = payload.get("ids")
    if not isinstance(raw_ids, list) or not raw_ids:
        raise InvalidInputError("Field 'ids' must be a non-empty list of message ids")
    if len(raw_ids) > DELETE_IDS_MAX:
        raise InvalidInputError(f"Field 'ids' accepts at most {DELETE_IDS_MAX} ids")
    ids = tuple(dict.fromkeys(_positive_id(value, "ids[]") for value in raw_ids))
    return DeleteRequest(chat=chat, ids=ids, expires_at=_expires_at(payload))


def _expires_at(payload: dict[str, Any]) -> float | None:
    expires_at = payload.get("expires_at")
    if expires_at is None:
        return None
    if isinstance(expires_at, bool) or not isinstance(expires_at, int | float) or expires_at <= 0:
        raise InvalidInputError("Field 'expires_at' must be a unix timestamp")
    return float(expires_at)


def _int_field(payload: dict[str, Any], field: str, default: int, *, low: int, high: int) -> int:
    value = payload.get(field, default)
    if value is None:
        value = default
    if isinstance(value, bool) or not isinstance(value, int):
        raise InvalidInputError(f"Field '{field}' must be an integer")
    if not low <= value <= high:
        raise InvalidInputError(f"Field '{field}' must be between {low} and {high}")
    return value


def _positive_id(value: Any, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise InvalidInputError(f"Field '{field}' must be a positive integer")
    if not 0 < value <= _MAX_MESSAGE_ID:
        raise InvalidInputError(f"Field '{field}' must be a positive integer")
    return value


# --- HTML постов ---------------------------------------------------------------
# Telethon 1.x html.unparse теряет спойлеры и превращает блок кода в
# «<pre>\n    <code…>\n        текст{}\n…», а его html.parse не знает
# <tg-spoiler>. Выгрузка, подсчёт длины и правка должны видеть один и тот же
# HTML, иначе пост, собранный из выгрузки, сломается при правке. Поэтому свой
# формат: теги Bot API (b, i, u, s, code, pre, a, tg-spoiler, blockquote,
# tg-emoji), разбор — парсером Telethon с поддержкой спойлеров.
_SIMPLE_TAGS = {
    "MessageEntityBold": "b",
    "MessageEntityItalic": "i",
    "MessageEntityUnderline": "u",
    "MessageEntityStrike": "s",
    "MessageEntityCode": "code",
    "MessageEntitySpoiler": "tg-spoiler",
}

# Эти сущности Telegram расставляет сам (ссылки, @упоминания, #хэштеги) и
# расставит заново после правки — это не форматирование автора.
_AUTO_ENTITIES = frozenset(
    {
        "MessageEntityUrl",
        "MessageEntityMention",
        "MessageEntityHashtag",
        "MessageEntityCashtag",
        "MessageEntityBotCommand",
        "MessageEntityEmail",
        "MessageEntityPhone",
        "MessageEntityBankCard",
    }
)


class _PostHtmlParser(HTMLToTelegramParser):
    """Разбор HTML Telethon плюс спойлеры: <tg-spoiler> и <span class="tg-spoiler">."""

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        classes = (dict(attrs).get("class") or "").split()
        if tag != "tg-spoiler" and not (tag == "span" and "tg-spoiler" in classes):
            super().handle_starttag(tag, attrs)
            return
        self._open_tags.appendleft(tag)
        self._open_tags_meta.appendleft(None)
        if tag not in self._building_entities:
            self._building_entities[tag] = tl_types.MessageEntitySpoiler(
                offset=len(self.text), length=0
            )


def parse_html(text: str) -> tuple[str, list[Any]]:
    """HTML поста → (текст, сущности) — так их получит Telegram."""
    if not text:
        return text, []
    parser = _PostHtmlParser()
    parser.feed(tl_helpers.add_surrogate(text))
    # close() дописывает хвост текста, который HTMLParser придержал бы из-за
    # «&» без «;» в конце (Telethon его не вызывает и такой хвост теряет).
    parser.close()
    plain = tl_helpers.strip_text(parser.text, parser.entities)
    entities = list(parser.entities)
    entities.reverse()
    entities.sort(key=lambda entity: entity.offset)
    return str(tl_helpers.del_surrogate(plain)), entities


def unparse_html(text: str, entities: Sequence[Any]) -> str:
    """(текст, сущности) → HTML, который parse_html разберёт обратно без потерь."""
    if not text:
        return text
    spans: list[tuple[int, int, int, tuple[str, str]]] = []
    for index, entity in enumerate(entities or ()):
        tags = _entity_tags(entity)
        if tags is not None and entity.length > 0:
            spans.append((entity.offset, entity.offset + entity.length, index, tags))
    if not spans:
        return _escape_text(text)

    surrogated = tl_helpers.add_surrogate(text)
    # Внешние сущности открываются раньше внутренних и закрываются позже.
    spans.sort(key=lambda span: (span[0], -span[1], span[2]))
    opens: dict[int, list[str]] = {}
    closes: dict[int, list[str]] = {}
    for start, end, _index, (open_tag, close_tag) in spans:
        start, end = _boundary(surrogated, start), _boundary(surrogated, end)
        if start >= end:
            continue
        opens.setdefault(start, []).append(open_tag)
        closes.setdefault(end, []).insert(0, close_tag)

    parts: list[str] = []
    previous = 0
    for point in sorted(opens.keys() | closes.keys()):
        parts.append(_escape_text(surrogated[previous:point]))
        parts.extend(closes.get(point, ()))
        parts.extend(opens.get(point, ()))
        previous = point
    parts.append(_escape_text(surrogated[previous:]))
    return str(tl_helpers.del_surrogate("".join(parts)))


def _entity_tags(entity: Any) -> tuple[str, str] | None:
    name = type(entity).__name__
    simple = _SIMPLE_TAGS.get(name)
    if simple is not None:
        return f"<{simple}>", f"</{simple}>"
    if name == "MessageEntityPre":
        language = str(getattr(entity, "language", "") or "")
        if language:
            return f'<pre><code class="language-{_escape_attr(language)}">', "</code></pre>"
        return "<pre>", "</pre>"
    if name == "MessageEntityTextUrl":
        return f'<a href="{_escape_attr(str(entity.url))}">', "</a>"
    if name == "MessageEntityMentionName":
        return f'<a href="tg://user?id={int(entity.user_id)}">', "</a>"
    if name == "MessageEntityCustomEmoji":
        return f'<tg-emoji emoji-id="{int(entity.document_id)}">', "</tg-emoji>"
    if name == "MessageEntityBlockquote":
        opening = (
            "<blockquote expandable>" if getattr(entity, "collapsed", False) else "<blockquote>"
        )
        return opening, "</blockquote>"
    return None


def _boundary(text: str, at: int) -> int:
    at = max(0, min(at, len(text)))
    while at < len(text) and tl_helpers.within_surrogate(text, at):
        at += 1
    return at


def _escape_text(text: str) -> str:
    return html_lib.escape(text, quote=False)


def _escape_attr(value: str) -> str:
    return html_lib.escape(value, quote=True)


def entity_signature(entities: Sequence[Any]) -> list[tuple[str, int, int, str]]:
    """Форматирование автора в сравнимом виде (без авто-сущностей Telegram)."""
    signature: list[tuple[str, int, int, str]] = []
    for entity in entities or ():
        name = type(entity).__name__
        if name in _AUTO_ENTITIES:
            continue
        extra = ""
        if name == "MessageEntityMentionName":
            # Упоминание уходит в Telegram ссылкой tg://user?id=…
            name, extra = "MessageEntityTextUrl", f"tg://user?id={entity.user_id}"
        elif name == "MessageEntityTextUrl":
            extra = str(entity.url)
        elif name == "MessageEntityPre":
            extra = str(getattr(entity, "language", "") or "")
        elif name == "MessageEntityCustomEmoji":
            extra = str(entity.document_id)
        elif name == "MessageEntityBlockquote":
            extra = str(bool(getattr(entity, "collapsed", False)))
        signature.append((name, int(entity.offset), int(entity.length), extra))
    return sorted(signature)


def html_is_lossless(text: str, entities: Sequence[Any], html_text: str) -> bool:
    """Разберётся ли html_text обратно ровно в исходные текст и форматирование."""
    try:
        plain, parsed = parse_html(html_text)
    except Exception:  # noqa: BLE001 — неразборчивый HTML = с потерями
        return False
    return plain == text and entity_signature(parsed) == entity_signature(entities)


class _PostHtmlMode:
    """parse_mode для Telethon: тот же разбор, что у подсчёта длины и выгрузки."""

    @staticmethod
    def parse(text: str) -> tuple[str, list[Any]]:
        return parse_html(text)

    @staticmethod
    def unparse(text: str, entities: Sequence[Any]) -> str:
        return unparse_html(text, entities)


POST_HTML = _PostHtmlMode()


def telethon_parse_mode(parse_mode: str | None) -> Any:
    if parse_mode == "html":
        return POST_HTML
    # None передаётся явно: без аргумента Telethon применил бы markdown клиента.
    return parse_mode


# --- длина текста ------------------------------------------------------------
def utf16_length(text: str) -> int:
    """Длина в единицах UTF-16 — в них Telegram меряет текст и сущности."""
    return len(text.encode("utf-16-le")) // 2


def render_entities(text: str, parse_mode: str | None) -> tuple[str, list[Any]]:
    """Текст после разбора разметки и сущности — как их получит Telegram."""
    if parse_mode is None:
        return text, []
    from telethon.extensions import markdown

    try:
        plain, entities = parse_html(text) if parse_mode == "html" else markdown.parse(text)
    except Exception as exc:  # noqa: BLE001 — любая ошибка разбора = плохой ввод
        raise InvalidInputError(f"Cannot parse text as {parse_mode}: {exc}") from None
    return str(plain), list(entities or [])


def render_text(text: str, parse_mode: str | None) -> tuple[str, int]:
    """Текст после разбора разметки и число сущностей."""
    plain, entities = render_entities(text, parse_mode)
    return plain, len(entities)


def rendered_length(text: str, parse_mode: str | None) -> int:
    """Длина текста так, как её увидит Telegram: разметка снята, счёт в UTF-16."""
    plain, _entities = render_entities(text, parse_mode)
    return utf16_length(plain)


def length_limit(has_media: bool) -> int:
    return CAPTION_LIMIT if has_media else TEXT_LIMIT


def check_length(length: int, *, has_media: bool) -> None:
    limit = length_limit(has_media)
    if length <= limit:
        return
    if has_media:
        raise ChannelPostError(
            "caption_too_long",
            f"Подпись к медиа — {length} символов, Telegram разрешает не больше {limit}",
            length=length,
            limit=limit,
        )
    raise ChannelPostError(
        "text_too_long",
        f"Текст — {length} символов, Telegram разрешает не больше {limit}",
        length=length,
        limit=limit,
    )


# --- описание поста ----------------------------------------------------------
_MEDIA_NAMES = {
    "MessageMediaPhoto": "photo",
    "MessageMediaWebPage": "webpage",
    "MessageMediaPoll": "poll",
    "MessageMediaGeo": "geo",
    "MessageMediaGeoLive": "geo_live",
    "MessageMediaVenue": "venue",
    "MessageMediaContact": "contact",
    "MessageMediaDice": "dice",
    "MessageMediaGame": "game",
    "MessageMediaInvoice": "invoice",
    "MessageMediaStory": "story",
    "MessageMediaGiveaway": "giveaway",
    "MessageMediaPaidMedia": "paid_media",
    "MessageMediaUnsupported": "unsupported",
}
# Порядок важен: GIF и видеостикер тоже несут атрибут видео, голосовое — аудио.
_DOCUMENT_KINDS = (
    ("sticker", "sticker"),
    ("video_note", "video_note"),
    ("gif", "animation"),
    ("voice", "voice"),
    ("audio", "audio"),
    ("video", "video"),
)


def media_type(message: Any) -> str | None:
    media = getattr(message, "media", None)
    if media is None:
        return None
    name = type(media).__name__
    if name == "MessageMediaEmpty":
        return None
    if name == "MessageMediaDocument":
        for attribute, kind in _DOCUMENT_KINDS:
            if getattr(message, attribute, None):
                return kind
        return "document"
    if name in _MEDIA_NAMES:
        return _MEDIA_NAMES[name]
    tail = name.removeprefix("MessageMedia") or "other"
    return re.sub(r"(?<!^)(?=[A-Z])", "_", tail).lower()


def has_caption_media(kind: str | None) -> bool:
    """Есть ли у поста медиа, к которому текст — подпись (лимит 1024)."""
    return kind is not None and kind != "webpage"


@dataclass(frozen=True, slots=True)
class ChatRights:
    creator: bool
    edit_messages: bool
    post_messages: bool
    known: bool


def chat_rights(entity: Any) -> ChatRights:
    rights = getattr(entity, "admin_rights", None)
    return ChatRights(
        creator=bool(getattr(entity, "creator", False)),
        edit_messages=bool(getattr(rights, "edit_messages", False)),
        post_messages=bool(getattr(rights, "post_messages", False)),
        # «min»-сущность приходит без прав: по ней ничего сказать нельзя.
        known=not bool(getattr(entity, "min", False)),
    )


def can_edit(message: Any, rights: ChatRights) -> bool | None:
    """Может ли аккаунт править пост канала. None — определить заранее нельзя."""
    if getattr(message, "action", None) is not None:
        return False
    if not rights.known:
        return None
    if rights.creator or rights.edit_messages:
        return True
    return bool(rights.post_messages and getattr(message, "out", False))


def serialize_post(message: Any, rights: ChatRights) -> dict[str, Any]:
    text = getattr(message, "message", None) or ""
    entities = list(getattr(message, "entities", None) or [])
    formatting = [e for e in entities if type(e).__name__ not in _AUTO_ENTITIES]
    kind = media_type(message)
    date = getattr(message, "date", None)
    # Форматирование (жирный, ссылки) в сыром тексте теряется; HTML нужен,
    # чтобы править такой пост, сохранив его (и чтобы бэкап восстанавливался).
    html_text = _to_html(text, entities) if formatting else None
    return {
        "id": int(message.id),
        "date": date.isoformat() if date is not None else None,
        "text": text,
        "html": html_text,
        # False — HTML не передаёт форматирование поста целиком (неизвестный
        # тип сущности, пробелы по краям): правка по нему что-то потеряет.
        "html_lossless": (
            html_is_lossless(text, entities, html_text) if html_text is not None else None
        ),
        "length": utf16_length(text),
        "entities_present": bool(entities),
        # Форматирование автора (жирный, ссылка под словом…), которое
        # пропадёт при правке простым текстом.
        "formatting_present": bool(formatting),
        "has_media": has_caption_media(kind),
        "media_type": kind,
        "grouped_id": _int_or_none(getattr(message, "grouped_id", None)),
        "views": _int_or_none(getattr(message, "views", None)),
        "can_edit": can_edit(message, rights),
    }


def _to_html(text: str, entities: list[Any]) -> str | None:
    try:
        return unparse_html(text, entities)
    except Exception as exc:  # noqa: BLE001 — без HTML пост всё равно читается
        logger.debug("post_html_unparse_failed", detail=str(exc)[:120])
        return None


def _int_or_none(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


# --- FloodWait -----------------------------------------------------------------
@contextlib.contextmanager
def _flood_waits_surface(client: Any) -> Iterator[None]:
    """На время команды выключает сон Telethon на FloodWait.

    По умолчанию Telethon сам пересыпает FloodWait до flood_sleep_threshold
    (60 с) и повторяет запрос до пяти раз, а после пятого бросает ValueError.
    Такая правка молча держала бы очередь команд воркера (она
    последовательная) минутами, а FloodWait 31–60 с не доходил бы до
    вызывающего. С порогом 0 каждый FloodWait приходит к нам, и решение —
    переждать ≤ FLOOD_RETRY_MAX_SECONDS один раз или вернуть секунды —
    принимает _FloodBudget.

    Порог — свойство клиента: пока идёт команда, его видят и другие корутины
    этого аккаунта. Это безопасно: отправка ответов (MessageSender) свой
    FloodWait обрабатывает сама, а окно — секунды одной команды.
    """
    previous = getattr(client, "flood_sleep_threshold", None)
    if isinstance(previous, bool) or not isinstance(previous, int | float):
        yield
        return
    client.flood_sleep_threshold = 0
    try:
        yield
    finally:
        client.flood_sleep_threshold = previous


class _FloodBudget:
    """Один повтор на команду после FloodWait не длиннее FLOOD_RETRY_MAX_SECONDS."""

    def __init__(self, sleep: Sleep) -> None:
        self._sleep = sleep
        self._retried = False
        self.waited = 0

    async def run[T](self, call: Callable[[], Awaitable[T]], *, step: str, **log: Any) -> T:
        from telethon.errors import FloodWaitError

        while True:
            try:
                return await call()
            except (FloodWaitError, TelegramFloodWaitError) as exc:
                seconds = int(exc.seconds)
                if self._retried or seconds > FLOOD_RETRY_MAX_SECONDS:
                    logger.warning("channel_flood_wait_exceeded", step=step, seconds=seconds, **log)
                    raise TelegramFloodWaitError(
                        seconds, f"Telegram просит подождать {seconds} с перед следующим запросом"
                    ) from None
                self._retried = True
                self.waited += seconds
                logger.info("channel_flood_wait", step=step, seconds=seconds, **log)
                await self._sleep(seconds + 1)


# --- чтение ------------------------------------------------------------------
async def read_posts(
    client: Any, request: ReadRequest, *, sleep: Sleep = asyncio.sleep
) -> dict[str, Any]:
    """Посты канала от старых к новым. Служебные сообщения пропускаются.

    next_offset_id — чем продолжить выгрузку вглубь истории; None — история
    кончилась. partial=True — очередная пачка не прочиталась (новый TL-тип в
    Telethon, долгий FloodWait), отдаём то, что успели прочитать, и причину.
    """
    budget = _FloodBudget(sleep)
    with _flood_waits_surface(client):
        entity = await budget.run(
            partial(resolve_chat_entity, client, request.chat), step="resolve"
        )
        require_channel(entity)
        rights = chat_rights(entity)

        missing: list[int] | None = None
        partial_error: dict[str, Any] | None = None
        if request.ids is not None:
            raw, missing = await _fetch_by_ids(client, entity, request.ids, budget)
            next_offset_id: int | None = None
        else:
            raw, partial_error = await _fetch_history(
                client, entity, request.limit, request.offset_id, budget
            )
            exhausted = len(raw) < request.limit or partial_error is not None
            next_offset_id = None if exhausted or not raw else min(int(m.id) for m in raw)

    posts: list[dict[str, Any]] = []
    skipped = 0
    for message in raw:
        if getattr(message, "action", None) is not None:
            skipped += 1
            continue
        posts.append(serialize_post(message, rights))
    posts.sort(key=lambda post: post["id"])

    chat = describe_chat(entity)
    result: dict[str, Any] = {
        "chat": chat,
        "posts": posts,
        "fetched": len(raw),
        "skipped_service": skipped,
        "next_offset_id": next_offset_id,
        "partial": partial_error is not None,
    }
    if partial_error is not None:
        result["error"] = partial_error
    if missing is not None:
        result["missing_ids"] = missing
    logger.info(
        "channel_posts_read",
        tg_chat_id=chat["id"],
        fetched=len(raw),
        posts=len(posts),
        partial=partial_error is not None,
    )
    return result


async def _history_batch(client: Any, entity: Any, limit: int, offset_id: int) -> list[Any]:
    return [
        message async for message in client.iter_messages(entity, limit=limit, offset_id=offset_id)
    ]


async def _fetch_history(
    client: Any, entity: Any, limit: int, offset_id: int, budget: _FloodBudget
) -> tuple[list[Any], dict[str, Any] | None]:
    from telethon.errors import RPCError

    collected: list[Any] = []
    offset = offset_id
    while len(collected) < limit:
        want = min(READ_BATCH_SIZE, limit - len(collected))
        try:
            batch = await budget.run(
                partial(_history_batch, client, entity, want, offset), step="history"
            )
        except TelegramFloodWaitError as exc:
            if not collected:
                raise
            return collected, _partial(offset, exc, code=exc.code, seconds=exc.seconds)
        except RPCError as exc:
            if not collected:
                raise telegram_error(exc) from None
            return collected, _partial(offset, exc)
        except Exception as exc:  # noqa: BLE001 — битая пачка не прячет уже прочитанное
            if not collected:
                if _retries_exhausted(exc):
                    raise telegram_unavailable(exc) from None
                raise TelegramError(
                    f"Не удалось разобрать историю канала: {type(exc).__name__}: {str(exc)[:200]}"
                ) from None
            return collected, _partial(offset, exc)
        collected.extend(batch)
        if len(batch) < want:
            break
        offset = int(batch[-1].id)
    return collected, None


def _partial(offset_id: int, exc: BaseException, **extra: Any) -> dict[str, Any]:
    logger.warning("channel_posts_batch_failed", offset_id=offset_id, error_type=type(exc).__name__)
    return {"offset_id": offset_id, "detail": f"{type(exc).__name__}: {str(exc)[:200]}", **extra}


async def _fetch_by_ids(
    client: Any, entity: Any, ids: Sequence[int], budget: _FloodBudget
) -> tuple[list[Any], list[int]]:
    found: dict[int, Any] = {}
    for start in range(0, len(ids), READ_BATCH_SIZE):
        chunk = list(ids[start : start + READ_BATCH_SIZE])
        messages = await _telegram_call(
            budget, partial(client.get_messages, entity, ids=chunk), step="get_messages"
        )
        for message in messages or []:
            message_id = getattr(message, "id", None) if message is not None else None
            if isinstance(message_id, int):
                found[message_id] = message
    missing = [message_id for message_id in ids if message_id not in found]
    return [found[message_id] for message_id in ids if message_id in found], missing


async def _telegram_call[T](
    budget: _FloodBudget, call: Callable[[], Awaitable[T]], *, step: str, **log: Any
) -> T:
    """Запрос к Telegram с FloodWait-бюджетом и переводом ошибок в доменные."""
    from telethon.errors import RPCError

    try:
        return await budget.run(call, step=step, **log)
    except RPCError as exc:
        raise telegram_error(exc) from None
    except ValueError as exc:
        raise telegram_unavailable(exc) from None


# --- правка ------------------------------------------------------------------
async def edit_post(
    client: Any,
    request: EditRequest,
    *,
    sleep: Sleep = asyncio.sleep,
    now: Now = utcnow,
) -> dict[str, Any]:
    """Заменяет текст поста (у медиа-поста — подпись)."""
    from telethon.errors import MessageNotModifiedError, RPCError

    # Текстовый лимит проверяем до любых запросов: длиннее 4096 не бывает ни
    # текста, ни подписи.
    new_length = rendered_length(request.text, request.parse_mode)
    check_length(new_length, has_media=False)
    _check_not_expired(request, now)

    budget = _FloodBudget(sleep)
    with _flood_waits_surface(client):
        entity = await budget.run(
            partial(resolve_chat_entity, client, request.chat), step="resolve"
        )
        require_channel(entity)
        current = await _telegram_call(
            budget,
            partial(client.get_messages, entity, ids=request.message_id),
            step="get_message",
        )
        if current is None:
            raise ChannelPostError(
                "message_not_found",
                f"Поста {request.message_id} нет в этом чате (удалён или неверный id)",
                message_id=request.message_id,
            )
        if getattr(current, "action", None) is not None:
            raise ChannelPostError(
                "service_message",
                f"Сообщение {request.message_id} служебное — у него нет текста для правки",
                message_id=request.message_id,
            )

        kind = media_type(current)
        has_media = has_caption_media(kind)
        check_length(new_length, has_media=has_media)
        if new_length == 0 and not has_media:
            raise ChannelPostError("empty_text", "Telegram не принимает пустой текст поста")

        old_length = utf16_length(getattr(current, "message", None) or "")
        base: dict[str, Any] = {
            "message_id": request.message_id,
            "old_length": old_length,
            "new_length": new_length,
            "has_media": has_media,
            "media_type": kind,
        }
        log_fields = {
            "tg_chat_id": describe_chat(entity)["id"],
            "message_id": request.message_id,
            "old_length": old_length,
            "new_length": new_length,
        }

        async def apply_edit() -> Any:
            # Проверка срока — перед каждой попыткой: после FloodWait вызывающий
            # мог уже перестать ждать.
            _check_not_expired(request, now)
            return await client.edit_message(
                entity,
                request.message_id,
                request.text,
                parse_mode=telethon_parse_mode(request.parse_mode),
                link_preview=request.link_preview,
            )

        try:
            edited = await budget.run(apply_edit, step="edit", **log_fields)
        except MessageNotModifiedError:
            logger.info("channel_post_not_modified", **log_fields)
            return {**base, "edited": False, "no_change": True, "flood_waited": budget.waited}
        except RPCError as exc:
            raise telegram_error(exc) from None
        except ValueError as exc:
            raise telegram_unavailable(exc) from None

    logger.info("channel_post_edited", **log_fields)
    edit_date = getattr(edited, "edit_date", None)
    return {
        **base,
        "edited": True,
        "no_change": False,
        "flood_waited": budget.waited,
        "edit_date": edit_date.isoformat() if edit_date is not None else None,
    }


def _check_not_expired(request: EditRequest, now: Now) -> None:
    _check_deadline(
        request.expires_at,
        now,
        f"Команда правки поста {request.message_id} просрочена: воркер взял её позже, "
        "чем её ждали, — правка не применена",
        message_id=request.message_id,
    )


def _check_deadline(expires_at: float | None, now: Now, message: str, **details: Any) -> None:
    if expires_at is None or now().timestamp() <= expires_at:
        return
    raise ChannelPostError("command_expired", message, **details)


# --- удаление ----------------------------------------------------------------
async def delete_posts(
    client: Any,
    request: DeleteRequest,
    *,
    sleep: Sleep = asyncio.sleep,
    now: Now = utcnow,
) -> dict[str, Any]:
    """Удаляет посты канала у всех подписчиков.

    Сначала смотрит, какие из id есть в канале, удаляет только их и
    перечитывает: в ответе — что удалено (deleted), чего не было ещё до
    команды (already_missing) и что осталось, хотя удалить просили
    (not_deleted: например, у аккаунта нет права удалять чужие посты, а
    Telegram промолчал).
    """
    from telethon.errors import RPCError

    expired = (
        f"Команда удаления {len(request.ids)} постов просрочена: воркер взял её позже, "
        "чем её ждали, — ничего не удалено"
    )
    _check_deadline(request.expires_at, now, expired)

    budget = _FloodBudget(sleep)
    with _flood_waits_surface(client):
        entity = await budget.run(
            partial(resolve_chat_entity, client, request.chat), step="resolve"
        )
        require_channel(entity)
        found, already_missing = await _fetch_by_ids(client, entity, request.ids, budget)
        present = [int(message.id) for message in found]
        log_fields = {
            "tg_chat_id": describe_chat(entity)["id"],
            "requested": len(request.ids),
            "present": len(present),
        }

        remaining: list[int] = []
        if present:

            async def apply_delete() -> Any:
                # Как у правки: после FloodWait вызывающий мог уже не ждать.
                _check_deadline(request.expires_at, now, expired)
                return await client.delete_messages(entity, present, revoke=True)

            try:
                await budget.run(apply_delete, step="delete", **log_fields)
            except RPCError as exc:
                raise telegram_error(exc) from None
            except ValueError as exc:
                raise telegram_unavailable(exc) from None
            left, _ = await _fetch_by_ids(client, entity, present, budget)
            remaining = [int(message.id) for message in left]

    kept = set(remaining)
    deleted = [message_id for message_id in present if message_id not in kept]
    logger.info(
        "channel_posts_deleted",
        deleted=len(deleted),
        not_deleted=len(remaining),
        already_missing=len(already_missing),
        **log_fields,
    )
    return {
        "deleted": deleted,
        "already_missing": already_missing,
        "not_deleted": remaining,
        "flood_waited": budget.waited,
    }


# --- ошибки Telegram ---------------------------------------------------------
def _retries_exhausted(exc: BaseException) -> bool:
    # Telethon после request_retries неудач (внутренние ошибки Telegram)
    # бросает ValueError('Request was unsuccessful N time(s)').
    return isinstance(exc, ValueError) and str(exc).startswith("Request was unsuccessful")


def telegram_unavailable(exc: BaseException) -> AppError:
    """ValueError Telethon → доменная ошибка (он бросает их и на повторы, и на ввод)."""
    if _retries_exhausted(exc):
        return ChannelPostError(
            "telegram_unavailable",
            "Telegram несколько раз подряд ответил внутренней ошибкой — повторите позже",
            detail=str(exc)[:200],
        )
    return TelegramError(f"{type(exc).__name__}: {str(exc)[:200]}")


def telegram_error(exc: BaseException) -> AppError:
    """RPC-ошибка Telethon → доменная ошибка с понятным кодом."""
    from telethon import errors as tg

    table: tuple[tuple[type[BaseException], str, str], ...] = (
        (
            tg.ChatAdminRequiredError,
            "chat_admin_required",
            "У аккаунта нет нужных прав администратора в этом чате",
        ),
        (
            tg.MessageAuthorRequiredError,
            "message_author_required",
            "Telegram разрешает править это сообщение только автору; для постов "
            "канала нужно право администратора «Редактировать сообщения»",
        ),
        (tg.MessageIdInvalidError, "message_not_found", "Сообщения с таким id нет в этом чате"),
        (
            tg.MessageDeleteForbiddenError,
            "message_delete_forbidden",
            "Telegram не даёт удалить эти сообщения: нужно право администратора "
            "«Удалять сообщения»",
        ),
        (
            tg.MessageEditTimeExpiredError,
            "message_edit_time_expired",
            "Истёк срок, в который Telegram разрешает править это сообщение",
        ),
        (tg.MediaCaptionTooLongError, "caption_too_long", "Подпись к медиа слишком длинная"),
        (tg.MessageTooLongError, "text_too_long", "Текст слишком длинный"),
        (tg.MessageEmptyError, "empty_text", "Telegram не принимает пустой текст"),
        (tg.ChannelPrivateError, "channel_private", "Канал приватный или аккаунт в нём не состоит"),
        (tg.ChatWriteForbiddenError, "chat_write_forbidden", "Аккаунту запрещено писать в чат"),
        (tg.UsernameNotOccupiedError, "chat_not_found", "Такого @username не существует"),
        (tg.UsernameInvalidError, "chat_not_found", "Некорректный @username"),
        (tg.InviteHashExpiredError, "chat_not_found", "Ссылка-приглашение истекла"),
        (tg.InviteHashInvalidError, "chat_not_found", "Ссылка-приглашение недействительна"),
        (tg.ChannelInvalidError, "chat_not_found", "Канал не найден"),
        (tg.PeerIdInvalidError, "chat_not_found", "Чат с таким id не найден"),
    )
    for error_type, code, message in table:
        if isinstance(exc, error_type):
            return ChannelPostError(code, message, telegram=type(exc).__name__)
    if isinstance(exc, tg.FloodWaitError):
        return TelegramFloodWaitError(int(exc.seconds))
    return TelegramError(f"{type(exc).__name__}: {str(exc)[:200]}")
