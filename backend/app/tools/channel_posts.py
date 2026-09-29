"""CLI: прочитать посты канала, заменить их тексты или удалить лишние — от имени
нашего аккаунта.

Запуск — внутри контейнера api (там настройки, Redis и база):

    # выгрузить все посты (это же бэкап)
    python -m app.tools.channel_posts read --account "Основной" --chat @my_channel \\
        --out /tmp/posts.json

    # проверить план правок (dry-run: в канале ничего не меняется)
    python -m app.tools.channel_posts edit --account "Основной" --chat @my_channel \\
        --plan /tmp/plan.json

    # применить
    python -m app.tools.channel_posts edit --account "Основной" --chat @my_channel \\
        --plan /tmp/plan.json --apply

    # удалить посты: сначала проверка, потом --apply с числом постов из проверки
    python -m app.tools.channel_posts delete --account "Основной" --chat @my_channel \\
        --ids /tmp/delete.txt
    python -m app.tools.channel_posts delete --account "Основной" --chat @my_channel \\
        --ids /tmp/delete.txt --apply --expect 423

План — JSON-список {"id": <id поста>, "text": "<новый текст>"}. Необязательно:
"html" — новый текст в HTML (уходит с parse_mode=html вместо text),
"parse_mode" — html | md | none для text этого элемента (иначе --parse-mode),
"link_preview" — true/false для этого поста.

Файл из read и бэкап тоже годятся как план: у поста с форматированием уходит
поле html, у остальных — text как простой текст. Поэтому текст поста с
форматированием правят в поле html, а бэкап восстанавливается одной командой
(edit --plan channel_backup_<время>.json --apply). Посты, текст которых не
изменился, пропускаются — их форматирование не трогается.

Сам CLI в Telegram не ходит: сессия аккаунта живёт только в воркере, и второе
подключение той же сессии разлогинит аккаунт. Команды READ_CHANNEL_POSTS и
EDIT_MESSAGE уходят воркеру через шину — так же, как у API. Канал ищется по
--chat один раз, дальше команды адресуют его помеченным id (-100…): повторный
поиск по @username на каждую правку — прямой путь к FloodWait на часы.

Перед --apply CLI сам сохраняет свежий бэкап текущих текстов правимых постов
(channel_backup_<время>.json) и по итогу пишет результаты в JSON. Правки идут
по одной с паузой и останавливаются на первой ошибке, которую нет смысла
повторять. Повторяется только FloodWait не длиннее --max-flood-wait. Если
воркер не ответил на правку вовремя, команда повторно не отправляется: CLI
перечитывает пост и по нему решает, применилась ли правка.

Удаление необратимо, поэтому: без --apply только список того, что будет
удалено; с --apply обязателен --expect — число постов, которое показала
проверка (не совпало — ничего не удаляется); перед удалением пишется бэкап
текстов (channel_delete_backup_<время>.json; медиа по нему не вернуть).
Файл --ids — JSON-список id или текст вида «12 15 20-40».
"""

from __future__ import annotations

import argparse
import asyncio
import difflib
import json
import re
import sys
import tempfile
import uuid
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.bus.commands import CommandBus
from app.bus.messages import Command, CommandResult, CommandType
from app.core.clock import utcnow
from app.core.config import get_settings
from app.core.errors import AppError, CommandTimeoutError
from app.core.logging import configure_logging
from app.core.runtime import Runtime
from app.models import Account
from app.telegram.channel_posts import (
    READ_LIMIT_MAX,
    TEXT_LIMIT,
    entity_signature,
    length_limit,
    normalize_chat_reference,
    render_entities,
    utf16_length,
)
from app.workers.lease import AccountLease

Caller = Callable[[Command, float], Awaitable[CommandResult]]
Echo = Callable[[str], None]
Sleep = Callable[[float], Awaitable[None]]
Clock = Callable[[], datetime]
ChatRef = str | int

EXIT_OK = 0
EXIT_FAILED = 1
EXIT_INVALID = 2

# Чтение 500 постов — несколько запросов к Telegram.
READ_TIMEOUT_SECONDS = 120.0
# Воркер тратит на правку до трёх запросов к Telegram и не больше одного
# FloodWait ≤ 30 с. Начать правку позже EDIT_EXPIRY_SECONDS после отправки
# команды он откажется (expires_at), так что к таймауту CLI команда либо уже
# выполнена, либо не будет выполнена никогда. Исключение — запрос, застрявший
# в сети: его исход CLI узнаёт, перечитав пост (команды воркер выполняет по
# одной, поэтому чтение встанет в очередь после правки), а не повторной
# отправкой.
EDIT_EXPIRY_SECONDS = 60.0
EDIT_TIMEOUT_SECONDS = 120.0
DEFAULT_PAGE_SIZE = 100
# Telethon сам делает паузу 1 с между запросами при выгрузке больших историй:
# так Telegram реже отвечает FloodWait.
DEFAULT_PAGE_PAUSE_SECONDS = 1.0
DEFAULT_PAUSE_SECONDS = 4.0
DEFAULT_MAX_FLOOD_WAIT = 120
IDS_PER_REQUEST = 100
# Удаление пачки: чтение, удаление и перечитывание — три запроса к Telegram.
DELETE_EXPIRY_SECONDS = 60.0
DELETE_TIMEOUT_SECONDS = 120.0
DEFAULT_DELETE_BATCH = 50
DELETE_BATCH_MAX = 100
DEFAULT_DELETE_PAUSE_SECONDS = 3.0
MAX_DELETE_IDS = 5000
MAX_PAGES = 2000
_FRAGMENT = 30

STATUS_OK = "ok"
STATUS_NO_CHANGE = "no_change"
STATUS_ERROR = "error"
_STATUS_LABELS = {STATUS_OK: "OK", STATUS_NO_CHANGE: "БЕЗ ИЗМЕНЕНИЙ", STATUS_ERROR: "ОШИБКА"}
# После этих исходов правки дальше не идём.
_STOP_STATUSES = frozenset({"failed", "unknown"})
_CHAT_TYPES = {
    "channel": "канал",
    "supergroup": "супергруппа",
    "group": "группа",
    "user": "пользователь",
}


class CliError(Exception):
    """Ошибка ввода или окружения: печатается человеку как есть."""


class PlanError(CliError):
    pass


class IdsError(CliError):
    """Файл со списком id для удаления не разобран."""


class AccountLookupError(CliError):
    pass


class CommandFailedError(Exception):
    """Воркер выполнил команду с ошибкой."""

    def __init__(self, result: CommandResult) -> None:
        self.code = result.error_code or "unknown"
        self.message = result.error_message or ""
        self.data = dict(result.data)
        super().__init__(f"{self.code}: {self.message}")


# --- план правок -------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class PlanItem:
    """Элемент плана.

    html — новый текст в HTML: если он есть, уходит он (с parse_mode=html), а
    text служит только для сверки. parse_mode при mode_set=True — разметка
    text именно этого элемента, иначе берётся --parse-mode. link_preview —
    превью ссылки для этого поста; None — как задано опцией или как сейчас.
    """

    id: int
    text: str | None
    html: str | None = None
    parse_mode: str | None = None
    mode_set: bool = False
    link_preview: bool | None = None


_DOCUMENT_KINDS = frozenset({"channel_posts", "channel_backup"})
_PLAN_PARSE_MODES: dict[str, str | None] = {"html": "html", "md": "md", "none": None}


