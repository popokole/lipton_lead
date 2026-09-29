"""CLI правки постов канала (app/tools/channel_posts.py).

Воркер подменён: CLI общается с ним только через Command/CommandResult,
поэтому подделка отвечает на READ_CHANNEL_POSTS и EDIT_MESSAGE по сценарию и
записывает всё, что ей прислали.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from app.bus.messages import Command, CommandResult, CommandType
from app.core.errors import CommandTimeoutError, WorkerUnavailableError
from app.tools.channel_posts import (
    EXIT_FAILED,
    EXIT_INVALID,
    EXIT_OK,
    EditOptions,
    PlanError,
    PlanItem,
    build_parser,
    check_item,
    diff_summary,
    load_plan,
    parse_plan,
    run_edit,
    run_read,
)

ACCOUNT = uuid.UUID("11111111-2222-3333-4444-555555555555")
CHAT_INFO = {"id": 1234567890, "title": "Шаблоны", "username": "templates", "type": "channel"}
NOW = datetime(2026, 9, 30, 10, 0, 0, tzinfo=UTC)


def post(
    post_id: int,
    text: str,
    *,
    media_type: str | None = None,
    formatting: bool = False,
    html: str | None = None,
    can_edit: bool | None = True,
) -> dict[str, Any]:
    has_media = media_type is not None and media_type != "webpage"
    return {
        "id": post_id,
        "date": "2026-09-01T12:30:00+00:00",
        "text": text,
        "html": html,
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
    """Отвечает как воркер. edit_script: id → очередь ответов (CommandResult или исключение)."""

    def __init__(
        self,
        posts: list[dict[str, Any]] | None = None,
        *,
        edit_script: dict[int, list[Any]] | None = None,
        on_first_edit: Callable[[], None] | None = None,
        partial_at_offset: int | None = None,
    ) -> None:
        self.posts = {p["id"]: p for p in posts or []}
        self.edit_script = edit_script or {}
        self.on_first_edit = on_first_edit
        self.partial_at_offset = partial_at_offset
        self.commands: list[Command] = []

    @property
    def edits(self) -> list[Command]:
        return [c for c in self.commands if c.type is CommandType.EDIT_MESSAGE]

    @property
    def reads(self) -> list[Command]:
        return [c for c in self.commands if c.type is CommandType.READ_CHANNEL_POSTS]

    async def __call__(self, command: Command, timeout_seconds: float) -> CommandResult:
        self.commands.append(command)
        if command.type is CommandType.READ_CHANNEL_POSTS:
            return self._read(command)
        if command.type is CommandType.EDIT_MESSAGE:
            if len(self.edits) == 1 and self.on_first_edit is not None:
                self.on_first_edit()
            script = self.edit_script.get(command.payload["message_id"]) or []
            if script:
                answer = script.pop(0)
                if isinstance(answer, BaseException):
                    raise answer
                return answer
            return CommandResult.success(command.id, edited=True, no_change=False, flood_waited=0)
        raise AssertionError(f"unexpected command {command.type}")

    def _read(self, command: Command) -> CommandResult:
        payload = command.payload
        if "ids" in payload:
            found = [self.posts[i] for i in payload["ids"] if i in self.posts]
            missing = [i for i in payload["ids"] if i not in self.posts]
            return CommandResult.success(
                command.id,
                chat=CHAT_INFO,
                posts=sorted(found, key=lambda p: p["id"]),
                missing_ids=missing,
            )
        offset, limit = payload["offset_id"], payload["limit"]
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


def read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


# --- план ---------------------------------------------------------------------
class TestParsePlan:
    def test_list_of_items(self) -> None:
        plan = parse_plan([{"id": 3, "text": "три"}, {"id": 1, "text": ""}])
        assert plan == [PlanItem(3, "три"), PlanItem(1, "")]

    def test_read_output_is_accepted_as_plan(self) -> None:
        document = {"kind": "channel_posts", "posts": [post(5, "пять"), post(6, "шесть")]}
        assert parse_plan(document) == [PlanItem(5, "пять"), PlanItem(6, "шесть")]

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

    def test_unchanged_html_matches_current_html(self) -> None:
        current = post(1, "жирный", formatting=True, html="<strong>жирный</strong>")
        check = check_item(PlanItem(1, "<strong>жирный</strong>"), current, "html")
        assert check.status == "no_change"

    def test_formatting_loss_is_warned(self) -> None:
        current = post(1, "жирный", formatting=True, html="<strong>жирный</strong>")
        check = check_item(PlanItem(1, "другой"), current, None)
        assert check.status == "ok"
        assert "форматирование" in check.warnings[0]

    def test_no_warning_with_html_mode(self) -> None:
        current = post(1, "жирный", formatting=True, html="<strong>жирный</strong>")
        check = check_item(PlanItem(1, "<b>другой</b>"), current, "html")
        assert check.warnings == []
        assert check.new_length == 6

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

    async def test_ids_are_read_in_chunks(self, tmp_path: Path) -> None:
        posts = [post(i, f"p{i}") for i in range(1, 251)]
        worker = FakeWorker(posts)

        await edit(worker, [PlanItem(i, f"n{i}") for i in range(1, 251)], tmp_path)

        assert [len(c.payload["ids"]) for c in worker.reads] == [100, 100, 50]


# --- edit: --apply ------------------------------------------------------------
class TestEditApply:
    async def test_backup_first_then_edits_with_pause(self, tmp_path: Path) -> None:
        backup = tmp_path / "channel_backup_20260930_100000.json"
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
            "chat": "@templates",
            "message_id": 3,
            "text": "ТРИ",
            "parse_mode": None,
            "link_preview": False,
        }
        assert sleep.calls == [4.0]  # пауза только между правками

        results = read_json(tmp_path / "channel_edit_results_20260930_100000.json")
        assert results["applied"] is True
        assert results["backup"] == str(backup)
        assert [(i["id"], i["status"]) for i in results["items"]] == [
            (3, "edited"),
            (2, "skipped_unchanged"),
            (1, "edited"),
        ]
        assert results["summary"] == {"edited": 2, "skipped_unchanged": 1}

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
        results = read_json(tmp_path / "channel_edit_results_20260930_100000.json")
        assert [(i["id"], i["status"]) for i in results["items"]] == [
            (1, "edited"),
            (2, "failed"),
            (3, "not_attempted"),
        ]
        assert results["items"][1]["error_code"] == "chat_admin_required"
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
        results = read_json(tmp_path / "channel_edit_results_20260930_100000.json")
        assert results["items"][0]["status"] == "edited"
        assert results["items"][0]["flood_waited"] == 40

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
        results = read_json(tmp_path / "channel_edit_results_20260930_100000.json")
        assert results["items"][0]["retry_after"] == 900
        assert results["items"][1]["status"] == "not_attempted"

    async def test_timeout_is_retried_and_counts_as_edited(self, tmp_path: Path) -> None:
        worker = FakeWorker(
            [post(1, "a")],
            edit_script={
                1: [CommandTimeoutError("no answer"), success(edited=False, no_change=True)]
            },
        )

        code = await edit(worker, [PlanItem(1, "A")], tmp_path, apply=True)

        assert code == EXIT_OK
        results = read_json(tmp_path / "channel_edit_results_20260930_100000.json")
        assert results["items"][0]["status"] == "edited"

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


# --- read ---------------------------------------------------------------------
class TestRead:
    async def test_reads_everything_oldest_first(self, tmp_path: Path) -> None:
        worker = FakeWorker([post(i, f"Привет {i}") for i in range(1, 251)])
        out = tmp_path / "posts.json"
        lines: list[str] = []

        code = await run_read(
            worker,
            account_id=ACCOUNT,
            chat="@templates",
            out_path=out,
            echo=lines.append,
            now=lambda: NOW,
        )

        assert code == EXIT_OK
        assert [(c.payload["limit"], c.payload["offset_id"]) for c in worker.reads] == [
            (100, 0),
            (100, 151),
            (100, 51),
        ]
        raw = out.read_text(encoding="utf-8")
        assert "Привет 1" in raw  # UTF-8 как есть, без \\u-экранирования
        document = json.loads(raw)
        assert document["count"] == 250
        assert [p["id"] for p in document["posts"]] == list(range(1, 251))
        assert document["chat_info"] == CHAT_INFO
        assert document["partial"] is False
        assert any("Прочитано постов: 250 (id 1…250)" in line for line in lines)

    async def test_limit_keeps_newest_posts(self, tmp_path: Path) -> None:
        worker = FakeWorker([post(i, "x") for i in range(1, 251)])
        out = tmp_path / "posts.json"

        await run_read(
            worker,
            account_id=ACCOUNT,
            chat="@templates",
            out_path=out,
            limit=120,
            echo=lambda _line: None,
        )

        assert [c.payload["limit"] for c in worker.reads] == [100, 20]
        ids = [p["id"] for p in read_json(out)["posts"]]
        assert ids == list(range(131, 251))

    async def test_partial_read_is_saved_and_flagged(self, tmp_path: Path) -> None:
        worker = FakeWorker([post(i, "x") for i in range(1, 251)], partial_at_offset=151)
        out = tmp_path / "posts.json"
        lines: list[str] = []

        code = await run_read(
            worker, account_id=ACCOUNT, chat="@templates", out_path=out, echo=lines.append
        )

        assert code == EXIT_FAILED
        document = read_json(out)
        assert document["partial"] is True
        assert document["count"] == 100
        assert any("выгрузка неполная" in line for line in lines)


# --- аргументы ----------------------------------------------------------------
class TestParser:
    def test_edit_defaults(self) -> None:
        args = build_parser().parse_args(
            ["edit", "--account", "main", "--chat", "@templates", "--plan", "/tmp/plan.json"]
        )
        assert args.apply is False
        assert args.pause == 4.0
        assert args.parse_mode == "none"
        assert args.link_preview is False

    def test_read_page_size_is_bounded(self) -> None:
        with pytest.raises(SystemExit):
            build_parser().parse_args(
                ["read", "--account", "main", "--chat", "@t", "--page-size", "501"]
            )

    def test_account_and_chat_are_required(self) -> None:
        with pytest.raises(SystemExit):
            build_parser().parse_args(["read", "--chat", "@templates"])
