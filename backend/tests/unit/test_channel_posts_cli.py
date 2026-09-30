"""CLI правки и удаления постов канала (app/tools/channel_posts.py).

Воркер подменён: CLI общается с ним только через Command/CommandResult,
поэтому подделка отвечает на READ_CHANNEL_POSTS и EDIT_MESSAGE по сценарию,
применяет правки к своим постам (как настоящий канал) и записывает всё, что
ей прислали.
"""

from __future__ import annotations

import base64
import json
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from app.bus.messages import Command, CommandResult, CommandType
from app.core.errors import CommandTimeoutError, WorkerUnavailableError
from app.telegram.channel_posts import entity_signature, render_entities, unparse_html
from app.tools.channel_posts import (
    DELETE_EXPIRY_SECONDS,
    EDIT_EXPIRY_SECONDS,
    EXIT_FAILED,
    EXIT_INVALID,
    EXIT_OK,
    MAX_DELETE_IDS,
    DeleteOptions,
    EditOptions,
    IdsError,
    MediaOptions,
    PlanError,
    PlanItem,
    build_parser,
    check_item,
    diff_summary,
    load_media_plan,
    load_plan,
    parse_ids,
    parse_plan,
    run_delete,
    run_edit,
    run_media,
    run_read,
)

ACCOUNT = uuid.UUID("11111111-2222-3333-4444-555555555555")
MARKED_ID = -1001234567890
CHAT_INFO = {"id": MARKED_ID, "title": "Шаблоны", "username": "templates", "type": "channel"}
NOW = datetime(2026, 9, 30, 10, 0, 0, tzinfo=UTC)
STAMP = "20260930_100000"
# Правка применилась, но ответ воркера не дошёл до CLI.
APPLY_THEN_TIMEOUT = object()


def post(
    post_id: int,
    text: str,
    *,
    media_type: str | None = None,
    formatting: bool = False,
    html: str | None = None,
    lossless: bool | None = None,
    can_edit: bool | None = True,
) -> dict[str, Any]:
    has_media = media_type is not None and media_type != "webpage"
    return {
        "id": post_id,
        "date": "2026-09-01T12:30:00+00:00",
        "text": text,
        "html": html,
        "html_lossless": (True if lossless is None else lossless) if html is not None else None,
        "length": len(text),
        "entities_present": formatting,
        "formatting_present": formatting,
        "has_media": has_media,
        "media_type": media_type,
        "grouped_id": None,
        "views": 10,
        "can_edit": can_edit,
    }


class FakeWorker:
    """Отвечает как воркер.

    edit_script: id → очередь ответов на правку (CommandResult, исключение или
    APPLY_THEN_TIMEOUT). page_script: offset_id → очередь ответов на страницу
    выгрузки до нормального ответа. read_failures: номер READ-команды (с нуля)
    → исключение вместо ответа.
    """

    def __init__(
        self,
        posts: list[dict[str, Any]] | None = None,
        *,
        edit_script: dict[int, list[Any]] | None = None,
        page_script: dict[int, list[Any]] | None = None,
        read_failures: dict[int, BaseException] | None = None,
        on_first_edit: Callable[[], None] | None = None,
        partial_at_offset: int | None = None,
        delete_script: dict[int, list[Any]] | None = None,
        media_script: dict[int, list[Any]] | None = None,
        undeletable: set[int] | None = None,
        on_first_delete: Callable[[], None] | None = None,
    ) -> None:
        self.posts = {p["id"]: dict(p) for p in posts or []}
        self.edit_script = edit_script or {}
        self.page_script = page_script or {}
        self.read_failures = read_failures or {}
        self.on_first_edit = on_first_edit
        self.partial_at_offset = partial_at_offset
        # Ключ — первый id пачки.
        self.delete_script = delete_script or {}
        self.undeletable = undeletable or set()
        self.on_first_delete = on_first_delete
        self.media_script = media_script or {}
        self.commands: list[Command] = []

    @property
    def media(self) -> list[Command]:
        return [c for c in self.commands if c.type is CommandType.EDIT_MEDIA]

    @property
    def deletes(self) -> list[Command]:
        return [c for c in self.commands if c.type is CommandType.DELETE_MESSAGES]

    @property
    def edits(self) -> list[Command]:
        return [c for c in self.commands if c.type is CommandType.EDIT_MESSAGE]

    @property
    def reads(self) -> list[Command]:
        return [c for c in self.commands if c.type is CommandType.READ_CHANNEL_POSTS]

    async def __call__(self, command: Command, timeout_seconds: float) -> CommandResult:
        self.commands.append(command)
        if command.type is CommandType.READ_CHANNEL_POSTS:
            failure = self.read_failures.get(len(self.reads) - 1)
            if failure is not None:
                raise failure
            return self._read(command)
        if command.type is CommandType.EDIT_MESSAGE:
            if len(self.edits) == 1 and self.on_first_edit is not None:
                self.on_first_edit()
            script = self.edit_script.get(command.payload["message_id"]) or []
            if script:
                answer = script.pop(0)
                if answer is APPLY_THEN_TIMEOUT:
                    self._apply(command.payload)
                    raise CommandTimeoutError("no answer")
                if isinstance(answer, BaseException):
                    raise answer
                return answer
            self._apply(command.payload)
            return CommandResult.success(command.id, edited=True, no_change=False, flood_waited=0)
        if command.type is CommandType.EDIT_MEDIA:
            script = self.media_script.get(command.payload["message_id"]) or []
            if script:
                answer = script.pop(0)
                if isinstance(answer, BaseException):
                    raise answer
                return answer
            return CommandResult.success(command.id, replaced=True, no_change=False, flood_waited=0)
        if command.type is CommandType.DELETE_MESSAGES:
            if len(self.deletes) == 1 and self.on_first_delete is not None:
                self.on_first_delete()
            ids = command.payload["ids"]
            script = self.delete_script.get(ids[0]) or []
            if script:
                answer = script.pop(0)
                if answer is APPLY_THEN_TIMEOUT:
                    self._delete(command.id, ids)
                    raise CommandTimeoutError("no answer")
                if isinstance(answer, BaseException):
                    raise answer
                return answer
            return self._delete(command.id, ids)
        raise AssertionError(f"unexpected command {command.type}")

    def _delete(self, command_id: uuid.UUID, ids: list[int]) -> CommandResult:
        present = [i for i in ids if i in self.posts]
        gone = [i for i in present if i not in self.undeletable]
        for post_id in gone:
            del self.posts[post_id]
        return CommandResult.success(
            command_id,
            deleted=gone,
            already_missing=[i for i in ids if i not in present],
            not_deleted=[i for i in present if i not in gone],
            flood_waited=0,
        )

    def _apply(self, payload: dict[str, Any]) -> None:
        plain, entities = render_entities(payload["text"], payload["parse_mode"])
        formatted = bool(entity_signature(entities))
        self.posts[payload["message_id"]].update(
            text=plain,
            html=unparse_html(plain, entities) if formatted else None,
            html_lossless=True if formatted else None,
            formatting_present=formatted,
            entities_present=formatted,
            length=len(plain),
        )

    def _read(self, command: Command) -> CommandResult:
        payload = command.payload
        if "ids" in payload:
            found = [dict(self.posts[i]) for i in payload["ids"] if i in self.posts]
            missing = [i for i in payload["ids"] if i not in self.posts]
            return CommandResult.success(
                command.id,
                chat=CHAT_INFO,
                posts=sorted(found, key=lambda p: p["id"]),
                missing_ids=missing,
            )
        offset, limit = payload["offset_id"], payload["limit"]
        script = self.page_script.get(offset) or []
        if script:
            answer = script.pop(0)
            if isinstance(answer, BaseException):
                raise answer
            return answer
        if self.partial_at_offset is not None and offset == self.partial_at_offset:
            return CommandResult.success(
                command.id,
                chat=CHAT_INFO,
                posts=[],
                fetched=0,
                skipped_service=0,
                next_offset_id=None,
                partial=True,
                error={"offset_id": offset, "detail": "TypeNotFoundError: boom"},
            )
        newest_first = sorted(
            (p for p in self.posts.values() if not offset or p["id"] < offset),
            key=lambda p: p["id"],
            reverse=True,
        )[:limit]
        return CommandResult.success(
            command.id,
            chat=CHAT_INFO,
            posts=sorted(newest_first, key=lambda p: p["id"]),
            fetched=len(newest_first),
            skipped_service=0,
            next_offset_id=(
                min(p["id"] for p in newest_first) if len(newest_first) == limit else None
            ),
            partial=False,
        )


