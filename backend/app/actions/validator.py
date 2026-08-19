"""Проверка ответа перед отправкой (ТЗ §19).

Последний рубеж между моделью и живым чатом. Правило простое: не прошло
проверку — не отправляем. Ответ не «исправляется» и не «дотягивается» до
приемлемого: молчание и передача человеку безопаснее, чем отправка чего-то
почти правильного от лица настоящего аккаунта.

Каждая проверка отдельная и возвращает причину отказа. Причина сохраняется в
`actions.validation` и попадает в панель: оператор должен видеть, почему ответ
не ушёл, а не догадываться.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from app.core.logging import get_logger

logger = get_logger(__name__)

# Признаки того, что модель заговорила о себе или пересказывает инструкции.
_LEAKED_INSTRUCTIONS = (
    "как языковая модель",
    "as an ai language model",
    "я — искусственный интеллект",
    "я искусственный интеллект",
    "system prompt",
    "системный промпт",
    "мои инструкции",
)

_PLACEHOLDER_PATTERNS = (
    re.compile(r"\{\{?\s*[a-z_]+\s*\}?\}", re.IGNORECASE),
    re.compile(r"\[(?:вставить|указать|ваш[аи]?|insert|your)\b[^\]]*\]", re.IGNORECASE),
    re.compile(r"\blorem ipsum\b", re.IGNORECASE),
)

_URL_RE = re.compile(r"https?://\S+", re.IGNORECASE)


@dataclass(frozen=True, slots=True)
class ValidationContext:
    """Всё, что нужно проверкам."""

    text: str
    refused: bool = False
    used_knowledge: bool = False
    require_grounding: bool = False
    max_length: int | None = None
    min_length: int = 2
    banned_phrases: tuple[str, ...] = ()
    required_links: tuple[str, ...] = ()
    recent_replies: tuple[str, ...] = ()
    ai_replies_in_row: int = 0
    max_replies_in_row: int = 3


@dataclass(frozen=True, slots=True)
class CheckResult:
    name: str
    passed: bool
    reason: str | None = None


@dataclass(frozen=True, slots=True)
class ValidationVerdict:
    passed: bool
    checks: tuple[CheckResult, ...] = field(default=())

    @property
    def failures(self) -> tuple[CheckResult, ...]:
        return tuple(check for check in self.checks if not check.passed)

    @property
    def first_failure(self) -> str | None:
        failures = self.failures
        return failures[0].reason if failures else None

    def to_payload(self) -> dict[str, Any]:
        """Представление для колонки actions.validation и для панели."""
        return {
            "passed": self.passed,
            "checks": [
                {"name": check.name, "passed": check.passed, "reason": check.reason}
                for check in self.checks
            ],
        }

    def __bool__(self) -> bool:
        return self.passed


# Проверка — обычная функция: имя приходит в самом результате, поэтому
# отдельный протокол с атрибутом name только мешал бы.
Check = Callable[[ValidationContext], CheckResult]


def _result(name: str, ok: bool, reason: str | None = None) -> CheckResult:
    return CheckResult(name=name, passed=ok, reason=None if ok else reason)


def check_not_refused(context: ValidationContext) -> CheckResult:
    return _result(
        "not_refused", not context.refused, "модель отказалась отвечать по имеющимся данным"
    )


def check_not_empty(context: ValidationContext) -> CheckResult:
    return _result("not_empty", bool(context.text.strip()), "ответ пустой")


def check_length(context: ValidationContext) -> CheckResult:
    text = context.text.strip()
    if len(text) < context.min_length:
        return _result("length", False, f"ответ короче {context.min_length} символов")
    if context.max_length is not None and len(text) > context.max_length:
        return _result("length", False, f"ответ длиннее допустимых {context.max_length} символов")
    return _result("length", True)


def check_no_banned_phrases(context: ValidationContext) -> CheckResult:
    lowered = context.text.lower()
    for phrase in context.banned_phrases:
        if phrase.strip() and phrase.lower() in lowered:
            return _result("banned_phrases", False, f"запрещённая фраза: {phrase}")
    return _result("banned_phrases", True)


def check_no_leaked_instructions(context: ValidationContext) -> CheckResult:
    lowered = context.text.lower()
    for marker in _LEAKED_INSTRUCTIONS:
        if marker in lowered:
            return _result("no_leaked_instructions", False, "ответ раскрывает инструкции")
    return _result("no_leaked_instructions", True)


def check_no_placeholders(context: ValidationContext) -> CheckResult:
    """Незаполненный шаблон в ответе — верный признак выдумки."""
    for pattern in _PLACEHOLDER_PATTERNS:
        if pattern.search(context.text):
            return _result("no_placeholders", False, "в ответе остался незаполненный шаблон")
    return _result("no_placeholders", True)


def check_required_links(context: ValidationContext) -> CheckResult:
    if not context.required_links:
        return _result("required_links", True)
    for link in context.required_links:
        if link not in context.text:
            return _result("required_links", False, f"в ответе нет обязательной ссылки: {link}")
    return _result("required_links", True)


def check_grounding(context: ValidationContext) -> CheckResult:
    """Сценарий требует опоры на базу знаний — значит, она должна быть."""
    if not context.require_grounding:
        return _result("grounding", True)
    if not context.used_knowledge:
        return _result("grounding", False, "ответ не опирается на базу знаний")
    if _URL_RE.search(context.text) and not context.required_links:
        # Ссылка, которой нет в источниках, — самая заметная форма выдумки.
        return _result("grounding", False, "в ответе ссылка, которой нет в базе знаний")
    return _result("grounding", True)


def check_no_duplicate(context: ValidationContext) -> CheckResult:
    normalized = _normalize(context.text)
    for previous in context.recent_replies:
        if _normalize(previous) == normalized:
            return _result("no_duplicate", False, "такой ответ уже отправлялся")
    return _result("no_duplicate", True)


def check_no_self_loop(context: ValidationContext) -> CheckResult:
    """Ограничение подряд идущих ответов без реплики собеседника (ТЗ §9)."""
    if context.ai_replies_in_row >= context.max_replies_in_row:
        return _result(
            "no_self_loop",
            False,
            f"подряд отправлено {context.ai_replies_in_row} ответов без ответа собеседника",
        )
    return _result("no_self_loop", True)


DEFAULT_CHECKS: tuple[Check, ...] = (
    check_not_refused,
    check_not_empty,
    check_length,
    check_no_banned_phrases,
    check_no_leaked_instructions,
    check_no_placeholders,
    check_required_links,
    check_grounding,
    check_no_duplicate,
    check_no_self_loop,
)


class ReplyValidator:
    """Прогоняет ответ через все проверки.

    Проверки выполняются целиком, даже если первая уже провалилась: оператору
    полезно видеть все причины сразу, а не устранять их по одной.
    """

    def __init__(self, checks: tuple[Check, ...] = DEFAULT_CHECKS) -> None:
        self._checks = checks

    def validate(self, context: ValidationContext) -> ValidationVerdict:
        results = tuple(check(context) for check in self._checks)
        verdict = ValidationVerdict(passed=all(result.passed for result in results), checks=results)
        if not verdict.passed:
            logger.info(
                "reply_validation_failed",
                failed=[result.name for result in verdict.failures],
                reason=verdict.first_failure,
            )
        return verdict


def _normalize(text: str) -> str:
    return re.sub(r"\s+", " ", text.strip().lower())
