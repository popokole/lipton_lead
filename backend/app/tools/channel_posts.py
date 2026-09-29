"""CLI: прочитать посты канала и заменить их тексты от имени нашего аккаунта.

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

План — JSON-список {"id": <id поста>, "text": "<новый текст>"}; подойдёт и файл
из read (берётся его список posts). Посты с тем же текстом пропускаются.

Сам CLI в Telegram не ходит: сессия аккаунта живёт только в воркере, и второе
подключение той же сессии разлогинит аккаунт. Команды READ_CHANNEL_POSTS и
EDIT_MESSAGE уходят воркеру через шину — так же, как у API.

Перед --apply CLI сам сохраняет свежий бэкап текущих текстов правимых постов
(channel_backup_<время>.json) и по итогу пишет результаты в JSON. Правки идут
по одной с паузой и останавливаются на первой ошибке, которую нет смысла
повторять. Повторяются только таймаут ответа воркера (правка идемпотентна) и
FloodWait не длиннее --max-flood-wait.
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
    length_limit,
    normalize_chat_reference,
    render_text,
    utf16_length,
)
from app.workers.lease import AccountLease

Caller = Callable[[Command, float], Awaitable[CommandResult]]
Echo = Callable[[str], None]
Sleep = Callable[[float], Awaitable[None]]
Clock = Callable[[], datetime]

EXIT_OK = 0
EXIT_FAILED = 1
EXIT_INVALID = 2

# Чтение 500 постов — несколько запросов к Telegram; правка — до 31 с
# FloodWait внутри воркера плюс сами запросы.
READ_TIMEOUT_SECONDS = 120.0
EDIT_TIMEOUT_SECONDS = 120.0
DEFAULT_PAGE_SIZE = 100
DEFAULT_PAUSE_SECONDS = 4.0
DEFAULT_MAX_FLOOD_WAIT = 120
IDS_PER_REQUEST = 100
MAX_PAGES = 2000
_FRAGMENT = 30

STATUS_OK = "ok"
STATUS_NO_CHANGE = "no_change"
STATUS_ERROR = "error"
_STATUS_LABELS = {STATUS_OK: "OK", STATUS_NO_CHANGE: "БЕЗ ИЗМЕНЕНИЙ", STATUS_ERROR: "ОШИБКА"}


class CliError(Exception):
    """Ошибка ввода или окружения: печатается человеку как есть."""


class PlanError(CliError):
    pass


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
    id: int
    text: str


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
    """Проверяет план: непустой список {id, text}, id — уникальные положительные."""
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
        text = item.get("text")
        if isinstance(post_id, bool) or not isinstance(post_id, int) or post_id <= 0:
            problems.append(f"{where}: id должен быть положительным целым, а не {post_id!r}")
            continue
        if not isinstance(text, str):
            problems.append(f"{where} (id {post_id}): text должен быть строкой")
            continue
        if post_id in seen:
            problems.append(f"{where}: id {post_id} встречается в плане повторно")
            continue
        seen.add(post_id)
        plan.append(PlanItem(id=post_id, text=text))
    if problems:
        raise PlanError("\n".join(problems))
    return plan


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


def check_item(item: PlanItem, current: dict[str, Any] | None, parse_mode: str | None) -> ItemCheck:
    """Сверяет новый текст с текущим постом: лимиты, права, есть ли что менять."""
    try:
        new_plain, new_entities = render_text(item.text, parse_mode)
    except AppError as exc:
        return ItemCheck(item.id, STATUS_ERROR, None, problems=[exc.message])
    new_length = utf16_length(new_plain)

    if current is None:
        problems = ["поста нет в канале (удалён, служебный или неверный id)"]
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
    )

    if new_length > limit:
        what = "подпись к медиа" if has_media else "текст"
        check.problems.append(f"{what} {new_length} символов — больше лимита {limit}")
    if new_length == 0 and not has_media:
        check.problems.append("пустой текст — Telegram его не примет")
    if current.get("can_edit") is False:
        check.problems.append(
            "аккаунт не может править этот пост (нужно право «Редактировать сообщения»)"
        )

    unchanged = _same_as_current(item.text, new_plain, new_entities, current, parse_mode)
    if not unchanged and parse_mode is None and current.get("formatting_present"):
        check.warnings.append(
            "у поста есть форматирование — без --parse-mode html оно пропадёт "
            "(HTML текущей версии — в поле html выгрузки)"
        )

    check.summary = "текст тот же" if unchanged else diff_summary(old_text, new_plain)
    if check.problems:
        check.status = STATUS_ERROR
    elif unchanged:
        check.status = STATUS_NO_CHANGE
    return check


def _same_as_current(
    text: str,
    plain: str,
    entity_count: int,
    current: dict[str, Any],
    parse_mode: str | None,
) -> bool:
    if entity_count == 0 and not current.get("formatting_present"):
        return plain == current.get("text")
    html = current.get("html")
    return parse_mode == "html" and html is not None and text == html


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


async def read_by_ids(
    caller: Caller, account_id: uuid.UUID, chat: str, ids: Sequence[int]
) -> dict[str, Any]:
    """Текущие версии конкретных постов — для проверки плана и бэкапа."""
    posts: list[dict[str, Any]] = []
    missing: list[int] = []
    chat_info: dict[str, Any] | None = None
    for start in range(0, len(ids), IDS_PER_REQUEST):
        chunk = list(ids[start : start + IDS_PER_REQUEST])
        data = await call_ok(
            caller,
            Command(
                type=CommandType.READ_CHANNEL_POSTS,
                account_id=account_id,
                payload={"chat": chat, "ids": chunk},
            ),
            READ_TIMEOUT_SECONDS,
        )
        chat_info = data.get("chat") or chat_info
        posts.extend(data.get("posts") or [])
        missing.extend(data.get("missing_ids") or [])
    posts.sort(key=lambda post: int(post["id"]))
    return {"chat": chat_info, "posts": posts, "missing_ids": missing}


def write_json(path: Path, document: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        json.dump(document, handle, ensure_ascii=False, indent=2)
        handle.write("\n")


def _stamp(moment: datetime) -> str:
    return moment.strftime("%Y%m%d_%H%M%S")


# --- read --------------------------------------------------------------------
async def run_read(
    caller: Caller,
    *,
    account_id: uuid.UUID,
    chat: str,
    out_path: Path,
    limit: int | None = None,
    page_size: int = DEFAULT_PAGE_SIZE,
    echo: Echo = print,
    now: Clock = utcnow,
) -> int:
    """Выгружает посты (последние limit или все) от старых к новым в JSON."""
    collected: dict[int, dict[str, Any]] = {}
    chat_info: dict[str, Any] | None = None
    skipped = 0
    offset = 0
    partial: dict[str, Any] | None = None

    for _page in range(MAX_PAGES):
        want = page_size if limit is None else min(page_size, limit - len(collected))
        if want <= 0:
            break
        data = await call_ok(
            caller,
            Command(
                type=CommandType.READ_CHANNEL_POSTS,
                account_id=account_id,
                payload={"chat": chat, "limit": want, "offset_id": offset},
            ),
            READ_TIMEOUT_SECONDS,
        )
        chat_info = data.get("chat") or chat_info
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
            "ВНИМАНИЕ: выгрузка неполная — Telegram/Telethon не отдал очередную "
            f"пачку (старше id {partial.get('offset_id')}): {partial.get('detail', '')}"
        )
        return EXIT_FAILED
    return EXIT_OK


def _chat_title(chat_info: dict[str, Any] | None, chat: str) -> str:
    if not chat_info:
        return chat
    username = f"@{chat_info['username']}, " if chat_info.get("username") else ""
    return f"{chat_info.get('title') or chat} ({username}id {chat_info.get('id')})"


def _post_line(post: dict[str, Any]) -> str:
    date = str(post.get("date") or "")[:16].replace("T", " ")
    kind = post.get("media_type") or "text"
    flags = []
    if post.get("formatting_present"):
        flags.append("формат")
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
    link_preview: bool = False
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
    by_id = {int(post["id"]): post for post in current["posts"]}
    checks = [check_item(item, by_id.get(item.id), options.parse_mode) for item in plan]

    mode = "ПРАВКА (--apply)" if options.apply else "ПРОВЕРКА (dry-run)"
    echo(f"Чат: {_chat_title(current['chat'], chat)} — {mode}")
    echo(
        f"parse_mode: {options.parse_mode or 'нет (простой текст)'}, "
        f"превью ссылок: {'да' if options.link_preview else 'нет'}"
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

    results_path = options.results_path or (
        options.backup_dir / f"channel_edit_results_{stamp}.json"
    )
    rows = {
        item.id: _row(item, check, _initial_status(check))
        for item, check in zip(plan, checks, strict=True)
    }
    failed: dict[str, Any] | None = None
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
                caller, account_id, chat, item, rows[item.id], options, echo, sleep
            )
            edits_done += 1
            rows[item.id] = row
            echo(_progress_line(row, edits_done, to_edit))
            if row["status"] == "failed":
                failed = row
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

    if failed is not None:
        echo(
            f"Остановлено на посте #{failed['id']}: {failed.get('error_code')}: "
            f"{failed.get('error_message')}. Остальные не тронуты."
        )
        return EXIT_FAILED
    return EXIT_OK


async def _edit_one(
    caller: Caller,
    account_id: uuid.UUID,
    chat: str,
    item: PlanItem,
    row: dict[str, Any],
    options: EditOptions,
    echo: Echo,
    sleep: Sleep,
) -> dict[str, Any]:
    payload = {
        "chat": chat,
        "message_id": item.id,
        "text": item.text,
        "parse_mode": options.parse_mode,
        "link_preview": options.link_preview,
    }
    waited = 0
    timed_out = False
    for attempt in (1, 2):
        command = Command(type=CommandType.EDIT_MESSAGE, account_id=account_id, payload=payload)
        try:
            result = await caller(command, EDIT_TIMEOUT_SECONDS)
        except CommandTimeoutError as exc:
            if attempt == 1:
                echo(f"    #{item.id}: воркер не ответил вовремя — повторяю (правка идемпотентна)")
                timed_out = True
                continue
            return _failed(row, exc.code, exc.message, waited)
        except AppError as exc:
            return _failed(row, exc.code, exc.message, waited)

        if result.ok:
            no_change = bool(result.data.get("no_change"))
            # Повтор после таймаута видит уже применённую первой попыткой правку.
            status = "edited" if (not no_change or timed_out) else "no_change"
            waited += int(result.data.get("flood_waited") or 0)
            return {**row, "status": status, "flood_waited": waited}

        seconds = _flood_seconds(result)
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


def _initial_status(check: ItemCheck) -> str:
    return "skipped_unchanged" if check.status == STATUS_NO_CHANGE else "not_attempted"


def _flood_seconds(result: CommandResult) -> int | None:
    if result.error_code != "telegram_flood_wait":
        return None
    seconds = result.data.get("seconds")
    if isinstance(seconds, int) and not isinstance(seconds, bool):
        return seconds
    match = re.search(r"(\d+)", result.error_message or "")
    return int(match.group(1)) if match else None


def _row(item: PlanItem, check: ItemCheck, status: str) -> dict[str, Any]:
    return {
        "id": item.id,
        "status": status,
        "old_length": check.old_length,
        "new_length": check.new_length,
        "media_type": check.media_type,
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


def _progress_line(row: dict[str, Any], done: int, total: int) -> str:
    status = row["status"]
    if status == "failed":
        return f"[{done}/{total}] #{row['id']}: ОШИБКА {row.get('error_code')}"
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


def _page_size(value: str) -> int:
    number = int(value)
    if not 1 <= number <= READ_LIMIT_MAX:
        raise argparse.ArgumentTypeError(f"от 1 до {READ_LIMIT_MAX}")
    return number


def _non_negative_float(value: str) -> float:
    number = float(value)
    if number < 0:
        raise argparse.ArgumentTypeError("не может быть отрицательным")
    return number


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m app.tools.channel_posts",
        description="Посты канала от имени нашего аккаунта (не бота): выгрузка и замена текстов.",
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
    read.add_argument("--out", type=Path, default=None, help="куда сохранить JSON")

    edit = commands.add_parser("edit", parents=[common], help="заменить тексты по плану")
    edit.add_argument("--plan", type=Path, required=True, help='JSON-список {"id", "text"}')
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
        help="разметка новых текстов (по умолчанию простой текст)",
    )
    edit.add_argument("--link-preview", action="store_true", help="включить превью ссылок")
    edit.add_argument(
        "--max-flood-wait",
        type=int,
        default=DEFAULT_MAX_FLOOD_WAIT,
        help="сколько секунд FloodWait CLI готов переждать перед одним повтором",
    )
    edit.add_argument(
        "--backup-dir",
        type=Path,
        default=Path(tempfile.gettempdir()),
        help="каталог для бэкапа и результатов",
    )
    edit.add_argument("--results", type=Path, default=None, help="куда записать результаты JSON")
    return parser


async def _run(args: argparse.Namespace) -> int:
    plan: list[PlanItem] = []
    try:
        if args.command == "edit":
            plan = load_plan(args.plan)
        normalize_chat_reference(args.chat)  # кривую ссылку ловим до подключений
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