class RecordingSleep:
    def __init__(self) -> None:
        self.calls: list[float] = []

    async def __call__(self, seconds: float) -> None:
        self.calls.append(seconds)


def failure(code: str, message: str = "", **data: Any) -> CommandResult:
    return CommandResult.failure(uuid.uuid4(), code, message, data=data)


def success(**data: Any) -> CommandResult:
    return CommandResult.success(uuid.uuid4(), **data)


async def edit(
    worker: FakeWorker,
    plan: list[PlanItem],
    tmp_path: Path,
    *,
    apply: bool = False,
    lines: list[str] | None = None,
    sleep: RecordingSleep | None = None,
    **options: Any,
) -> int:
    return await run_edit(
        worker,
        account_id=ACCOUNT,
        chat="@templates",
        plan=plan,
        options=EditOptions(apply=apply, backup_dir=tmp_path, **options),
        echo=(lines if lines is not None else []).append,
        sleep=sleep or RecordingSleep(),
        now=lambda: NOW,
    )


async def read(
    worker: FakeWorker,
    out: Path,
    *,
    lines: list[str] | None = None,
    sleep: RecordingSleep | None = None,
    **options: Any,
) -> int:
    return await run_read(
        worker,
        account_id=ACCOUNT,
        chat="@templates",
        out_path=out,
        echo=(lines if lines is not None else []).append,
        sleep=sleep or RecordingSleep(),
        now=lambda: NOW,
        **options,
    )


def read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def results(tmp_path: Path) -> dict[str, Any]:
    return read_json(tmp_path / f"channel_edit_results_{STAMP}.json")


# --- план ---------------------------------------------------------------------
class TestParsePlan:
    def test_list_of_items(self) -> None:
        plan = parse_plan([{"id": 3, "text": "три"}, {"id": 1, "text": ""}])
        assert plan == [PlanItem(3, "три"), PlanItem(1, "")]

    def test_read_output_is_accepted_as_plan(self) -> None:
        document = {"kind": "channel_posts", "posts": [post(5, "пять"), post(6, "шесть")]}
        # text в выгрузке — простой текст, а не разметка, что бы ни было в --parse-mode.
        assert parse_plan(document) == [
            PlanItem(5, "пять", mode_set=True),
            PlanItem(6, "шесть", mode_set=True),
        ]

    def test_formatted_post_in_read_output_carries_html(self) -> None:
        document = {
            "kind": "channel_posts",
            "posts": [post(7, "жирный", formatting=True, html="<b>жирный</b>")],
        }
        assert parse_plan(document) == [PlanItem(7, "жирный", html="<b>жирный</b>", mode_set=True)]

    def test_item_options(self) -> None:
        plan = parse_plan(
            [
                {"id": 1, "html": "<b>x</b>"},
                {"id": 2, "text": "**y**", "parse_mode": "md", "link_preview": True},
                {"id": 3, "text": "z", "parse_mode": None},
                {"id": 4, "text": "w", "parse_mode": "none"},
            ]
        )
        assert plan == [
            PlanItem(1, None, html="<b>x</b>"),
            PlanItem(2, "**y**", parse_mode="md", mode_set=True, link_preview=True),
            PlanItem(3, "z", mode_set=True),
            PlanItem(4, "w", mode_set=True),
        ]

    @pytest.mark.parametrize("raw", [[], {}, "text", None, {"posts": []}])
    def test_empty_or_not_a_list(self, raw: Any) -> None:
        with pytest.raises(PlanError):
            parse_plan(raw)

    def test_all_problems_are_reported_together(self) -> None:
        raw = [
            {"id": 1, "text": "ok"},
            {"id": 1, "text": "дубль"},
            {"id": "2", "text": "x"},
            {"id": True, "text": "x"},
            {"id": 0, "text": "x"},
            {"id": 4, "text": None},
            "не объект",
        ]
        with pytest.raises(PlanError) as caught:
            parse_plan(raw)
        message = str(caught.value)
        assert "id 1 встречается в плане повторно" in message
        assert "'2'" in message
        assert "True" in message
        assert "text должен быть строкой" in message
        assert "ожидается объект" in message
        assert len(message.splitlines()) == 6

    def test_item_option_problems(self) -> None:
        raw = [
            {"id": 1, "text": "x", "parse_mode": "markdown"},
            {"id": 2, "text": "x", "link_preview": "yes"},
            {"id": 3, "html": 5},
        ]
        with pytest.raises(PlanError) as caught:
            parse_plan(raw)
        message = str(caught.value)
        assert "parse_mode должен быть" in message
        assert "link_preview должен быть" in message
        assert "html должен быть строкой" in message

    def test_load_plan_tolerates_bom(self, tmp_path: Path) -> None:
        path = tmp_path / "plan.json"
        path.write_bytes(b"\xef\xbb\xbf" + json.dumps([{"id": 1, "text": "Привет"}]).encode())
        assert load_plan(path) == [PlanItem(1, "Привет")]

    def test_load_plan_errors(self, tmp_path: Path) -> None:
        with pytest.raises(PlanError, match="не найден"):
            load_plan(tmp_path / "missing.json")
        broken = tmp_path / "broken.json"
        broken.write_text("[{", encoding="utf-8")
        with pytest.raises(PlanError, match="JSON"):
            load_plan(broken)