def load_plan(path: Path) -> list[PlanItem]:
    try:
        # utf-8-sig: Блокнот Windows любит дописывать BOM.
        raw = json.loads(path.read_text(encoding="utf-8-sig"))
    except FileNotFoundError:
        raise PlanError(f"Файл плана не найден: {path}") from None
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PlanError(f"План должен быть JSON в UTF-8: {exc}") from None
    return parse_plan(raw)


def parse_plan(raw: Any) -> list[PlanItem]:
    """Проверяет план: непустой список {id, text | html, parse_mode?, link_preview?}.

    id — уникальные положительные. В файле выгрузки или бэкапе text — ровно
    то, что сейчас в посте, то есть простой текст, а не разметка: поэтому для
    таких файлов parse_mode элементов по умолчанию — «без разметки».
    """
    document = isinstance(raw, dict) and raw.get("kind") in _DOCUMENT_KINDS
    items = raw.get("posts") if isinstance(raw, dict) else raw
    if not isinstance(items, list) or not items:
        raise PlanError('План должен быть непустым JSON-списком объектов {"id": ..., "text": ...}')

    plan: list[PlanItem] = []
    problems: list[str] = []
    seen: set[int] = set()
    for index, item in enumerate(items, start=1):
        where = f"элемент {index}"
        if not isinstance(item, dict):
            problems.append(f"{where}: ожидается объект {{id, text}}")
            continue
        post_id = item.get("id")
        if isinstance(post_id, bool) or not isinstance(post_id, int) or post_id <= 0:
            problems.append(f"{where}: id должен быть положительным целым, а не {post_id!r}")
            continue
        problem = _item_problem(item)
        if problem is not None:
            problems.append(f"{where} (id {post_id}): {problem}")
            continue
        if post_id in seen:
            problems.append(f"{where}: id {post_id} встречается в плане повторно")
            continue
        seen.add(post_id)
        plan.append(_plan_item(post_id, item, document=document))
    if problems:
        raise PlanError("\n".join(problems))
    return plan


def _item_problem(item: dict[str, Any]) -> str | None:
    text, html = item.get("text"), item.get("html")
    if text is not None and not isinstance(text, str):
        return "text должен быть строкой"
    if html is not None and not isinstance(html, str):
        return "html должен быть строкой"
    if text is None and html is None:
        return "text должен быть строкой (или задайте html)"
    if "parse_mode" in item:
        mode = item["parse_mode"]
        if mode is not None and not (isinstance(mode, str) and mode in _PLAN_PARSE_MODES):
            return "parse_mode должен быть html, md, none или null"
    preview = item.get("link_preview")
    if preview is not None and not isinstance(preview, bool):
        return "link_preview должен быть true или false"
    return None


def _plan_item(post_id: int, item: dict[str, Any], *, document: bool) -> PlanItem:
    mode_set = "parse_mode" in item
    mode = item.get("parse_mode")
    parse_mode = _PLAN_PARSE_MODES[mode] if isinstance(mode, str) else None
    return PlanItem(
        id=post_id,
        text=item.get("text"),
        html=item.get("html"),
        parse_mode=parse_mode,
        mode_set=mode_set or document,
        link_preview=item.get("link_preview"),
    )


# --- проверка правки ---------------------------------------------------------
@dataclass(slots=True)
class ItemCheck:
    id: int
    status: str
    new_length: int | None
    old_length: int | None = None
    limit: int | None = None
    media_type: str | None = None
    summary: str = ""
    problems: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    # Что и как уйдёт воркеру; expected_text — текст поста после правки.
    send_text: str = ""
    send_mode: str | None = None
    send_link_preview: bool = False
    expected_text: str = ""


def check_item(
    item: PlanItem,
    current: dict[str, Any] | None,
    parse_mode: str | None = None,
    link_preview: bool | None = None,
) -> ItemCheck:
    """Сверяет новый текст с текущим постом: лимиты, права, есть ли что менять."""
    send_text, send_mode, source_problems = _outgoing(item, current, parse_mode)
    try:
        new_plain, new_entities = render_entities(send_text, send_mode)
    except AppError as exc:
        return ItemCheck(item.id, STATUS_ERROR, None, problems=[*source_problems, exc.message])
    formatting = entity_signature(new_entities)
    new_length = utf16_length(new_plain)

    if current is None:
        problems = ["поста нет в канале (удалён, служебный или неверный id)", *source_problems]
        if new_length > TEXT_LIMIT:
            problems.append(f"текст {new_length} символов — больше лимита {TEXT_LIMIT}")
        return ItemCheck(item.id, STATUS_ERROR, new_length, problems=problems)

    has_media = bool(current.get("has_media"))
    limit = length_limit(has_media)
    old_text = str(current.get("text") or "")
    check = ItemCheck(
        item.id,
        STATUS_OK,
        new_length,
        old_length=int(current.get("length", utf16_length(old_text))),
        limit=limit,
        media_type=current.get("media_type"),
        send_text=send_text,
        send_mode=send_mode,
        send_link_preview=_effective_preview(item, current, link_preview),
        expected_text=new_plain,
    )
    check.problems.extend(source_problems)

    if new_length > limit:
        what = "подпись к медиа" if has_media else "текст"
        check.problems.append(f"{what} {new_length} символов — больше лимита {limit}")
    if new_length == 0 and not has_media:
        check.problems.append("пустой текст — Telegram его не примет")
    if current.get("can_edit") is False:
        check.problems.append(
            "аккаунт не может править этот пост (нужно право «Редактировать сообщения»)"
        )

    unchanged = _same_as_current(item, send_text, new_plain, formatting, current)
    if unchanged:
        check.summary = "текст тот же"
    else:
        check.warnings.extend(_warnings(item, formatting, current, check.send_link_preview))
        check.summary = (
            "текст тот же, меняется форматирование"
            if new_plain == old_text
            else diff_summary(old_text, new_plain)
        )

    if check.problems:
        check.status = STATUS_ERROR
    elif unchanged:
        check.status = STATUS_NO_CHANGE
    return check


def _outgoing(
    item: PlanItem, current: dict[str, Any] | None, default_mode: str | None
) -> tuple[str, str | None, list[str]]:
    """Текст и разметка, которые уйдут воркеру, и противоречия в элементе."""
    if item.html is not None:
        return item.html, "html", _html_conflicts(item, current)
    if item.mode_set:
        return item.text or "", item.parse_mode, []
    return item.text or "", default_mode, []


def _html_conflicts(item: PlanItem, current: dict[str, Any] | None) -> list[str]:
    """В элементе и text, и html: уходит html, поэтому правка одного text потерялась бы."""
    if item.text is None or item.html is None or current is None:
        return []
    if item.text == current.get("text"):
        return []
    if item.html == current.get("html"):
        return [
            "изменено поле text, а поле html — нет; у поста есть форматирование, и в "
            "канал уходит html: впишите новый текст в html или удалите поле html, "
            "чтобы отправить text без форматирования"
        ]
    try:
        plain, _entities = render_entities(item.html, "html")
    except AppError:
        return []  # ошибку разбора html покажет check_item
    if plain != item.text:
        return ["text и html расходятся — правьте что-то одно (в канал уходит html)"]
    return []


def _same_as_current(
    item: PlanItem,
    send_text: str,
    new_plain: str,
    formatting: list[tuple[str, int, int, str]],
    current: dict[str, Any],
) -> bool:
    if item.html is not None and send_text == current.get("html"):
        return True
    if new_plain != current.get("text"):
        return False
    if not formatting:
        # Текст тот же, а своего форматирования план не задаёт — пост не
        # трогаем, даже если в нём есть форматирование: правка простым
        # текстом его бы стёрла.
        return True
    return formatting == _current_formatting(current)


