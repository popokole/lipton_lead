"""Ручное сообщение в личку от имени аккаунта, с разметкой Telegram HTML.

    python -m app.tools.send_message --account <uuid|label|@username> \\
        --to @username --html-file post.html [--expect-user-id 123] [--apply]

Без --apply только показывает, что уйдёт: текст без тегов, длину и разметку
(цитаты, жирный…). С --apply команда SEND_MESSAGE уходит воркеру через шину —
сессия живёт только в воркере (см. app/tools/channel_posts.py). Теги — как в
Bot API: b, i, u, s, code, pre, a, tg-spoiler, blockquote [expandable].
--expect-user-id сверяет числовой id получателя перед отправкой: username мог
смениться, и сообщение ушло бы чужому человеку.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from collections import Counter
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from app.bus.commands import CommandBus
from app.bus.messages import Command, CommandType
from app.core.config import get_settings
from app.core.errors import AppError
from app.core.logging import configure_logging
from app.core.runtime import Runtime
from app.telegram.channel_posts import (
    TEXT_LIMIT,
    normalize_chat_reference,
    parse_html,
    utf16_length,
)
from app.tools.channel_posts import AccountLookupError, find_account
from app.workers.lease import AccountLease

EXIT_OK = 0
EXIT_FAILED = 1
EXIT_INVALID = 2

_SEND_TIMEOUT_SECONDS = 90.0  # воркер ещё «печатает» несколько секунд перед отправкой


def describe(html_text: str) -> tuple[str, list[str]]:
    """Текст без тегов и сводка разметки — то, что увидит получатель."""
    plain, entities = parse_html(html_text)
    kinds: Counter[str] = Counter()
    for entity in entities:
        name = type(entity).__name__.removeprefix("MessageEntity")
        if name == "Blockquote" and getattr(entity, "collapsed", False):
            name = "Blockquote (expandable)"
        kinds[name] += 1
    summary = [f"{name}: {count}" for name, count in sorted(kinds.items())]
    return plain, summary


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m app.tools.send_message",
        description="Сообщение в личку от имени аккаунта (Telegram HTML).",
    )
    parser.add_argument("--account", required=True, help="id, название или @username аккаунта")
    parser.add_argument("--to", required=True, help="получатель: @username, t.me/… или id")
    parser.add_argument(
        "--html-file", type=Path, required=True, help="файл с текстом в Telegram HTML"
    )
    parser.add_argument("--expect-user-id", type=int, help="ожидаемый числовой id получателя")
    parser.add_argument("--link-preview", action="store_true", help="показывать превью ссылок")
    parser.add_argument(
        "--apply", action="store_true", help="отправить (без флага — только проверка)"
    )
    return parser


async def _run(args: argparse.Namespace) -> int:
    try:
        html_text = args.html_file.read_text(encoding="utf-8").strip()
        normalize_chat_reference(args.to)
        plain, summary = describe(html_text)
    except OSError as exc:
        print(f"Не прочитать файл: {exc}")
        return EXIT_INVALID
    except AppError as exc:
        print(f"Ошибка: {exc.message}")
        return EXIT_INVALID

    length = utf16_length(plain)
    print(
        f"Получатель: {args.to}"
        + (f" (ожидаемый id {args.expect_user_id})" if args.expect_user_id else "")
    )
    print(f"Длина: {length} из {TEXT_LIMIT}; разметка: {', '.join(summary) or 'нет'}")
    print("--- текст без тегов ---")
    print(plain)
    print("-----------------------")
    if not plain.strip() or length > TEXT_LIMIT:
        print("Текст пустой или длиннее лимита Telegram.")
        return EXIT_INVALID
    if not args.apply:
        print("Проверка без отправки. Для отправки добавьте --apply.")
        return EXIT_OK

    settings = get_settings()
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
        payload: dict[str, Any] = {
            "to": args.to,
            "text": html_text,
            "parse_mode": "html",
            "link_preview": bool(args.link_preview),
        }
        if args.expect_user_id is not None:
            payload["expect_user_id"] = args.expect_user_id
        result = await bus.call(
            Command(type=CommandType.SEND_MESSAGE, account_id=account_id, payload=payload),
            timeout_seconds=_SEND_TIMEOUT_SECONDS,
        )
        if not result.ok:
            print(f"Воркер вернул ошибку {result.error_code}: {result.error_message}")
            return EXIT_FAILED
        message_id, chat_id = result.data.get("tg_message_id"), result.data.get("chat_id")
        print(f"Отправлено: сообщение {message_id} в чат {chat_id}")
        return EXIT_OK
    except AccountLookupError as exc:
        print(f"Ошибка: {exc}")
        return EXIT_INVALID
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