# --- проверка -----------------------------------------------------------------
class TestDiffSummary:
    def test_same(self) -> None:
        assert diff_summary("abc", "abc") == "текст тот же"

    def test_words_and_first_change(self) -> None:
        summary = diff_summary("Цена 990 рублей", "Цена 950 рублей")
        assert "слов −1 +1" in summary
        assert "строк 1→1" in summary
        # Фрагмент — с начала слова, в котором расхождение.
        assert "«…990 рублей» → «…950 рублей»" in summary

    def test_change_at_start_has_no_leading_ellipsis(self) -> None:
        summary = diff_summary("Скидка 10%", "Акция 10%")
        assert "«Скидка 10%» → «Акция 10%»" in summary

    def test_whitespace_only(self) -> None:
        assert diff_summary("a b", "a  b").startswith("отличия только в пробелах")

    def test_long_fragment_is_truncated(self) -> None:
        summary = diff_summary("x" * 10 + "a" * 100, "x" * 10 + "b" * 100)
        assert "…" in summary
        assert "a" * 31 not in summary


def formatted(
    post_id: int = 1, text: str = "жирный", html: str = "<b>жирный</b>", **kw: Any
) -> Any:
    return post(post_id, text, formatting=True, html=html, **kw)


def document_item(current: dict[str, Any], **changes: Any) -> PlanItem:
    """Элемент плана из файла выгрузки с правками владельца."""
    return parse_plan({"kind": "channel_posts", "posts": [{**current, **changes}]})[0]


class TestCheckItem:
    def test_missing_post(self) -> None:
        check = check_item(PlanItem(9, "x"), None, None)
        assert check.status == "error"
        assert "поста нет" in check.problems[0]

    def test_caption_over_limit(self) -> None:
        check = check_item(PlanItem(1, "я" * 1025), post(1, "фото", media_type="photo"), None)
        assert check.status == "error"
        assert check.limit == 1024
        assert "подпись к медиа 1025" in check.problems[0]

    def test_long_text_on_text_post_is_fine(self) -> None:
        check = check_item(PlanItem(1, "я" * 2000), post(1, "текст"), None)
        assert check.status == "ok"
        assert check.limit == 4096

    def test_text_over_4096(self) -> None:
        check = check_item(PlanItem(1, "я" * 4097), post(1, "текст"), None)
        assert check.status == "error"

    def test_unchanged(self) -> None:
        check = check_item(PlanItem(1, "тот же"), post(1, "тот же"), None)
        assert check.status == "no_change"

    def test_unchanged_html_matches_current_formatting(self) -> None:
        check = check_item(PlanItem(1, "<strong>жирный</strong>"), formatted(), "html")
        assert check.status == "no_change"

    @pytest.mark.parametrize("parse_mode", [None, "html"])
    def test_formatted_post_with_same_plain_text_is_left_alone(
        self, parse_mode: str | None
    ) -> None:
        # План {id, text} с тем же текстом не должен стирать жирный/ссылки.
        check = check_item(PlanItem(1, "жирный"), formatted(), parse_mode)
        assert check.status == "no_change"
        assert check.warnings == []

    @pytest.mark.parametrize("parse_mode", [None, "html"])
    def test_untouched_formatted_post_from_read_file_is_skipped(
        self, parse_mode: str | None
    ) -> None:
        current = formatted()
        check = check_item(document_item(current), current, parse_mode)
        assert check.status == "no_change"

    @pytest.mark.parametrize("parse_mode", [None, "html"])
    def test_plain_post_from_read_file_is_sent_as_plain_text(self, parse_mode: str | None) -> None:
        current = post(1, "Цена < 100 & скидка")
        check = check_item(document_item(current, text="Цена < 90 & скидка"), current, parse_mode)
        assert check.status == "ok"
        assert check.send_mode is None
        assert check.send_text == "Цена < 90 & скидка"
        assert check.expected_text == "Цена < 90 & скидка"

    def test_html_edit_in_read_file_keeps_formatting(self) -> None:
        current = formatted()
        item = document_item(current, text="новый", html="<b>новый</b>")
        check = check_item(item, current, None)
        assert check.status == "ok"
        assert (check.send_text, check.send_mode) == ("<b>новый</b>", "html")
        assert check.warnings == []

    def test_html_only_edit_in_read_file_is_fine(self) -> None:
        current = formatted()
        check = check_item(document_item(current, html="<b>новый</b>"), current, None)
        assert check.status == "ok"
        assert check.expected_text == "новый"

    def test_text_only_edit_of_formatted_post_is_an_error(self) -> None:
        current = formatted()
        check = check_item(document_item(current, text="новый"), current, None)
        assert check.status == "error"
        assert "поле html" in check.problems[0]

    def test_text_and_html_disagree(self) -> None:
        current = formatted()
        item = document_item(current, text="одно", html="<b>другое</b>")
        check = check_item(item, current, None)
        assert check.status == "error"
        assert "расходятся" in check.problems[0]

    def test_lossy_html_is_warned(self) -> None:
        current = formatted(lossless=False)
        check = check_item(document_item(current, html="<b>новый</b>"), current, None)
        assert check.status == "ok"
        assert any("неточный" in warning for warning in check.warnings)

    def test_formatting_only_change(self) -> None:
        check = check_item(PlanItem(1, None, html="<i>жирный</i>"), formatted(), None)
        assert check.status == "ok"
        assert check.summary == "текст тот же, меняется форматирование"

    def test_formatting_loss_is_warned(self) -> None:
        check = check_item(PlanItem(1, "другой"), formatted(), None)
        assert check.status == "ok"
        assert "форматирование" in check.warnings[0]

    def test_no_warning_with_html_mode(self) -> None:
        check = check_item(PlanItem(1, "<b>другой</b>"), formatted(), "html")
        assert check.warnings == []
        assert check.new_length == 6

    def test_markup_in_plain_mode_is_literal(self) -> None:
        check = check_item(PlanItem(1, "<b>x</b>"), post(1, "старый"), None)
        assert check.new_length == 8
        assert check.send_mode is None

    def test_link_preview_is_kept_by_default(self) -> None:
        current = post(1, "см. https://x.com", media_type="webpage")
        check = check_item(PlanItem(1, "см. https://y.com"), current, None)
        assert check.send_link_preview is True
        assert check.warnings == []

    def test_removing_link_preview_is_warned(self) -> None:
        current = post(1, "см. https://x.com", media_type="webpage")
        check = check_item(PlanItem(1, "см. https://y.com"), current, None, link_preview=False)
        assert check.send_link_preview is False
        assert any("превью" in warning for warning in check.warnings)

    def test_item_link_preview_overrides_option(self) -> None:
        item = PlanItem(1, "https://y.com", link_preview=True)
        check = check_item(item, post(1, "старый"), None, link_preview=False)
        assert check.send_link_preview is True

    def test_post_that_cannot_be_edited(self) -> None:
        check = check_item(PlanItem(1, "новый"), post(1, "старый", can_edit=False), None)
        assert check.status == "error"

    def test_empty_text_for_text_post(self) -> None:
        check = check_item(PlanItem(1, ""), post(1, "старый"), None)
        assert check.status == "error"