def _current_formatting(current: dict[str, Any]) -> list[tuple[str, int, int, str]] | None:
    html = current.get("html")
    if not html:
        return []
    try:
        _plain, entities = render_entities(str(html), "html")
    except AppError:
        return None
    return entity_signature(entities)


def _warnings(
    item: PlanItem,
    formatting: list[tuple[str, int, int, str]],
    current: dict[str, Any],
    link_preview: bool,
) -> list[str]:
    warnings: list[str] = []
    if not formatting and current.get("formatting_present"):
        warnings.append(
            "у поста есть форматирование — простым текстом оно пропадёт (чтобы "
            "сохранить, правьте поле html выгрузки или пишите HTML с --parse-mode html)"
        )
    if item.html is not None and current.get("html_lossless") is False:
        warnings.append(
            "HTML этого поста в выгрузке неточный — часть форматирования при правке пропадёт"
        )
    if current.get("media_type") == "webpage" and not link_preview:
        warnings.append("превью ссылки у поста будет удалено (link_preview выключен)")
    return warnings


def _effective_preview(item: PlanItem, current: dict[str, Any], option: bool | None) -> bool:
    if item.link_preview is not None:
        return item.link_preview
    if option is not None:
        return option
    # По умолчанию — как сейчас: превью есть, только если оно есть у поста.
    return current.get("media_type") == "webpage"


def diff_summary(old: str, new: str) -> str:
    """Однострочная сводка отличий: слова, похожесть, строки, первое расхождение."""
    if old == new:
        return "текст тот же"
    old_words, new_words = old.split(), new.split()
    matcher = difflib.SequenceMatcher(None, old_words, new_words, autojunk=False)
    removed = added = 0
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag in ("replace", "delete"):
            removed += i2 - i1
        if tag in ("replace", "insert"):
            added += j2 - j1
    lines = f"строк {_line_count(old)}→{_line_count(new)}"
    if removed == 0 and added == 0:
        return f"отличия только в пробелах и переносах, {lines}"
    words = f"слов −{removed} +{added}, похожесть {matcher.ratio():.0%}, {lines}"
    return f"{words} | {_first_change(old, new)}"


def _line_count(text: str) -> int:
    return text.count("\n") + 1 if text else 0


def _first_change(old: str, new: str) -> str:
    diff_at = next(
        (index for index, (a, b) in enumerate(zip(old, new, strict=False)) if a != b),
        min(len(old), len(new)),
    )
    # Начало слова, в котором расхождение: префикс до diff_at у обоих текстов
    # общий, поэтому граница одна и та же.
    start = max(old.rfind(" ", 0, diff_at), old.rfind("\n", 0, diff_at)) + 1
    return f"«{_fragment(old, start)}» → «{_fragment(new, start)}»"


def _fragment(text: str, start: int) -> str:
    piece = text[start : start + _FRAGMENT].replace("\n", "⏎")
    prefix = "…" if start > 0 else ""
    suffix = "…" if start + _FRAGMENT < len(text) else ""
    return f"{prefix}{piece}{suffix}"


def format_check(check: ItemCheck) -> list[str]:
    kind = check.media_type or "text"
    old = "?" if check.old_length is None else str(check.old_length)
    new = "?" if check.new_length is None else str(check.new_length)
    limit = "" if check.limit is None else f"/{check.limit}"
    label = _STATUS_LABELS[check.status]
    lines = [f"#{check.id:<8} {kind:<10} {old + '→' + new + limit:<17} {label:<14} {check.summary}"]
    lines.extend(f"    ошибка: {problem}" for problem in check.problems)
    lines.extend(f"    внимание: {warning}" for warning in check.warnings)
    return lines


# --- общение с воркером ------------------------------------------------------
async def call_ok(caller: Caller, command: Command, timeout_seconds: float) -> dict[str, Any]:
    result = await caller(command, timeout_seconds)
    if not result.ok:
        raise CommandFailedError(result)
    return dict(result.data)


def chat_target(chat_info: dict[str, Any] | None, fallback: ChatRef) -> ChatRef:
    """Чем адресовать канал в следующих командах.

    Воркер отдаёт помеченный id канала (-100…): по нему кеш сессии находит
    канал без запросов к Telegram, тогда как @username или ссылка-приглашение
    в каждой команде — это лишний ResolveUsername/CheckChatInvite.
    """
    chat_id = (chat_info or {}).get("id")
    if isinstance(chat_id, int) and not isinstance(chat_id, bool) and chat_id < 0:
        return chat_id
    return fallback


async def read_by_ids(
    caller: Caller, account_id: uuid.UUID, chat: ChatRef, ids: Sequence[int]
) -> dict[str, Any]:
    """Текущие версии конкретных постов — для проверки плана и бэкапа."""
    posts: list[dict[str, Any]] = []
    missing: list[int] = []
    chat_info: dict[str, Any] | None = None
    target = chat
    for start in range(0, len(ids), IDS_PER_REQUEST):
        chunk = list(ids[start : start + IDS_PER_REQUEST])
        data = await call_ok(
            caller,
            Command(
                type=CommandType.READ_CHANNEL_POSTS,
                account_id=account_id,
                payload={"chat": target, "ids": chunk},
            ),
            READ_TIMEOUT_SECONDS,
        )
        chat_info = data.get("chat") or chat_info
        target = chat_target(chat_info, target)
        posts.extend(data.get("posts") or [])
        missing.extend(data.get("missing_ids") or [])
    posts.sort(key=lambda post: int(post["id"]))
    return {"chat": chat_info, "target": target, "posts": posts, "missing_ids": missing}


