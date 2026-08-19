"""Поиск ключевых слов (ТЗ §10).

Отдельный модуль, потому что это самая горячая часть конвейера: через него
проходит каждое сообщение отслеживаемых чатов, и он же решает, стоит ли вообще
тратить деньги на AI. Дешёвые проверки идут первыми и отсекают очевидно
неподходящие сообщения до обращения к модели.

Регулярные выражения компилируются один раз на набор слов и кешируются.
Компиляция на каждое сообщение — заметная часть времени обработки при
нескольких десятках правил.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import StrEnum
from functools import lru_cache
from typing import Any


class MatchMode(StrEnum):
    SUBSTRING = "substring"
    WHOLE_WORD = "whole_word"
    EXACT = "exact"
    REGEX = "regex"


@dataclass(frozen=True, slots=True)
class Hit:
    term: str
    start: int
    end: int


@dataclass(frozen=True, slots=True)
class KeywordSpec:
    """Условие по словам из правила."""

    terms: tuple[str, ...] = ()
    exclude: tuple[str, ...] = ()
    mode: MatchMode = MatchMode.SUBSTRING
    case_sensitive: bool = False

    @property
    def is_empty(self) -> bool:
        return not self.terms and not self.exclude

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> KeywordSpec:
        data = data or {}
        raw_mode = str(data.get("mode", MatchMode.SUBSTRING))
        try:
            mode = MatchMode(raw_mode)
        except ValueError as exc:
            raise ValueError(f"unknown keyword match mode: {raw_mode}") from exc

        return cls(
            terms=tuple(_clean(data.get("terms"))),
            exclude=tuple(_clean(data.get("exclude"))),
            mode=mode,
            case_sensitive=bool(data.get("case_sensitive", False)),
        )


@dataclass
class KeywordMatcher:
    """Проверяет текст на соответствие набору слов."""

    spec: KeywordSpec
    _include: re.Pattern[str] | None = field(default=None, init=False, repr=False)
    _exclude: re.Pattern[str] | None = field(default=None, init=False, repr=False)

    def __post_init__(self) -> None:
        self._include = _compile(self.spec.terms, self.spec.mode, self.spec.case_sensitive)
        # Исключения всегда ищутся как отдельные слова или подстроки: писать
        # регулярку в списке исключений незачем, а ошибиться в ней — легко.
        exclude_mode = (
            MatchMode.WHOLE_WORD if self.spec.mode is MatchMode.WHOLE_WORD else MatchMode.SUBSTRING
        )
        self._exclude = _compile(self.spec.exclude, exclude_mode, self.spec.case_sensitive)

    def find(self, text: str) -> list[Hit]:
        """Все совпадения. Пустой список — и когда не совпало, и когда исключено."""
        if not text:
            return []
        if self._exclude is not None and self._exclude.search(text):
            return []
        if self._include is None:
            # Слов нет, но и исключения не сработали: условие выполнено пусто.
            return []
        return [
            Hit(term=match.group(0), start=match.start(), end=match.end())
            for match in self._include.finditer(text)
        ]

    def matches(self, text: str) -> bool:
        if self.spec.is_empty:
            return True
        if not text:
            return False
        if self._exclude is not None and self._exclude.search(text):
            return False
        if self._include is None:
            return True
        return self._include.search(text) is not None


def _clean(values: Any) -> list[str]:
    if not isinstance(values, (list, tuple)):
        return []
    return [str(value).strip() for value in values if str(value).strip()]


def _compile(
    terms: tuple[str, ...], mode: MatchMode, case_sensitive: bool
) -> re.Pattern[str] | None:
    if not terms:
        return None
    return _compile_cached(terms, mode, case_sensitive)


@lru_cache(maxsize=512)
def _compile_cached(
    terms: tuple[str, ...], mode: MatchMode, case_sensitive: bool
) -> re.Pattern[str]:
    flags = re.UNICODE | (0 if case_sensitive else re.IGNORECASE)

    if mode is MatchMode.REGEX:
        parts = [f"(?:{term})" for term in terms]
    elif mode is MatchMode.WHOLE_WORD:
        # \b не работает на границе «слово—кириллица» одинаково во всех случаях,
        # поэтому границу задаём явно через lookaround по словесным символам.
        parts = [rf"(?<!\w){re.escape(term)}(?!\w)" for term in terms]
    elif mode is MatchMode.EXACT:
        parts = [rf"\A\s*{re.escape(term)}\s*\Z" for term in terms]
    else:
        parts = [re.escape(term) for term in terms]

    try:
        return re.compile("|".join(parts), flags)
    except re.error as exc:
        raise ValueError(f"invalid keyword pattern: {exc}") from exc


def compile_regex(pattern: str | None) -> re.Pattern[str] | None:
    """Компилирует regex правила. Невалидный шаблон — ошибка конфигурации."""
    if not pattern:
        return None
    try:
        return _compile_regex_cached(pattern)
    except re.error as exc:
        raise ValueError(f"invalid rule regex: {exc}") from exc


@lru_cache(maxsize=256)
def _compile_regex_cached(pattern: str) -> re.Pattern[str]:
    return re.compile(pattern, re.IGNORECASE | re.UNICODE)