# --- edit: dry-run ------------------------------------------------------------
class TestEditDryRun:
    async def test_dry_run_changes_nothing(self, tmp_path: Path) -> None:
        worker = FakeWorker([post(1, "старый один"), post(2, "тот же")])
        lines: list[str] = []

        code = await edit(
            worker,
            [PlanItem(1, "новый один"), PlanItem(2, "тот же")],
            tmp_path,
            lines=lines,
        )

        assert code == EXIT_OK
        assert worker.edits == []
        assert [c.payload for c in worker.reads] == [{"chat": "@templates", "ids": [1, 2]}]
        output = "\n".join(lines)
        assert "ПРОВЕРКА (dry-run)" in output
        assert "Шаблоны (@templates, канал, id -1001234567890)" in output
        assert "#1" in output and "11→10/4096" in output and "OK" in output
        assert "#2" in output and "БЕЗ ИЗМЕНЕНИЙ" in output
        assert "к правке 1, без изменений 1, с ошибками 0" in output
        assert list(tmp_path.iterdir()) == []  # ни бэкапа, ни результатов

    async def test_dry_run_reports_errors_with_exit_code(self, tmp_path: Path) -> None:
        worker = FakeWorker([post(1, "фото", media_type="photo")])
        lines: list[str] = []

        code = await edit(
            worker, [PlanItem(1, "я" * 1100), PlanItem(2, "x")], tmp_path, lines=lines
        )

        assert code == EXIT_INVALID
        output = "\n".join(lines)
        assert "ошибка: подпись к медиа 1100" in output
        assert "ошибка: поста нет" in output

    async def test_dry_run_can_write_report(self, tmp_path: Path) -> None:
        worker = FakeWorker([post(1, "старый")])
        report = tmp_path / "report.json"

        await edit(worker, [PlanItem(1, "новый")], tmp_path, results_path=report)

        document = read_json(report)
        assert document["applied"] is False
        assert document["items"][0]["status"] == "dry_run_ok"

    async def test_ids_are_read_in_chunks_resolving_the_chat_once(self, tmp_path: Path) -> None:
        posts = [post(i, f"p{i}") for i in range(1, 251)]
        worker = FakeWorker(posts)

        await edit(worker, [PlanItem(i, f"n{i}") for i in range(1, 251)], tmp_path)

        assert [len(c.payload["ids"]) for c in worker.reads] == [100, 100, 50]
        # После первого ответа канал адресуется помеченным id, без @username.
        assert [c.payload["chat"] for c in worker.reads] == ["@templates", MARKED_ID, MARKED_ID]


