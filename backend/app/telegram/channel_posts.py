"""Чтение и правка постов канала от имени аккаунта (не бота).

Задача владельца: в канале, где наш аккаунт — администратор, прочитать все
посты-шаблоны и потом заменить их тексты. Делает это живой Telethon-клиент
воркера: второе подключение той же сессии из другого процесса разлогинит
аккаунт (AuthKeyDuplicatedError), поэтому сюда приходят команды шины, а не
прямые вызовы из API или CLI.

Что здесь важно:
  - длина проверяется ДО запроса на правку — так, как её считает Telegram:
    после разбора разметки и в UTF-16 (эмодзи — две единицы). Лимит для
    подписи к медиа (1024) меньше, чем для текста (4096), поэтому перед
    правкой читаем сам пост и смотрим, есть ли у него медиа;
  - превью ссылки (MessageMediaWebPage) медиа не считается: это обычный
    текстовый пост, и лимит у него текстовый;
  - FloodWait до FLOOD_RETRY_MAX_SECONDS пережидаем и повторяем один раз,
    дольше — отдаём вызывающему с числом секунд: держать очередь команд
    воркера минутами нельзя;
  - MessageNotModified — не ошибка: текст уже такой, какой просили;
  - в лог уходят только id и длины, не тексты постов.
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import Any

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
FLOOD_RETRY_MAX_SECONDS = 30
PARSE_MODES: tuple[str, ...] = ("html", "md")
_MAX_MESSAGE_ID = 2**31 - 1

Sleep = Callable[[float], Awaitable[None]]


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
_USERNAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{3,31}$")
_INVITE_HASH_RE = re.compile(r"^[A-Za-z0-9_-]{8,}$")


def normalize_chat_reference(raw: Any) -> str | int:
    """Приводит то, чем человек обозначил чат, к виду, который понимает get_entity.

    Принимает: числовой id (голый или с -100), @username, username,
    ссылку t.me/<name>[/<пост>], t.me/c/<id>/<пост>, ссылку-приглашение
    t.me/+hash или t.me/joinchat/hash и сам хеш приглашения с «+» впереди.
    Хеш без «+» неотличим от username, если состоит только из букв и цифр, —
    поэтому его лучше передавать с «+».
    """
    if isinstance(raw, bool):
        raise InvalidInputError("Field 'chat' must be a chat reference, not a boolean")
    if isinstance(raw, int):
        return raw
    if not isinstance(raw, str) or not raw.strip():
        raise InvalidInputError("Field 'chat' is required")

    ref = raw.strip()
    if ref.lstrip("-").isdigit():
        return int(ref)

    match = _TME_RE.match(ref)
    if match:
        rest = match.group("rest").split("?", 1)[0].strip("/")
        parts = [part for part in rest.split("/") if part]
        if not parts:
            raise InvalidInputError(f"Cannot parse chat link: {ref}")
        head = parts[0]
        if head == "c" and len(parts) >= 2 and parts[1].isdigit():
            # Ссылка на пост приватного канала: t.me/c/<id>/<пост>.
            return int(f"-100{parts[1]}")
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


def _invite_link(invite_hash: str, original: str) -> str:
    invite_hash = invite_hash.strip("/")
    if not invite_hash:
        raise InvalidInputError(f"Invite link has no hash: {original}")
    return f"https://t.me/+{invite_hash}"


async def resolve_chat_entity(client: Any, reference: str | int) -> Any:
    """Находит чат по нормализованной ссылке.

    Сессия, собранная из tdata, приходит без кеша сущностей: по числовому id
    get_entity её не найдёт, пока аккаунт не «увидит» чат. Для числового id
    делаем одну запасную попытку через список диалогов — он заодно наполняет
    кеш access_hash.
    """
    from telethon.errors import RPCError

    try:
        return await client.get_entity(reference)
    except RPCError as exc:
        raise telegram_error(exc) from None
    except ValueError as exc:
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


async def _find_in_dialogs(client: Any, chat_id: int) -> Any | None:
    try:
        dialogs = await client.get_dialogs(limit=None)
    except Exception as exc:  # noqa: BLE001 — запасной путь, исходная ошибка важнее
        logger.warning("channel_dialogs_fallback_failed", detail=str(exc)[:200])
        return None
    for dialog in dialogs or []:
        entity = getattr(dialog, "entity", None)
        if getattr(dialog, "id", None) == chat_id:
            return entity
        # Голый id канала (без -100) совпадает с entity.id, а не с dialog.id.
        if chat_id > 0 and getattr(entity, "id", None) == chat_id:
            return entity
    return None


def describe_chat(entity: Any) -> dict[str, Any]:
    kind = "user"
    if getattr(entity, "broadcast", False):
        kind = "channel"
    elif getattr(entity, "megagroup", False):
        kind = "supergroup"
    elif getattr(entity, "title", None) is not None:
        kind = "group"
    return {
        "id": _int_or_none(getattr(entity, "id", None)),
        "title": getattr(entity, "title", None),
        "username": getattr(entity, "username", None),
        "type": kind,
    }


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


def parse_read_payload(payload: dict[str, Any]) -> ReadRequest:
    """READ_CHANNEL_POSTS: {chat, limit ≤ 500, offset_id = 0, ids?}.

    offset_id — как в Telegram: читаются посты с id < offset_id, 0 — с самого
    нового. ids — точечное чтение конкретных постов (для бэкапа перед правкой).
    """
    chat = normalize_chat_reference(payload.get("chat"))
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
    """EDIT_MESSAGE: {chat, message_id, text, parse_mode: html|md|None, link_preview=False}."""
    chat = normalize_chat_reference(payload.get("chat"))
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
    )


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


# --- длина текста ------------------------------------------------------------
def utf16_length(text: str) -> int:
    """Длина в единицах UTF-16 — в них Telegram меряет текст и сущности."""
    return len(text.encode("utf-16-le")) // 2


def render_text(text: str, parse_mode: str | None) -> tuple[str, int]:
    """Текст после разбора разметки и число сущностей — как их получит Telegram."""
    if parse_mode is None:
        return text, 0
    from telethon.extensions import html, markdown

    try:
        plain, entities = html.parse(text) if parse_mode == "html" else markdown.parse(text)
    except Exception as exc:  # noqa: BLE001 — любая ошибка разбора = плохой ввод
        raise InvalidInputError(f"Cannot parse text as {parse_mode}: {exc}") from None
    return str(plain), len(entities or [])


def rendered_length(text: str, parse_mode: str | None) -> int:
    """Длина текста так, как её увидит Telegram: разметка снята, счёт в UTF-16."""
    plain, _entities = render_text(text, parse_mode)
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


# Эти сущности Telegram расставляет сам (ссылки, @упоминания, #хэштеги) и
# расставит заново после правки простым текстом — это не форматирование автора.
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
    broadcast: bool
    creator: bool
    edit_messages: bool
    post_messages: bool
    known: bool


def chat_rights(entity: Any) -> ChatRights:
    rights = getattr(entity, "admin_rights", None)
    return ChatRights(
        broadcast=bool(getattr(entity, "broadcast", False)),
        creator=bool(getattr(entity, "creator", False)),
        edit_messages=bool(getattr(rights, "edit_messages", False)),
        post_messages=bool(getattr(rights, "post_messages", False)),
        # «min»-сущность приходит без прав: по ней ничего сказать нельзя.
        known=not bool(getattr(entity, "min", False)),
    )


def can_edit(message: Any, rights: ChatRights) -> bool | None:
    """Может ли аккаунт править пост. None — определить заранее нельзя."""
    if getattr(message, "action", None) is not None:
        return False
    if not rights.known:
        return None
    if rights.broadcast:
        if rights.creator or rights.edit_messages:
            return True
        return bool(rights.post_messages and getattr(message, "out", False))
    # В группах чужие сообщения не правит никто, свои — в пределах окна
    # Telegram, которое заранее не известно.
    return None if getattr(message, "out", False) else False


def serialize_post(message: Any, rights: ChatRights) -> dict[str, Any]:
    text = getattr(message, "message", None) or ""
    entities = list(getattr(message, "entities", None) or [])
    kind = media_type(message)
    date = getattr(message, "date", None)
    return {
        "id": int(message.id),
        "date": date.isoformat() if date is not None else None,
        "text": text,
        # Форматирование (жирный, ссылки) в сыром тексте теряется; HTML с
        # сущностями нужен, чтобы править такой пост с parse_mode=html.
        "html": _to_html(text, entities) if entities else None,
        "length": utf16_length(text),
        "entities_present": bool(entities),
        # Форматирование автора (жирный, ссылка под словом…), которое
        # пропадёт при правке простым текстом.
        "formatting_present": any(type(e).__name__ not in _AUTO_ENTITIES for e in entities),
        "has_media": has_caption_media(kind),
        "media_type": kind,
        "grouped_id": _int_or_none(getattr(message, "grouped_id", None)),
        "views": _int_or_none(getattr(message, "views", None)),
        "can_edit": can_edit(message, rights),
    }


def _to_html(text: str, entities: list[Any]) -> str | None:
    from telethon.extensions import html

    try:
        return str(html.unparse(text, entities))
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


# --- чтение ------------------------------------------------------------------
async def read_posts(client: Any, request: ReadRequest) -> dict[str, Any]:
    """Посты канала от старых к новым. Служебные сообщения пропускаются.

    next_offset_id — чем продолжить выгрузку вглубь истории; None — история
    кончилась. partial=True — очередная пачка не разобралась (новый TL-тип в
    Telethon), отдаём то, что успели прочитать, и причину.
    """
    entity = await resolve_chat_entity(client, request.chat)
    rights = chat_rights(entity)

    missing: list[int] | None = None
    partial: dict[str, Any] | None = None
    if request.ids is not None:
        raw, missing = await _fetch_by_ids(client, entity, request.ids)
        next_offset_id: int | None = None
    else:
        raw, partial = await _fetch_history(client, entity, request.limit, request.offset_id)
        exhausted = len(raw) < request.limit or partial is not None
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
        "partial": partial is not None,
    }
    if partial is not None:
        result["error"] = partial
    if missing is not None:
        result["missing_ids"] = missing
    logger.info(
        "channel_posts_read",
        tg_chat_id=chat["id"],
        fetched=len(raw),
        posts=len(posts),
        partial=partial is not None,
    )
    return result


async def _fetch_history(
    client: Any, entity: Any, limit: int, offset_id: int
) -> tuple[list[Any], dict[str, Any] | None]:
    from telethon.errors import RPCError

    collected: list[Any] = []
    offset = offset_id
    while len(collected) < limit:
        want = min(READ_BATCH_SIZE, limit - len(collected))
        batch: list[Any] = []
        try:
            async for message in client.iter_messages(entity, limit=want, offset_id=offset):
                batch.append(message)
        except RPCError as exc:
            if not collected:
                raise telegram_error(exc) from None
            return collected, _partial(offset, exc)
        except Exception as exc:  # noqa: BLE001 — битая пачка не прячет уже прочитанное
            if not collected:
                raise TelegramError(
                    f"Не удалось разобрать историю канала: {type(exc).__name__}: {str(exc)[:200]}"
                ) from None
            return collected, _partial(offset, exc)
        collected.extend(batch)
        if len(batch) < want:
            break
        offset = int(batch[-1].id)
    return collected, None


def _partial(offset_id: int, exc: BaseException) -> dict[str, Any]:
    logger.warning("channel_posts_batch_failed", offset_id=offset_id, error_type=type(exc).__name__)
    return {"offset_id": offset_id, "detail": f"{type(exc).__name__}: {str(exc)[:200]}"}


async def _fetch_by_ids(
    client: Any, entity: Any, ids: Sequence[int]
) -> tuple[list[Any], list[int]]:
    from telethon.errors import RPCError

    found: dict[int, Any] = {}
    for start in range(0, len(ids), READ_BATCH_SIZE):
        chunk = list(ids[start : start + READ_BATCH_SIZE])
        try:
            messages = await client.get_messages(entity, ids=chunk)
        except RPCError as exc:
            raise telegram_error(exc) from None
        for message in messages or []:
            message_id = getattr(message, "id", None) if message is not None else None
            if isinstance(message_id, int):
                found[message_id] = message
    missing = [message_id for message_id in ids if message_id not in found]
    return [found[message_id] for message_id in ids if message_id in found], missing


# --- правка ------------------------------------------------------------------
async def edit_post(
    client: Any, request: EditRequest, *, sleep: Sleep = asyncio.sleep
) -> dict[str, Any]:
    """Заменяет текст поста (у медиа-поста — подпись)."""
    from telethon.errors import FloodWaitError, MessageNotModifiedError, RPCError

    # Текстовый лимит проверяем до любых запросов: длиннее 4096 не бывает ни
    # текста, ни подписи.
    new_length = rendered_length(request.text, request.parse_mode)
    check_length(new_length, has_media=False)

    entity = await resolve_chat_entity(client, request.chat)
    try:
        current = await client.get_messages(entity, ids=request.message_id)
    except RPCError as exc:
        raise telegram_error(exc) from None
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
        "tg_chat_id": _int_or_none(getattr(entity, "id", None)),
        "message_id": request.message_id,
        "old_length": old_length,
        "new_length": new_length,
    }

    flood_waited = 0
    for attempt in (1, 2):
        try:
            edited = await client.edit_message(
                entity,
                request.message_id,
                request.text,
                # None явно: без аргумента Telethon применил бы markdown клиента.
                parse_mode=request.parse_mode,
                link_preview=request.link_preview,
            )
        except MessageNotModifiedError:
            logger.info("channel_post_not_modified", **log_fields)
            return {**base, "edited": False, "no_change": True, "flood_waited": flood_waited}
        except FloodWaitError as exc:
            seconds = int(exc.seconds)
            if attempt == 1 and seconds <= FLOOD_RETRY_MAX_SECONDS:
                logger.info("channel_edit_flood_wait", seconds=seconds, **log_fields)
                flood_waited += seconds
                await sleep(seconds + 1)
                continue
            logger.warning("channel_edit_flood_wait_exceeded", seconds=seconds, **log_fields)
            raise TelegramFloodWaitError(
                seconds, f"Telegram просит подождать {seconds} с перед следующей правкой"
            ) from None
        except RPCError as exc:
            raise telegram_error(exc) from None

        logger.info("channel_post_edited", **log_fields)
        edit_date = getattr(edited, "edit_date", None)
        return {
            **base,
            "edited": True,
            "no_change": False,
            "flood_waited": flood_waited,
            "edit_date": edit_date.isoformat() if edit_date is not None else None,
        }

    raise TelegramError("edit retries exhausted")  # pragma: no cover — цикл выше всегда выходит


# --- ошибки Telegram ---------------------------------------------------------
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