def write_json(path: Path, document: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        json.dump(document, handle, ensure_ascii=False, indent=2)
        handle.write("\n")


def _stamp(moment: datetime) -> str:
    return moment.strftime("%Y%m%d_%H%M%S")


def _flood_seconds(code: str | None, data: dict[str, Any], message: str | None) -> int | None:
    if code != "telegram_flood_wait":
        return None
    seconds = data.get("seconds")
    if isinstance(seconds, int) and not isinstance(seconds, bool):
        return seconds
    match = re.search(r"(\d+)", message or "")
    return int(match.group(1)) if match else None


# --- read --------------------------------------------------------------------
async def run_read(
    caller: Caller,
    *,
    account_id: uuid.UUID,
    chat: str,
    out_path: Path,
    limit: int | None = None,
    page_size: int = DEFAULT_PAGE_SIZE,
    page_pause: float = DEFAULT_PAGE_PAUSE_SECONDS,
    max_flood_wait: int = DEFAULT_MAX_FLOOD_WAIT,
    echo: Echo = print,
    sleep: Sleep = asyncio.sleep,
    now: Clock = utcnow,
) -> int:
    """Выгружает посты (последние limit или все) от старых к новым в JSON.

    Сбой посреди выгрузки не теряет уже прочитанное: файл пишется с
    partial=true и причиной.
    """
    collected: dict[int, dict[str, Any]] = {}
    chat_info: dict[str, Any] | None = None
    target: ChatRef = chat
    skipped = 0
    offset = 0
    partial: dict[str, Any] | None = None

    for page in range(MAX_PAGES):
        want = page_size if limit is None else min(page_size, limit - len(collected))
        if want <= 0:
            break
        if page and page_pause > 0:
            await sleep(page_pause)
        try:
            data = await _read_page(
                caller, account_id, target, want, offset, max_flood_wait, echo, sleep
            )
        except (CommandFailedError, AppError) as exc:
            partial = {"offset_id": offset, "code": exc.code, "detail": exc.message}
            break
        chat_info = data.get("chat") or chat_info
        target = chat_target(chat_info, target)
        skipped += int(data.get("skipped_service") or 0)
        for post in data.get("posts") or []:
            collected[int(post["id"])] = post
        if data.get("partial"):
            partial = data.get("error") or {"offset_id": offset}
            break
        next_offset = data.get("next_offset_id")
        # Защита от зацикливания: курсор обязан уходить вглубь истории.
        if next_offset is None or (offset and int(next_offset) >= offset):
            break
        offset = int(next_offset)

    if partial is not None and not collected:
        echo(f"Ошибка: ничего не прочитано — {_partial_reason(partial)}")
        return EXIT_FAILED

    posts = sorted(collected.values(), key=lambda post: int(post["id"]))
    if limit is not None and len(posts) > limit:
        posts = posts[-limit:]

    document: dict[str, Any] = {
        "kind": "channel_posts",
        "account_id": str(account_id),
        "chat": chat,
        "chat_info": chat_info,
        "fetched_at": now().isoformat(),
        "count": len(posts),
        "skipped_service": skipped,
        "partial": partial is not None,
        "posts": posts,
    }
    if partial is not None:
        document["error"] = partial
    write_json(out_path, document)

    echo(f"Чат: {_chat_title(chat_info, chat)}")
    for post in posts:
        echo(_post_line(post))
    span = f" (id {posts[0]['id']}…{posts[-1]['id']})" if posts else ""
    echo(f"Прочитано постов: {len(posts)}{span}, служебных пропущено: {skipped}")
    echo(f"Сохранено: {out_path}")
    if partial is not None:
        echo(
            "ВНИМАНИЕ: выгрузка неполная — очередная пачка (старше id "
            f"{partial.get('offset_id')}) не прочиталась: {_partial_reason(partial)}"
        )
        return EXIT_FAILED
    return EXIT_OK


async def _read_page(
    caller: Caller,
    account_id: uuid.UUID,
    target: ChatRef,
    want: int,
    offset: int,
    max_flood_wait: int,
    echo: Echo,
    sleep: Sleep,
) -> dict[str, Any]:
    """Одна страница выгрузки; FloodWait не длиннее max_flood_wait пережидаем один раз."""
    payload = {"chat": target, "limit": want, "offset_id": offset}
    for attempt in (1, 2):
        command = Command(
            type=CommandType.READ_CHANNEL_POSTS, account_id=account_id, payload=payload
        )
        try:
            return await call_ok(caller, command, READ_TIMEOUT_SECONDS)
        except CommandFailedError as exc:
            seconds = _flood_seconds(exc.code, exc.data, exc.message)
            if attempt == 2 or seconds is None or seconds > max_flood_wait:
                raise
            echo(f"Telegram просит подождать {seconds} с — жду и продолжаю выгрузку")
            await sleep(seconds + 1)
    raise AssertionError("unreachable")  # pragma: no cover — цикл выше всегда выходит


def _partial_reason(partial: dict[str, Any]) -> str:
    code = partial.get("code")
    detail = partial.get("detail", "")
    return f"{code}: {detail}" if code else str(detail)


def _chat_title(chat_info: dict[str, Any] | None, chat: ChatRef) -> str:
    if not chat_info:
        return str(chat)
    details: list[str] = []
    if chat_info.get("username"):
        details.append(f"@{chat_info['username']}")
    kind = chat_info.get("type")
    if kind:
        details.append(_CHAT_TYPES.get(str(kind), str(kind)))
    details.append(f"id {chat_info.get('id')}")
    return f"{chat_info.get('title') or chat} ({', '.join(details)})"


def _post_line(post: dict[str, Any]) -> str:
    date = str(post.get("date") or "")[:16].replace("T", " ")
    kind = post.get("media_type") or "text"
    flags = []
    if post.get("formatting_present"):
        flags.append("формат")
    if post.get("html_lossless") is False:
        flags.append("html-неточный")
    if post.get("grouped_id"):
        flags.append("альбом")
    if post.get("can_edit") is False:
        flags.append("нельзя править")
    length = f"{post.get('length', 0):>5} симв."
    return f"#{post['id']:<8} {date:<16} {kind:<10} {length} {' '.join(flags)}".rstrip()


# --- edit --------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class EditOptions:
    parse_mode: str | None = None
    # None — превью как сейчас у каждого поста; True/False — для всех.
    link_preview: bool | None = None
    apply: bool = False
    pause_seconds: float = DEFAULT_PAUSE_SECONDS
    max_flood_wait: int = DEFAULT_MAX_FLOOD_WAIT
    backup_dir: Path = field(default_factory=lambda: Path(tempfile.gettempdir()))
    results_path: Path | None = None


async def run_edit(
    caller: Caller,
    *,
    account_id: uuid.UUID,
    chat: str,
    plan: list[PlanItem],
    options: EditOptions,
    echo: Echo = print,
    sleep: Sleep = asyncio.sleep,
    now: Clock = utcnow,
) -> int:
    """Dry-run по умолчанию; с options.apply — бэкап, затем правки по одной."""
    ids = [item.id for item in plan]
    current = await read_by_ids(caller, account_id, chat, ids)
    target: ChatRef = current["target"]
    by_id = {int(post["id"]): post for post in current["posts"]}
    checks = [
        check_item(item, by_id.get(item.id), options.parse_mode, options.link_preview)
        for item in plan
    ]

    mode = "ПРАВКА (--apply)" if options.apply else "ПРОВЕРКА (dry-run)"
    echo(f"Чат: {_chat_title(current['chat'], chat)} — {mode}")
    echo(
        f"parse_mode: {options.parse_mode or 'нет (простой текст)'} "
        "(элементы с html уходят как html), "
        f"превью ссылок: {_preview_label(options.link_preview)}"
    )
    for check in checks:
        for line in format_check(check):
            echo(line)

    to_edit = sum(check.status == STATUS_OK for check in checks)
    unchanged = sum(check.status == STATUS_NO_CHANGE for check in checks)
    invalid = sum(check.status == STATUS_ERROR for check in checks)
    echo(f"Итого: к правке {to_edit}, без изменений {unchanged}, с ошибками {invalid}")

    started = now()
    stamp = _stamp(started)
    if not options.apply:
        if options.results_path is not None:
            write_json(
                options.results_path,
                _report(
                    account_id,
                    chat,
                    current["chat"],
                    started,
                    now(),
                    False,
                    None,
                    [
                        _row(item, check, "dry_run_" + check.status)
                        for item, check in zip(plan, checks, strict=True)
                    ],
                ),
            )
        echo("Это проверка: в канале ничего не изменено. Чтобы применить — добавьте --apply.")
        return EXIT_INVALID if invalid else EXIT_OK

    if invalid:
        echo("Есть ошибки — ничего не правлю. Исправьте план и запустите снова.")
        return EXIT_INVALID
    if to_edit == 0:
        echo("Править нечего: все тексты уже такие, как в плане.")
        return EXIT_OK

    backup_path = options.backup_dir / f"channel_backup_{stamp}.json"
    write_json(
        backup_path,
        {
            "kind": "channel_backup",
            "account_id": str(account_id),
            "chat": chat,
            "chat_info": current["chat"],
            "saved_at": started.isoformat(),
            "posts": [by_id[post_id] for post_id in ids if post_id in by_id],
        },
    )
    echo(f"Бэкап текущих текстов: {backup_path}")
    echo(f"Откат: edit --plan {backup_path} --apply")

    results_path = options.results_path or (
        options.backup_dir / f"channel_edit_results_{stamp}.json"
    )
    rows = {
        item.id: _row(item, check, _initial_status(check))
        for item, check in zip(plan, checks, strict=True)
    }
    stopped: dict[str, Any] | None = None
    edits_done = 0
    try:
        for item, check in zip(plan, checks, strict=True):
            if check.status != STATUS_OK:
                continue
            if edits_done:
                await sleep(options.pause_seconds)
            # Если прервут посреди запроса, в результатах будет честное
            # «неизвестно», а не «не трогали».
            rows[item.id] = {**rows[item.id], "status": "in_flight"}
            row = await _edit_one(
                caller, account_id, target, item, check, rows[item.id], options, echo, sleep, now
            )
            edits_done += 1
            rows[item.id] = row
            echo(_progress_line(row, edits_done, to_edit))
            if row["status"] in _STOP_STATUSES:
                stopped = row
                break
    finally:
        # Файл результатов пишется при любом исходе, в том числе при Ctrl+C:
        # по нему видно, что уже изменено, а что — нет.
        ordered = [rows[post_id] for post_id in ids]
        write_json(
            results_path,
            _report(account_id, chat, current["chat"], started, now(), True, backup_path, ordered),
        )
        summary = _count_statuses(ordered)
        echo("Результат: " + ", ".join(f"{key} {value}" for key, value in sorted(summary.items())))
        echo(f"Результаты: {results_path}")

    if stopped is not None:
        if stopped["status"] == "unknown":
            echo(
                f"Остановлено на посте #{stopped['id']}: исход правки неизвестен — "
                f"{stopped.get('error_message')}. Следующие посты не тронуты."
            )
        else:
            echo(
                f"Остановлено на посте #{stopped['id']}: {stopped.get('error_code')}: "
                f"{stopped.get('error_message')}. Остальные не тронуты."
            )
        return EXIT_FAILED
    return EXIT_OK


def _preview_label(option: bool | None) -> str:
    if option is None:
        return "как сейчас у поста"
    return "да" if option else "нет"


async def _edit_one(
    caller: Caller,
    account_id: uuid.UUID,
    target: ChatRef,
    item: PlanItem,
    check: ItemCheck,
    row: dict[str, Any],
    options: EditOptions,
    echo: Echo,
    sleep: Sleep,
    now: Clock,
) -> dict[str, Any]:
    waited = 0
    for attempt in (1, 2):
        payload = {
            "chat": target,
            "message_id": item.id,
            "text": check.send_text,
            "parse_mode": check.send_mode,
            "link_preview": check.send_link_preview,
            # Позже этого воркер правку не начнёт: CLI к тому времени может
            # перестать ждать, и «тихая» правка потом исказила бы результаты.
            "expires_at": now().timestamp() + EDIT_EXPIRY_SECONDS,
        }
        command = Command(type=CommandType.EDIT_MESSAGE, account_id=account_id, payload=payload)
        try:
            result = await caller(command, EDIT_TIMEOUT_SECONDS)
        except CommandTimeoutError:
            echo(
                f"    #{item.id}: воркер не ответил за {EDIT_TIMEOUT_SECONDS:.0f} с — "
                "перечитываю пост; повторно правку не отправляю"
            )
            return await _verify_after_timeout(
                caller, account_id, target, item, check, {**row, "flood_waited": waited}, echo
            )
        except AppError as exc:
            return _failed(row, exc.code, exc.message, waited)

        if result.ok:
            no_change = bool(result.data.get("no_change"))
            waited += int(result.data.get("flood_waited") or 0)
            status = "no_change" if no_change else "edited"
            return {**row, "status": status, "flood_waited": waited}

        seconds = _flood_seconds(result.error_code, result.data, result.error_message)
        if attempt == 1 and seconds is not None and seconds <= options.max_flood_wait:
            echo(f"    #{item.id}: Telegram просит подождать {seconds} с — жду и повторяю")
            waited += seconds
            await sleep(seconds + 1)
            continue
        failed = _failed(row, result.error_code or "unknown", result.error_message or "", waited)
        if seconds is not None:
            failed["retry_after"] = seconds
        return failed
    return _failed(row, "retries_exhausted", "Повторы исчерпаны", waited)  # pragma: no cover


async def _verify_after_timeout(
    caller: Caller,
    account_id: uuid.UUID,
    target: ChatRef,
    item: PlanItem,
    check: ItemCheck,
    row: dict[str, Any],
    echo: Echo,
) -> dict[str, Any]:
    """Правка без ответа: применилась ли она, узнаём по самому посту."""
    command = Command(
        type=CommandType.READ_CHANNEL_POSTS,
        account_id=account_id,
        payload={"chat": target, "ids": [item.id]},
    )
    try:
        data = await call_ok(caller, command, READ_TIMEOUT_SECONDS)
    except (CommandFailedError, AppError) as exc:
        return _unknown(
            row,
            "воркер не ответил на правку, а перечитать пост не удалось "
            f"({exc.code}) — правка могла примениться, проверьте пост",
        )
    posts = [post for post in data.get("posts") or [] if post.get("id") == item.id]
    if posts and posts[0].get("text") == check.expected_text:
        echo(f"    #{item.id}: в канале уже новый текст — правка применена")
        return {**row, "status": "edited", "verified_after_timeout": True}
    return _unknown(
        row,
        "воркер не ответил на правку, и в канале прежний текст — скорее всего, правка "
        "не применена (просроченную команду воркер отклонит); проверьте пост и "
        "запустите план снова",
    )


def _initial_status(check: ItemCheck) -> str:
    return "skipped_unchanged" if check.status == STATUS_NO_CHANGE else "not_attempted"


def _row(item: PlanItem, check: ItemCheck, status: str) -> dict[str, Any]:
    return {
        "id": item.id,
        "status": status,
        "old_length": check.old_length,
        "new_length": check.new_length,
        "media_type": check.media_type,
        "parse_mode": check.send_mode,
        "problems": list(check.problems),
        "warnings": list(check.warnings),
    }


def _failed(row: dict[str, Any], code: str, message: str, waited: int) -> dict[str, Any]:
    return {
        **row,
        "status": "failed",
        "error_code": code,
        "error_message": message,
        "flood_waited": waited,
    }


def _unknown(row: dict[str, Any], message: str) -> dict[str, Any]:
    return {**row, "status": "unknown", "error_code": "edit_unconfirmed", "error_message": message}


def _progress_line(row: dict[str, Any], done: int, total: int) -> str:
    status = row["status"]
    if status == "failed":
        return f"[{done}/{total}] #{row['id']}: ОШИБКА {row.get('error_code')}"
    if status == "unknown":
        return f"[{done}/{total}] #{row['id']}: НЕИЗВЕСТНО (воркер не ответил)"
    label = "изменён" if status == "edited" else "уже был таким"
    return f"[{done}/{total}] #{row['id']}: {label} ({row['old_length']}→{row['new_length']})"


def _count_statuses(rows: list[dict[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for row in rows:
        counts[row["status"]] = counts.get(row["status"], 0) + 1
    return counts


def _report(
    account_id: uuid.UUID,
    chat: str,
    chat_info: dict[str, Any] | None,
    started: datetime,
    finished: datetime,
    applied: bool,
    backup_path: Path | None,
    rows: list[dict[str, Any]],
) -> dict[str, Any]:
    return {
        "kind": "channel_edit_results",
        "account_id": str(account_id),
        "chat": chat,
        "chat_info": chat_info,
        "applied": applied,
        "started_at": started.isoformat(),
        "finished_at": finished.isoformat(),
        "backup": str(backup_path) if backup_path is not None else None,
        "summary": _count_statuses(rows),
        "items": rows,
    }


# --- delete ------------------------------------------------------------------
_ID_TOKEN_RE = re.compile(r"^(\d+)(?:-(\d+))?$")


def load_ids(path: Path) -> list[int]:
    try:
        raw = path.read_text(encoding="utf-8-sig")
    except OSError as exc:
        raise IdsError(f"Не удалось прочитать {path}: {exc}") from None
    return parse_ids(raw)


def parse_ids(raw: str) -> list[int]:
    """JSON-список id или текст «12 15 20-40» (запятые и переводы строк тоже)."""
    text = raw.strip()
    if not text:
        raise IdsError("Список id пуст")
    ids: list[int] = []
    if text.startswith("["):
        try:
            data = json.loads(text)
        except json.JSONDecodeError as exc:
            raise IdsError(f"Некорректный JSON: {exc}") from None
        for value in data:
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise IdsError(f"В JSON-списке не id поста: {value!r}")
            ids.append(value)
    else:
        for token in re.split(r"[\s,;]+", text):
            match = _ID_TOKEN_RE.match(token)
            if not match:
                raise IdsError(f"Не понял «{token}»: нужен id или диапазон вида 20-40")
            low = int(match.group(1))
            high = int(match.group(2) or low)
            if low <= 0 or high < low:
                raise IdsError(f"Некорректный диапазон «{token}»")
            if high - low >= MAX_DELETE_IDS:
                raise IdsError(f"Диапазон «{token}» длиннее {MAX_DELETE_IDS} id")
            ids.extend(range(low, high + 1))
    unique = sorted(set(ids))
    if not unique:
        raise IdsError("Список id пуст")
    if len(unique) > MAX_DELETE_IDS:
        raise IdsError(f"За один запуск удаляется не больше {MAX_DELETE_IDS} id")
    return unique


@dataclass(frozen=True, slots=True)
class DeleteOptions:
    apply: bool = False
    # Сколько постов ожидается к удалению: сверяется с каналом перед --apply.
    expect: int | None = None
    batch_size: int = DEFAULT_DELETE_BATCH
    pause_seconds: float = DEFAULT_DELETE_PAUSE_SECONDS
    max_flood_wait: int = DEFAULT_MAX_FLOOD_WAIT
    backup_dir: Path = field(default_factory=lambda: Path(tempfile.gettempdir()))
    results_path: Path | None = None


async def run_delete(
    caller: Caller,
    *,
    account_id: uuid.UUID,
    chat: str,
    ids: list[int],
    options: DeleteOptions,
    echo: Echo = print,
    sleep: Sleep = asyncio.sleep,
    now: Clock = utcnow,
) -> int:
    """Dry-run по умолчанию; с options.apply — сверка --expect, бэкап, удаление пачками."""
    current = await read_by_ids(caller, account_id, chat, ids)
    target: ChatRef = current["target"]
    posts: list[dict[str, Any]] = current["posts"]
    existing = [int(post["id"]) for post in posts]
    missing = sorted(int(post_id) for post_id in current["missing_ids"])

    mode = "УДАЛЕНИЕ (--apply)" if options.apply else "ПРОВЕРКА (dry-run)"
    echo(f"Чат: {_chat_title(current['chat'], chat)} — {mode}")
    for post in posts:
        echo(f"{_post_line(post)}  {_snippet(post.get('text') or '')}".rstrip())
    echo(f"Итого: к удалению {len(existing)}, уже нет в канале {len(missing)}")

    started = now()
    stamp = _stamp(started)
    rows: dict[int, dict[str, Any]] = {
        post_id: {"id": post_id, "status": "missing"} for post_id in missing
    }
    for post in posts:
        rows[int(post["id"])] = {
            "id": int(post["id"]),
            "status": "not_attempted",
            "media_type": post.get("media_type"),
            "length": post.get("length"),
        }

    if not options.apply:
        if options.results_path is not None:
            dry = [{**rows[i], "status": "dry_run_" + rows[i]["status"]} for i in sorted(rows)]
            write_json(
                options.results_path,
                _delete_report(account_id, chat, current["chat"], started, now(), False, None, dry),
            )
        echo(
            "Это проверка: в канале ничего не удалено. Чтобы удалить — добавьте "
            f"--apply --expect {len(existing)}."
        )
        return EXIT_OK

    if not existing:
        echo("Удалять нечего: этих постов в канале уже нет.")
        return EXIT_OK
    if options.expect != len(existing):
        echo(
            f"--expect {options.expect} не совпадает с числом постов к удалению "
            f"({len(existing)}) — ничего не удаляю. Проверьте список и запустите снова."
        )
        return EXIT_INVALID

    backup_path = options.backup_dir / f"channel_delete_backup_{stamp}.json"
    write_json(
        backup_path,
        {
            "kind": "channel_delete_backup",
            "account_id": str(account_id),
            "chat": chat,
            "chat_info": current["chat"],
            "saved_at": started.isoformat(),
            "posts": posts,
        },
    )
    echo(f"Бэкап удаляемых постов: {backup_path} (тексты и даты; медиа по нему не вернуть)")

    results_path = options.results_path or (
        options.backup_dir / f"channel_delete_results_{stamp}.json"
    )
    batches = [
        existing[start : start + options.batch_size]
        for start in range(0, len(existing), options.batch_size)
    ]
    stopped: dict[str, Any] | None = None
    deleted_total = 0
    try:
        for number, batch in enumerate(batches, start=1):
            if number > 1:
                await sleep(options.pause_seconds)
            for post_id in batch:
                rows[post_id] = {**rows[post_id], "status": "in_flight"}
            outcome = await _delete_batch(
                caller, account_id, target, batch, options, echo, sleep, now
            )
            gone = set(outcome.get("deleted") or [])
            for post_id in batch:
                if post_id in gone:
                    status = "deleted"
                elif outcome["status"] in ("ok", "partial"):
                    status = "not_deleted"
                else:
                    status = outcome["status"]
                rows[post_id] = {**rows[post_id], "status": status}
                for key in ("error_code", "error_message", "retry_after"):
                    if key in outcome and status not in ("deleted", "not_deleted"):
                        rows[post_id][key] = outcome[key]
            deleted_total += len(gone)
            echo(_delete_progress(number, len(batches), batch, outcome, deleted_total))
            if outcome["status"] != "ok":
                stopped = {**outcome, "batch": number}
                break
    finally:
        ordered = [rows[post_id] for post_id in sorted(rows)]
        write_json(
            results_path,
            _delete_report(
                account_id, chat, current["chat"], started, now(), True, backup_path, ordered
            ),
        )
        summary = _count_statuses(ordered)
        echo("Результат: " + ", ".join(f"{key} {value}" for key, value in sorted(summary.items())))
        echo(f"Результаты: {results_path}")

    if stopped is None:
        return EXIT_OK
    if stopped["status"] == "partial":
        echo(
            f"Остановлено на пачке {stopped['batch']}: Telegram не удалил "
            f"{len(stopped['not_deleted'])} постов (например, #{stopped['not_deleted'][0]}) — "
            "проверьте права аккаунта «Удалять сообщения». Следующие пачки не тронуты."
        )
    elif stopped["status"] == "unknown":
        echo(
            f"Остановлено на пачке {stopped['batch']}: исход неизвестен — "
            f"{stopped.get('error_message')}. Следующие пачки не тронуты."
        )
    else:
        echo(
            f"Остановлено на пачке {stopped['batch']}: {stopped.get('error_code')}: "
            f"{stopped.get('error_message')}. Следующие пачки не тронуты."
        )
    return EXIT_FAILED


async def _delete_batch(
    caller: Caller,
    account_id: uuid.UUID,
    target: ChatRef,
    batch: list[int],
    options: DeleteOptions,
    echo: Echo,
    sleep: Sleep,
    now: Clock,
) -> dict[str, Any]:
    """Одна пачка. ok — удалены все; partial — Telegram часть оставил."""
    waited = 0
    for attempt in (1, 2):
        payload = {
            "chat": target,
            "ids": batch,
            "expires_at": now().timestamp() + DELETE_EXPIRY_SECONDS,
        }
        command = Command(type=CommandType.DELETE_MESSAGES, account_id=account_id, payload=payload)
        try:
            result = await caller(command, DELETE_TIMEOUT_SECONDS)
        except CommandTimeoutError:
            echo(
                f"    воркер не ответил за {DELETE_TIMEOUT_SECONDS:.0f} с — "
                "перечитываю пачку; повторно удаление не отправляю"
            )
            return await _verify_deleted(caller, account_id, target, batch, waited)
        except AppError as exc:
            return _batch_failed(exc.code, exc.message, waited)

        if result.ok:
            waited += int(result.data.get("flood_waited") or 0)
            # already_missing — посты, что были при проверке, но исчезли до
            # команды: их удалил прошлый запуск или человек. Для нас — удалены.
            gone = set(result.data.get("deleted") or []) | set(
                result.data.get("already_missing") or []
            )
            left = [post_id for post_id in batch if post_id not in gone]
            return {
                "status": "partial" if left else "ok",
                "deleted": [post_id for post_id in batch if post_id in gone],
                "not_deleted": left,
                "flood_waited": waited,
            }

        seconds = _flood_seconds(result.error_code, result.data, result.error_message)
        if attempt == 1 and seconds is not None and seconds <= options.max_flood_wait:
            echo(f"    Telegram просит подождать {seconds} с — жду и повторяю пачку")
            waited += seconds
            await sleep(seconds + 1)
            continue
        failed = _batch_failed(result.error_code or "unknown", result.error_message or "", waited)
        if seconds is not None:
            failed["retry_after"] = seconds
        return failed
    return _batch_failed("retries_exhausted", "Повторы исчерпаны", waited)  # pragma: no cover


async def _verify_deleted(
    caller: Caller,
    account_id: uuid.UUID,
    target: ChatRef,
    batch: list[int],
    waited: int,
) -> dict[str, Any]:
    """Удаление без ответа: что исчезло, узнаём, перечитав пачку."""
    command = Command(
        type=CommandType.READ_CHANNEL_POSTS,
        account_id=account_id,
        payload={"chat": target, "ids": batch},
    )
    try:
        data = await call_ok(caller, command, READ_TIMEOUT_SECONDS)
    except (CommandFailedError, AppError) as exc:
        return {
            "status": "unknown",
            "error_code": "delete_unconfirmed",
            "error_message": "воркер не ответил на удаление, а перечитать пачку не удалось "
            f"({exc.code}) — посты могли удалиться, запустите проверку снова",
            "flood_waited": waited,
        }
    left = {int(post["id"]) for post in data.get("posts") or []}
    return {
        "status": "partial" if left else "ok",
        "deleted": [post_id for post_id in batch if post_id not in left],
        "not_deleted": [post_id for post_id in batch if post_id in left],
        "flood_waited": waited,
        "verified_after_timeout": True,
    }


def _batch_failed(code: str, message: str, waited: int) -> dict[str, Any]:
    return {
        "status": "failed",
        "deleted": [],
        "error_code": code,
        "error_message": message,
        "flood_waited": waited,
    }


def _delete_progress(
    number: int, total: int, batch: list[int], outcome: dict[str, Any], deleted_total: int
) -> str:
    head = f"[{number}/{total}] #{batch[0]}…#{batch[-1]}:"
    status = outcome["status"]
    if status == "failed":
        return f"{head} ОШИБКА {outcome.get('error_code')}"
    if status == "unknown":
        return f"{head} НЕИЗВЕСТНО (воркер не ответил)"
    done = len(outcome.get("deleted") or [])
    tail = f", не удалено {len(outcome['not_deleted'])}" if outcome.get("not_deleted") else ""
    return f"{head} удалено {done}{tail} (всего {deleted_total})"


def _snippet(text: str) -> str:
    line = " ".join(text.split())
    return line if len(line) <= 50 else line[:49] + "…"


def _delete_report(
    account_id: uuid.UUID,
    chat: str,
    chat_info: dict[str, Any] | None,
    started: datetime,
    finished: datetime,
    applied: bool,
    backup_path: Path | None,
    rows: list[dict[str, Any]],
) -> dict[str, Any]:
    return {
        **_report(account_id, chat, chat_info, started, finished, applied, backup_path, rows),
        "kind": "channel_delete_results",
    }


# --- аккаунт -----------------------------------------------------------------
_PHONE_RE = re.compile(r"^\+?[\d\s()-]{7,}$")


async def find_account(db: AsyncSession, reference: str) -> Account:
    """Аккаунт по id, названию (label), телефону или @username."""
    ref = reference.strip()
    if not ref:
        raise AccountLookupError("Укажите --account")
    try:
        account_id: uuid.UUID | None = uuid.UUID(ref)
    except ValueError:
        account_id = None
    if account_id is not None:
        account = await db.get(Account, account_id)
        if account is None:
            raise AccountLookupError(f"Аккаунта {account_id} нет в базе")
        return account

    conditions = [
        func.lower(Account.label) == ref.lower(),
        func.lower(Account.username) == ref.lstrip("@").lower(),
    ]
    if _PHONE_RE.match(ref):
        digits = re.sub(r"\D", "", ref)
        conditions.append(Account.phone_e164.in_([f"+{digits}", digits]))
    stmt = select(Account).where(or_(*conditions)).order_by(Account.created_at)
    matches = list((await db.scalars(stmt)).all())
    if len(matches) == 1:
        return matches[0]
    if matches:
        raise AccountLookupError(
            f"Под «{ref}» подходит несколько аккаунтов — укажите id: " + _listing(matches)
        )
    known = list((await db.scalars(select(Account).order_by(Account.created_at))).all())
    raise AccountLookupError(
        f"Аккаунт «{ref}» не найден. Есть: " + (_listing(known) or "аккаунтов нет")
    )


def _listing(accounts: Sequence[Account]) -> str:
    return "; ".join(f"{account.id} «{account.label}»" for account in accounts)


# --- точка входа -------------------------------------------------------------
def _positive_int(value: str) -> int:
    number = int(value)
    if number <= 0:
        raise argparse.ArgumentTypeError("нужно положительное число")
    return number


def _non_negative_int(value: str) -> int:
    number = int(value)
    if number < 0:
        raise argparse.ArgumentTypeError("не может быть отрицательным")
    return number


def _page_size(value: str) -> int:
    number = int(value)
    if not 1 <= number <= READ_LIMIT_MAX:
        raise argparse.ArgumentTypeError(f"от 1 до {READ_LIMIT_MAX}")
    return number


def _delete_batch_size(value: str) -> int:
    number = int(value)
    if not 1 <= number <= DELETE_BATCH_MAX:
        raise argparse.ArgumentTypeError(f"от 1 до {DELETE_BATCH_MAX}")
    return number


def _non_negative_float(value: str) -> float:
    number = float(value)
    if number < 0:
        raise argparse.ArgumentTypeError("не может быть отрицательным")
    return number


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m app.tools.channel_posts",
        description=(
            "Посты канала от имени нашего аккаунта (не бота): выгрузка, замена текстов, удаление."
        ),
    )
    commands = parser.add_subparsers(dest="command", required=True)

    common = argparse.ArgumentParser(add_help=False)
    common.add_argument(
        "--account", required=True, help="id аккаунта (uuid), его название, телефон или @username"
    )
    common.add_argument(
        "--chat",
        required=True,
        help="@username канала, ссылка t.me/..., ссылка-приглашение (+hash) или числовой id",
    )
    common.add_argument(
        "--max-flood-wait",
        type=_non_negative_int,
        default=DEFAULT_MAX_FLOOD_WAIT,
        help="сколько секунд FloodWait CLI готов переждать перед одним повтором",
    )

    read = commands.add_parser("read", parents=[common], help="выгрузить посты в JSON (бэкап)")
    read.add_argument(
        "--limit",
        type=_positive_int,
        default=None,
        help="сколько последних постов (по умолчанию все)",
    )
    read.add_argument(
        "--page-size",
        type=_page_size,
        default=DEFAULT_PAGE_SIZE,
        help=f"постов за одну команду воркеру, до {READ_LIMIT_MAX}",
    )
    read.add_argument(
        "--page-pause",
        type=_non_negative_float,
        default=DEFAULT_PAGE_PAUSE_SECONDS,
        help="пауза между страницами выгрузки, с",
    )
    read.add_argument("--out", type=Path, default=None, help="куда сохранить JSON")

    edit = commands.add_parser("edit", parents=[common], help="заменить тексты по плану")
    edit.add_argument(
        "--plan", type=Path, required=True, help='JSON-список {"id", "text"} или файл из read'
    )
    edit.add_argument(
        "--apply", action="store_true", help="применить (без флага — только проверка)"
    )
    edit.add_argument(
        "--pause",
        type=_non_negative_float,
        default=DEFAULT_PAUSE_SECONDS,
        help="пауза между правками, с",
    )
    edit.add_argument(
        "--parse-mode",
        choices=("none", "html", "md"),
        default="none",
        help="разметка text у элементов без своего parse_mode и html (по умолчанию простой текст)",
    )
    preview = edit.add_mutually_exclusive_group()
    preview.add_argument(
        "--link-preview",
        dest="link_preview",
        action="store_const",
        const=True,
        default=None,
        help="включить превью ссылок во всех постах плана",
    )
    preview.add_argument(
        "--no-link-preview",
        dest="link_preview",
        action="store_const",
        const=False,
        help="выключить превью ссылок (по умолчанию превью остаётся, как сейчас у поста)",
    )
    edit.add_argument(
        "--backup-dir",
        type=Path,
        default=Path(tempfile.gettempdir()),
        help="каталог для бэкапа и результатов",
    )
    edit.add_argument("--results", type=Path, default=None, help="куда записать результаты JSON")

    delete = commands.add_parser(
        "delete", parents=[common], help="удалить посты у всех (необратимо)"
    )
    delete.add_argument(
        "--ids",
        type=Path,
        required=True,
        help="файл с id: JSON-список или текст «12 15 20-40»",
    )
    delete.add_argument(
        "--apply", action="store_true", help="удалить (без флага — только список к удалению)"
    )
    delete.add_argument(
        "--expect",
        type=_positive_int,
        default=None,
        help="сколько постов удалится (число из проверки); обязателен с --apply",
    )
    delete.add_argument(
        "--batch",
        type=_delete_batch_size,
        default=DEFAULT_DELETE_BATCH,
        help=f"постов в одной команде воркеру, до {DELETE_BATCH_MAX}",
    )
    delete.add_argument(
        "--pause",
        type=_non_negative_float,
        default=DEFAULT_DELETE_PAUSE_SECONDS,
        help="пауза между пачками, с",
    )
    delete.add_argument(
        "--backup-dir",
        type=Path,
        default=Path(tempfile.gettempdir()),
        help="каталог для бэкапа и результатов",
    )
    delete.add_argument("--results", type=Path, default=None, help="куда записать результаты JSON")
    return parser


async def _run(args: argparse.Namespace) -> int:
    plan: list[PlanItem] = []
    delete_ids: list[int] = []
    try:
        if args.command == "edit":
            plan = load_plan(args.plan)
        if args.command == "delete":
            delete_ids = load_ids(args.ids)
            if args.apply and args.expect is None:
                raise IdsError(
                    "С --apply нужен --expect: число постов к удалению из проверки без --apply"
                )
        normalize_chat_reference(args.chat)  # кривую ссылку ловим до подключений
    except IdsError as exc:
        print(f"Ошибка списка id: {exc}")
        return EXIT_INVALID
    except CliError as exc:
        print(f"Ошибка плана:\n{exc}")
        return EXIT_INVALID
    except AppError as exc:
        print(f"Ошибка: {exc.message}")
        return EXIT_INVALID

    settings = get_settings()
    # Служебные логи не должны перемешиваться с выводом для человека.
    configure_logging(settings.model_copy(update={"log_level": "WARNING"}))
    runtime = Runtime(settings)
    await runtime.startup()
    try:
        async with runtime.database.session() as db:
            account = await find_account(db, args.account)
            account_id, label = account.id, account.label
        print(f"Аккаунт: {label} ({account_id})")

        redis = runtime.redis.client
        # Как в API (app/api/deps.py): CLI аренду не берёт, только читает.
        lease = AccountLease(redis, "api", ttl_seconds=settings.account_lease_ttl_seconds)
        bus = CommandBus(redis, lease)

        async def caller(command: Command, timeout_seconds: float) -> CommandResult:
            return await bus.call(command, timeout_seconds=timeout_seconds)

        stamp = _stamp(utcnow())
        if args.command == "read":
            out_path = args.out or Path(tempfile.gettempdir()) / f"channel_posts_{stamp}.json"
            return await run_read(
                caller,
                account_id=account_id,
                chat=args.chat,
                out_path=out_path,
                limit=args.limit,
                page_size=args.page_size,
                page_pause=args.page_pause,
                max_flood_wait=args.max_flood_wait,
            )
        if args.command == "delete":
            return await run_delete(
                caller,
                account_id=account_id,
                chat=args.chat,
                ids=delete_ids,
                options=DeleteOptions(
                    apply=args.apply,
                    expect=args.expect,
                    batch_size=args.batch,
                    pause_seconds=args.pause,
                    max_flood_wait=args.max_flood_wait,
                    backup_dir=args.backup_dir,
                    results_path=args.results,
                ),
            )
        return await run_edit(
            caller,
            account_id=account_id,
            chat=args.chat,
            plan=plan,
            options=EditOptions(
                parse_mode=None if args.parse_mode == "none" else args.parse_mode,
                link_preview=args.link_preview,
                apply=args.apply,
                pause_seconds=args.pause,
                max_flood_wait=args.max_flood_wait,
                backup_dir=args.backup_dir,
                results_path=args.results,
            ),
        )
    except CliError as exc:
        print(f"Ошибка: {exc}")
        return EXIT_INVALID
    except CommandFailedError as exc:
        print(f"Воркер вернул ошибку {exc.code}: {exc.message}")
        return EXIT_FAILED
    except AppError as exc:
        print(f"Ошибка {exc.code}: {exc.message}")
        return EXIT_FAILED
    finally:
        await runtime.shutdown()


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return asyncio.run(_run(args))
    except KeyboardInterrupt:
        print("Прервано.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