# --- edit: --apply ------------------------------------------------------------
class TestEditApply:
    async def test_backup_first_then_edits_with_pause(self, tmp_path: Path) -> None:
        backup = tmp_path / f"channel_backup_{STAMP}.json"
        seen_backup: list[bool] = []
        worker = FakeWorker(
            [post(1, "один"), post(2, "два"), post(3, "три")],
            on_first_edit=lambda: seen_backup.append(backup.exists()),
        )
        sleep = RecordingSleep()
        lines: list[str] = []

        code = await edit(
            worker,
            [PlanItem(3, "ТРИ"), PlanItem(2, "два"), PlanItem(1, "ОДИН")],
            tmp_path,
            apply=True,
            sleep=sleep,
            lines=lines,
            pause_seconds=4.0,
        )

        assert code == EXIT_OK
        assert seen_backup == [True]  # бэкап записан до первой правки
        saved = read_json(backup)
        assert [p["id"] for p in saved["posts"]] == [3, 2, 1]
        assert saved["posts"][0]["text"] == "три"
        # Порядок плана сохраняется, неизменённый пост не трогаем.
        assert [c.payload["message_id"] for c in worker.edits] == [3, 1]
        assert worker.edits[0].payload == {
            "chat": MARKED_ID,
            "message_id": 3,
            "text": "ТРИ",
            "parse_mode": None,
            "link_preview": False,
            "expires_at": NOW.timestamp() + EDIT_EXPIRY_SECONDS,
        }
        assert sleep.calls == [4.0]  # пауза только между правками

        report = results(tmp_path)
        assert report["applied"] is True
        assert report["backup"] == str(backup)
        assert [(i["id"], i["status"]) for i in report["items"]] == [
            (3, "edited"),
            (2, "skipped_unchanged"),
            (1, "edited"),
        ]
        assert report["summary"] == {"edited": 2, "skipped_unchanged": 1}

    async def test_stops_on_first_non_retryable_error(self, tmp_path: Path) -> None:
        worker = FakeWorker(
            [post(1, "a"), post(2, "b"), post(3, "c")],
            edit_script={2: [failure("chat_admin_required", "нет прав")]},
        )
        lines: list[str] = []

        code = await edit(
            worker,
            [PlanItem(1, "A"), PlanItem(2, "B"), PlanItem(3, "C")],
            tmp_path,
            apply=True,
            lines=lines,
        )

        assert code == EXIT_FAILED
        assert [c.payload["message_id"] for c in worker.edits] == [1, 2]
        report = results(tmp_path)
        assert [(i["id"], i["status"]) for i in report["items"]] == [
            (1, "edited"),
            (2, "failed"),
            (3, "not_attempted"),
        ]
        assert report["items"][1]["error_code"] == "chat_admin_required"
        assert any("Остановлено на посте #2" in line for line in lines)

    async def test_worker_flood_wait_is_waited_once(self, tmp_path: Path) -> None:
        worker = FakeWorker(
            [post(1, "a")],
            edit_script={1: [failure("telegram_flood_wait", "подождите 40 с", seconds=40)]},
        )
        sleep = RecordingSleep()

        code = await edit(worker, [PlanItem(1, "A")], tmp_path, apply=True, sleep=sleep)

        assert code == EXIT_OK
        assert sleep.calls == [41]
        assert len(worker.edits) == 2
        item = results(tmp_path)["items"][0]
        assert item["status"] == "edited"
        assert item["flood_waited"] == 40

    async def test_too_long_flood_wait_stops(self, tmp_path: Path) -> None:
        worker = FakeWorker(
            [post(1, "a"), post(2, "b")],
            edit_script={1: [failure("telegram_flood_wait", "подождите 900 с", seconds=900)]},
        )
        sleep = RecordingSleep()

        code = await edit(
            worker, [PlanItem(1, "A"), PlanItem(2, "B")], tmp_path, apply=True, sleep=sleep
        )

        assert code == EXIT_FAILED
        assert sleep.calls == []
        report = results(tmp_path)
        assert report["items"][0]["retry_after"] == 900
        assert report["items"][1]["status"] == "not_attempted"

    async def test_timeout_is_verified_by_rereading_not_by_resending(self, tmp_path: Path) -> None:
        worker = FakeWorker([post(1, "a"), post(2, "b")], edit_script={1: [APPLY_THEN_TIMEOUT]})

        code = await edit(worker, [PlanItem(1, "A"), PlanItem(2, "B")], tmp_path, apply=True)

        assert code == EXIT_OK
        assert [c.payload["message_id"] for c in worker.edits] == [1, 2]  # без повтора #1
        assert worker.reads[-1].payload == {"chat": MARKED_ID, "ids": [1]}
        items = results(tmp_path)["items"]
        assert items[0]["status"] == "edited"
        assert items[0]["verified_after_timeout"] is True
        assert items[1]["status"] == "edited"

    async def test_unconfirmed_edit_is_unknown_and_stops(self, tmp_path: Path) -> None:
        worker = FakeWorker(
            [post(1, "a"), post(2, "b")], edit_script={1: [CommandTimeoutError("no answer")]}
        )
        lines: list[str] = []

        code = await edit(
            worker, [PlanItem(1, "A"), PlanItem(2, "B")], tmp_path, apply=True, lines=lines
        )

        assert code == EXIT_FAILED
        assert len(worker.edits) == 1  # в очередь воркера второй экземпляр не ушёл
        items = results(tmp_path)["items"]
        assert [(i["id"], i["status"]) for i in items] == [(1, "unknown"), (2, "not_attempted")]
        assert items[0]["error_code"] == "edit_unconfirmed"
        assert any("исход правки неизвестен" in line for line in lines)

    async def test_failed_verification_is_unknown(self, tmp_path: Path) -> None:
        worker = FakeWorker(
            [post(1, "a")],
            edit_script={1: [APPLY_THEN_TIMEOUT]},
            read_failures={1: CommandTimeoutError("still busy")},
        )

        code = await edit(worker, [PlanItem(1, "A")], tmp_path, apply=True)

        assert code == EXIT_FAILED
        item = results(tmp_path)["items"][0]
        assert item["status"] == "unknown"
        assert "command_timeout" in item["error_message"]

    async def test_worker_unavailable_is_not_retried(self, tmp_path: Path) -> None:
        worker = FakeWorker([post(1, "a")], edit_script={1: [WorkerUnavailableError("no worker")]})

        code = await edit(worker, [PlanItem(1, "A")], tmp_path, apply=True)

        assert code == EXIT_FAILED
        assert len(worker.edits) == 1

    async def test_invalid_plan_is_not_applied_at_all(self, tmp_path: Path) -> None:
        worker = FakeWorker([post(1, "a"), post(2, "фото", media_type="photo")])

        code = await edit(worker, [PlanItem(1, "A"), PlanItem(2, "я" * 2000)], tmp_path, apply=True)

        assert code == EXIT_INVALID
        assert worker.edits == []
        assert list(tmp_path.iterdir()) == []

    async def test_nothing_to_change(self, tmp_path: Path) -> None:
        worker = FakeWorker([post(1, "a")])

        code = await edit(worker, [PlanItem(1, "a")], tmp_path, apply=True)

        assert code == EXIT_OK
        assert worker.edits == []

    async def test_parse_mode_is_sent_to_worker(self, tmp_path: Path) -> None:
        worker = FakeWorker([post(1, "a")])

        await edit(
            worker,
            [PlanItem(1, "<b>A</b>")],
            tmp_path,
            apply=True,
            parse_mode="html",
            link_preview=True,
        )

        assert worker.edits[0].payload["parse_mode"] == "html"
        assert worker.edits[0].payload["link_preview"] is True

    async def test_backup_restores_formatting_and_plain_text(self, tmp_path: Path) -> None:
        original = [
            formatted(1, "жирный текст", "<b>жирный</b> текст"),
            post(2, "Цена < 100 & скидка"),
        ]
        worker = FakeWorker(original)

        code = await edit(
            worker, [PlanItem(1, "новый один"), PlanItem(2, "новый два")], tmp_path, apply=True
        )
        assert code == EXIT_OK
        assert worker.posts[1]["html"] is None  # правка простым текстом стёрла жирный

        restore = load_plan(tmp_path / f"channel_backup_{STAMP}.json")
        edits_before = len(worker.edits)
        code = await edit(worker, restore, tmp_path, apply=True)

        assert code == EXIT_OK
        payloads = [c.payload for c in worker.edits[edits_before:]]
        assert [(p["message_id"], p["text"], p["parse_mode"]) for p in payloads] == [
            (1, "<b>жирный</b> текст", "html"),
            (2, "Цена < 100 & скидка", None),
        ]
        for before in original:
            after = worker.posts[before["id"]]
            assert (after["text"], after["html"]) == (before["text"], before["html"])


# --- read ---------------------------------------------------------------------
class TestRead:
    async def test_reads_everything_oldest_first(self, tmp_path: Path) -> None:
        worker = FakeWorker([post(i, f"Привет {i}") for i in range(1, 251)])
        out = tmp_path / "posts.json"
        lines: list[str] = []
        sleep = RecordingSleep()

        code = await read(worker, out, lines=lines, sleep=sleep)

        assert code == EXIT_OK
        assert [(c.payload["limit"], c.payload["offset_id"]) for c in worker.reads] == [
            (100, 0),
            (100, 151),
            (100, 51),
        ]
        # Канал ищется по @username один раз, дальше — по помеченному id.
        assert [c.payload["chat"] for c in worker.reads] == ["@templates", MARKED_ID, MARKED_ID]
        assert sleep.calls == [1.0, 1.0]  # пауза между страницами, не перед первой
        raw = out.read_text(encoding="utf-8")
        assert "Привет 1" in raw  # UTF-8 как есть, без \\u-экранирования
        document = json.loads(raw)
        assert document["count"] == 250
        assert [p["id"] for p in document["posts"]] == list(range(1, 251))
        assert document["chat_info"] == CHAT_INFO
        assert document["partial"] is False
        assert any("Шаблоны (@templates, канал, id -1001234567890)" in line for line in lines)
        assert any("Прочитано постов: 250 (id 1…250)" in line for line in lines)

    async def test_limit_keeps_newest_posts(self, tmp_path: Path) -> None:
        worker = FakeWorker([post(i, "x") for i in range(1, 251)])
        out = tmp_path / "posts.json"

        await read(worker, out, limit=120)

        assert [c.payload["limit"] for c in worker.reads] == [100, 20]
        ids = [p["id"] for p in read_json(out)["posts"]]
        assert ids == list(range(131, 251))

    async def test_partial_read_is_saved_and_flagged(self, tmp_path: Path) -> None:
        worker = FakeWorker([post(i, "x") for i in range(1, 251)], partial_at_offset=151)
        out = tmp_path / "posts.json"
        lines: list[str] = []

        code = await read(worker, out, lines=lines)

        assert code == EXIT_FAILED
        document = read_json(out)
        assert document["partial"] is True
        assert document["count"] == 100
        assert any("выгрузка неполная" in line for line in lines)

    async def test_failure_midway_keeps_what_was_read(self, tmp_path: Path) -> None:
        worker = FakeWorker(
            [post(i, "x") for i in range(1, 251)],
            page_script={151: [CommandTimeoutError("no answer")]},
        )
        out = tmp_path / "posts.json"

        code = await read(worker, out)

        assert code == EXIT_FAILED
        document = read_json(out)
        assert document["partial"] is True
        assert document["count"] == 100
        assert document["error"] == {
            "offset_id": 151,
            "code": "command_timeout",
            "detail": "no answer",
        }

    async def test_flood_wait_on_a_page_is_waited_once(self, tmp_path: Path) -> None:
        worker = FakeWorker(
            [post(i, "x") for i in range(1, 251)],
            page_script={151: [failure("telegram_flood_wait", "подождите", seconds=40)]},
        )
        out = tmp_path / "posts.json"
        sleep = RecordingSleep()

        code = await read(worker, out, sleep=sleep)

        assert code == EXIT_OK
        assert 41 in sleep.calls
        assert read_json(out)["count"] == 250

    async def test_long_flood_wait_on_a_page_stops_with_partial(self, tmp_path: Path) -> None:
        worker = FakeWorker(
            [post(i, "x") for i in range(1, 251)],
            page_script={151: [failure("telegram_flood_wait", "подождите", seconds=4000)]},
        )
        out = tmp_path / "posts.json"

        code = await read(worker, out)

        assert code == EXIT_FAILED
        assert read_json(out)["error"]["code"] == "telegram_flood_wait"

    async def test_failure_on_first_page_writes_nothing(self, tmp_path: Path) -> None:
        worker = FakeWorker(
            [post(1, "x")], page_script={0: [failure("not_a_channel", "это не канал")]}
        )
        out = tmp_path / "posts.json"
        lines: list[str] = []

        code = await read(worker, out, lines=lines)

        assert code == EXIT_FAILED
        assert not out.exists()
        assert any("ничего не прочитано" in line and "not_a_channel" in line for line in lines)


# --- аргументы ----------------------------------------------------------------
EDIT_ARGS = ["edit", "--account", "main", "--chat", "@templates", "--plan", "/tmp/plan.json"]
DELETE_ARGS = ["delete", "--account", "main", "--chat", "@templates", "--ids", "/tmp/ids.txt"]


class TestParser:
    def test_edit_defaults(self) -> None:
        args = build_parser().parse_args(EDIT_ARGS)
        assert args.apply is False
        assert args.pause == 4.0
        assert args.parse_mode == "none"
        assert args.link_preview is None  # превью как сейчас у поста
        assert args.max_flood_wait == 120

    @pytest.mark.parametrize(
        ("flag", "expected"), [("--link-preview", True), ("--no-link-preview", False)]
    )
    def test_link_preview_flags(self, flag: str, expected: bool) -> None:
        assert build_parser().parse_args([*EDIT_ARGS, flag]).link_preview is expected

    def test_link_preview_flags_are_exclusive(self) -> None:
        with pytest.raises(SystemExit):
            build_parser().parse_args([*EDIT_ARGS, "--link-preview", "--no-link-preview"])

    def test_read_defaults(self) -> None:
        args = build_parser().parse_args(["read", "--account", "main", "--chat", "@t"])
        assert args.page_pause == 1.0
        assert args.max_flood_wait == 120

    def test_read_page_size_is_bounded(self) -> None:
        with pytest.raises(SystemExit):
            build_parser().parse_args(
                ["read", "--account", "main", "--chat", "@t", "--page-size", "501"]
            )

    def test_account_and_chat_are_required(self) -> None:
        with pytest.raises(SystemExit):
            build_parser().parse_args(["read", "--chat", "@templates"])

    def test_delete_defaults(self) -> None:
        args = build_parser().parse_args(DELETE_ARGS)
        assert args.apply is False
        assert args.expect is None
        assert args.batch == 50
        assert args.pause == 3.0

    def test_delete_batch_is_bounded(self) -> None:
        with pytest.raises(SystemExit):
            build_parser().parse_args([*DELETE_ARGS, "--batch", "101"])

    def test_delete_needs_ids_file(self) -> None:
        with pytest.raises(SystemExit):
            build_parser().parse_args(["delete", "--account", "main", "--chat", "@t"])


# --- delete ------------------------------------------------------------------
class TestParseIds:
    def test_json_list(self) -> None:
        assert parse_ids("[5, 3, 5]") == [3, 5]

    def test_text_with_ranges_commas_and_newlines(self) -> None:
        assert parse_ids("12, 15\n20-23;9") == [9, 12, 15, 20, 21, 22, 23]

    @pytest.mark.parametrize(
        "raw", ["", "   ", "[]", "abc", "5-3", "0", "[true]", "[0]", '["1"]', "[1,", "1-2-3"]
    )
    def test_bad_input(self, raw: str) -> None:
        with pytest.raises(IdsError):
            parse_ids(raw)

    def test_too_many(self) -> None:
        with pytest.raises(IdsError):
            parse_ids(f"1-{MAX_DELETE_IDS + 1}")


async def delete(
    worker: FakeWorker,
    ids: list[int],
    tmp_path: Path,
    *,
    lines: list[str] | None = None,
    sleep: RecordingSleep | None = None,
    **options: Any,
) -> int:
    return await run_delete(
        worker,
        account_id=ACCOUNT,
        chat="@templates",
        ids=ids,
        options=DeleteOptions(backup_dir=tmp_path, **options),
        echo=(lines if lines is not None else []).append,
        sleep=sleep or RecordingSleep(),
        now=lambda: NOW,
    )


def delete_results(tmp_path: Path) -> dict[str, Any]:
    return read_json(tmp_path / f"channel_delete_results_{STAMP}.json")


def statuses(report: dict[str, Any]) -> list[tuple[int, str]]:
    return [(item["id"], item["status"]) for item in report["items"]]


class TestDeleteDryRun:
    async def test_lists_posts_and_changes_nothing(self, tmp_path: Path) -> None:
        worker = FakeWorker([post(1, "первый шаблон"), post(2, "второй")])
        lines: list[str] = []

        code = await delete(worker, [1, 2, 9], tmp_path, lines=lines)

        assert code == EXIT_OK
        assert worker.deletes == []
        assert set(worker.posts) == {1, 2}
        assert any("первый шаблон" in line for line in lines)
        assert "Итого: к удалению 2, уже нет в канале 1" in lines
        assert lines[-1].endswith("--apply --expect 2.")
        assert list(tmp_path.iterdir()) == []

    async def test_dry_run_results_file(self, tmp_path: Path) -> None:
        worker = FakeWorker([post(1, "a")])
        out = tmp_path / "dry.json"

        await delete(worker, [1, 2], tmp_path, results_path=out)

        report = read_json(out)
        assert report["kind"] == "channel_delete_results"
        assert report["applied"] is False
        assert statuses(report) == [(1, "dry_run_not_attempted"), (2, "dry_run_missing")]


class TestDeleteApply:
    async def test_expect_mismatch_deletes_nothing(self, tmp_path: Path) -> None:
        worker = FakeWorker([post(1, "a"), post(2, "b")])
        lines: list[str] = []

        code = await delete(worker, [1, 2, 3], tmp_path, apply=True, expect=3, lines=lines)

        assert code == EXIT_INVALID
        assert worker.deletes == []
        assert list(tmp_path.iterdir()) == []
        assert "ничего не удаляю" in lines[-1]

    async def test_backup_first_then_batches_with_pause(self, tmp_path: Path) -> None:
        backup = tmp_path / f"channel_delete_backup_{STAMP}.json"
        seen_backup: list[bool] = []
        worker = FakeWorker(
            [post(i, f"пост {i}") for i in range(1, 6)],
            on_first_delete=lambda: seen_backup.append(backup.exists()),
        )
        sleep = RecordingSleep()

        code = await delete(
            worker,
            [1, 2, 3, 4, 5, 8],
            tmp_path,
            apply=True,
            expect=5,
            batch_size=2,
            pause_seconds=3.0,
            sleep=sleep,
        )

        assert code == EXIT_OK
        assert seen_backup == [True]
        saved = read_json(backup)
        assert saved["kind"] == "channel_delete_backup"
        assert [p["id"] for p in saved["posts"]] == [1, 2, 3, 4, 5]
        assert saved["posts"][0]["text"] == "пост 1"
        assert [c.payload["ids"] for c in worker.deletes] == [[1, 2], [3, 4], [5]]
        assert worker.deletes[0].payload == {
            "chat": MARKED_ID,
            "ids": [1, 2],
            "expires_at": NOW.timestamp() + DELETE_EXPIRY_SECONDS,
        }
        assert sleep.calls == [3.0, 3.0]
        assert worker.posts == {}
        report = delete_results(tmp_path)
        assert report["applied"] is True
        assert report["backup"] == str(backup)
        assert statuses(report) == [(i, "deleted") for i in range(1, 6)] + [(8, "missing")]

    async def test_nothing_to_delete(self, tmp_path: Path) -> None:
        worker = FakeWorker([post(1, "a")])

        code = await delete(worker, [7], tmp_path, apply=True, expect=1)

        assert code == EXIT_OK
        assert worker.deletes == []

    async def test_posts_kept_by_telegram_stop_the_run(self, tmp_path: Path) -> None:
        worker = FakeWorker([post(i, "x") for i in range(1, 5)], undeletable={2})
        lines: list[str] = []

        code = await delete(
            worker, [1, 2, 3, 4], tmp_path, apply=True, expect=4, batch_size=2, lines=lines
        )

        assert code == EXIT_FAILED
        assert len(worker.deletes) == 1
        assert statuses(delete_results(tmp_path)) == [
            (1, "deleted"),
            (2, "not_deleted"),
            (3, "not_attempted"),
            (4, "not_attempted"),
        ]
        assert any("Удалять сообщения" in line for line in lines)

    async def test_vanished_before_command_counts_as_deleted(self, tmp_path: Path) -> None:
        worker = FakeWorker(
            [post(1, "a"), post(2, "b")],
            delete_script={
                1: [success(deleted=[1], already_missing=[2], not_deleted=[], flood_waited=0)]
            },
        )

        code = await delete(worker, [1, 2], tmp_path, apply=True, expect=2)

        assert code == EXIT_OK
        assert statuses(delete_results(tmp_path)) == [(1, "deleted"), (2, "deleted")]

    async def test_short_flood_wait_is_waited_and_retried(self, tmp_path: Path) -> None:
        worker = FakeWorker(
            [post(1, "a")],
            delete_script={1: [failure("telegram_flood_wait", "wait", seconds=40)]},
        )
        sleep = RecordingSleep()

        code = await delete(worker, [1], tmp_path, apply=True, expect=1, sleep=sleep)

        assert code == EXIT_OK
        assert len(worker.deletes) == 2
        assert sleep.calls == [41]
        assert delete_results(tmp_path)["items"][0]["status"] == "deleted"

    async def test_long_flood_wait_stops(self, tmp_path: Path) -> None:
        worker = FakeWorker(
            [post(1, "a"), post(2, "b")],
            delete_script={1: [failure("telegram_flood_wait", "wait", seconds=500)]},
        )

        code = await delete(worker, [1, 2], tmp_path, apply=True, expect=2, batch_size=1)

        assert code == EXIT_FAILED
        assert len(worker.deletes) == 1
        items = delete_results(tmp_path)["items"]
        assert items[0]["status"] == "failed"
        assert items[0]["retry_after"] == 500
        assert items[1]["status"] == "not_attempted"

    async def test_worker_error_stops(self, tmp_path: Path) -> None:
        worker = FakeWorker(
            [post(1, "a")],
            delete_script={1: [failure("message_delete_forbidden", "нет права")]},
        )
        lines: list[str] = []

        code = await delete(worker, [1], tmp_path, apply=True, expect=1, lines=lines)

        assert code == EXIT_FAILED
        assert delete_results(tmp_path)["items"][0]["error_code"] == "message_delete_forbidden"
        assert "message_delete_forbidden" in lines[-1]

    async def test_timeout_is_resolved_by_rereading(self, tmp_path: Path) -> None:
        worker = FakeWorker([post(1, "a"), post(2, "b")], delete_script={1: [APPLY_THEN_TIMEOUT]})

        code = await delete(worker, [1, 2], tmp_path, apply=True, expect=2, batch_size=1)

        assert code == EXIT_OK
        # Первую пачку не переотправили: её исход узнали чтением.
        assert [c.payload["ids"] for c in worker.deletes] == [[1], [2]]
        assert statuses(delete_results(tmp_path)) == [(1, "deleted"), (2, "deleted")]

    async def test_timeout_and_failed_reread_is_unknown(self, tmp_path: Path) -> None:
        worker = FakeWorker(
            [post(1, "a")],
            delete_script={1: [CommandTimeoutError("no answer")]},
            read_failures={1: WorkerUnavailableError("down")},
        )

        code = await delete(worker, [1], tmp_path, apply=True, expect=1)

        assert code == EXIT_FAILED
        item = delete_results(tmp_path)["items"][0]
        assert item["status"] == "unknown"
        assert item["error_code"] == "delete_unconfirmed"


# --- media -------------------------------------------------------------------
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16


def media_plan(tmp_path: Path, entries: list[dict[str, Any]]) -> Path:
    plan = tmp_path / "media.json"
    plan.write_text(json.dumps(entries), encoding="utf-8")
    return plan


class TestMediaPlan:
    def test_relative_paths_from_plan_folder(self, tmp_path: Path) -> None:
        (tmp_path / "cards").mkdir()
        (tmp_path / "cards" / "1.png").write_bytes(PNG)

        items = load_media_plan(media_plan(tmp_path, [{"id": 1, "file": "cards/1.png"}]))

        assert [(i.id, i.path.name, i.kind, i.data) for i in items] == [(1, "1.png", "png", PNG)]

    def test_collects_all_problems(self, tmp_path: Path) -> None:
        (tmp_path / "a.gif").write_bytes(b"GIF89a")
        (tmp_path / "ok.png").write_bytes(PNG)
        plan = media_plan(
            tmp_path,
            [
                {"id": 1, "file": "missing.png"},
                {"id": 2, "file": "a.gif"},
                {"id": 0, "file": "ok.png"},
                {"id": 3},
                {"id": 4, "file": "ok.png"},
                {"id": 4, "file": "ok.png"},
            ],
        )

        with pytest.raises(PlanError) as caught:
            load_media_plan(plan)

        text = str(caught.value)
        for fragment in ("#1", "#2", "элемент 3", "#3", "элемент 6"):
            assert fragment in text


async def media(
    worker: FakeWorker,
    tmp_path: Path,
    ids: list[int],
    *,
    lines: list[str] | None = None,
    **opts: Any,
) -> int:
    for post_id in ids:
        (tmp_path / f"{post_id}.png").write_bytes(PNG)
    plan = load_media_plan(media_plan(tmp_path, [{"id": i, "file": f"{i}.png"} for i in ids]))
    return await run_media(
        worker,
        account_id=ACCOUNT,
        chat="@templates",
        plan=plan,
        options=MediaOptions(backup_dir=tmp_path, **opts),
        echo=(lines if lines is not None else []).append,
        sleep=RecordingSleep(),
        now=lambda: NOW,
    )


class TestRunMedia:
    async def test_dry_run_checks_posts_are_photos(self, tmp_path: Path) -> None:
        worker = FakeWorker([post(1, "a", media_type="photo"), post(2, "b")])
        lines: list[str] = []

        code = await media(worker, tmp_path, [1, 2, 3], lines=lines)

        assert code == EXIT_INVALID
        assert worker.media == []
        assert "Итого: к замене 1, с ошибками 2" in lines

    async def test_apply_sends_image_with_pause(self, tmp_path: Path) -> None:
        worker = FakeWorker([post(1, "a", media_type="photo"), post(2, "b", media_type="photo")])

        code = await media(worker, tmp_path, [1, 2], apply=True)

        assert code == EXIT_OK
        assert [c.payload["message_id"] for c in worker.media] == [1, 2]
        assert worker.media[0].payload["chat"] == MARKED_ID
        assert base64.b64decode(worker.media[0].payload["image_b64"]) == PNG
        report = read_json(tmp_path / f"channel_media_results_{STAMP}.json")
        assert [(i["id"], i["status"]) for i in report["items"]] == [
            (1, "replaced"),
            (2, "replaced"),
        ]

    async def test_error_stops_the_run(self, tmp_path: Path) -> None:
        worker = FakeWorker(
            [post(1, "a", media_type="photo"), post(2, "b", media_type="photo")],
            media_script={1: [failure("bad_image", "не смог")]},
        )

        code = await media(worker, tmp_path, [1, 2], apply=True)

        assert code == EXIT_FAILED
        assert len(worker.media) == 1
        report = read_json(tmp_path / f"channel_media_results_{STAMP}.json")
        assert [(i["id"], i["status"]) for i in report["items"]] == [
            (1, "failed"),
            (2, "not_attempted"),
        ]

    async def test_timeout_is_unknown_and_stops(self, tmp_path: Path) -> None:
        worker = FakeWorker(
            [post(1, "a", media_type="photo"), post(2, "b", media_type="photo")],
            media_script={1: [CommandTimeoutError("no answer")]},
        )

        code = await media(worker, tmp_path, [1, 2], apply=True)

        assert code == EXIT_FAILED
        assert len(worker.media) == 1
