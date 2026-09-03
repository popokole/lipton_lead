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
from difflib import SequenceMatcher
from enum import StrEnum
from functools import lru_cache
from typing import Any

DEFAULT_FUZZY_THRESHOLD = 0.8
_WORD_RE = re.compile(r"\w+", re.UNICODE)


class MatchMode(StrEnum):
    SUBSTRING = "substring"
    WHOLE_WORD = "whole_word"
    EXACT = "exact"
    REGEX = "regex"
    FUZZY = "fuzzy"


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
    # Только для mode=FUZZY: минимальное сходство слова с термом (0..1).
    fuzzy_threshold: float = DEFAULT_FUZZY_THRESHOLD

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

        try:
            fuzzy_threshold = float(data.get("fuzzy_threshold", DEFAULT_FUZZY_THRESHOLD))
        except (TypeError, ValueError):
            fuzzy_threshold = DEFAULT_FUZZY_THRESHOLD

        return cls(
            terms=tuple(_clean(data.get("terms"))),
            exclude=tuple(_clean(data.get("exclude"))),
            mode=mode,
            case_sensitive=bool(data.get("case_sensitive", False)),
            fuzzy_threshold=fuzzy_threshold,
        )


@dataclass
class KeywordMatcher:
    """Проверяет текст на соответствие набору слов."""

    spec: KeywordSpec
    _include: re.Pattern[str] | None = field(default=None, init=False, repr=False)
    _exclude: re.Pattern[str] | None = field(default=None, init=False, repr=False)

    def __post_init__(self) -> None:
        # FUZZY не компилируется в regex: сравнение идёт по словам через
        # SequenceMatcher, чтобы ловить опечатки в длинных характерных корнях.
        if self.spec.mode is not MatchMode.FUZZY:
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
        if self.spec.mode is MatchMode.FUZZY:
            return _find_fuzzy(text, self.spec.terms, self.spec.fuzzy_threshold)
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
        if self.spec.mode is MatchMode.FUZZY:
            return bool(_find_fuzzy(text, self.spec.terms, self.spec.fuzzy_threshold))
        if self._include is None:
            return True
        return self._include.search(text) is not None


def _find_fuzzy(text: str, terms: tuple[str, ...], threshold: float) -> list[Hit]:
    """Сравнивает каждое слово текста с каждым термом (регистронезависимо).

    Только словá, не подстроки: иначе короткие термы ловят опечатку в
    середине случайного слова. Позиции — по исходному (не lowercased) тексту.
    """
    if not terms:
        return []
    lowered_terms = tuple(term.lower() for term in terms)
    hits: list[Hit] = []
    for match in _WORD_RE.finditer(text):
        word = match.group(0).lower()
        for term in lowered_terms:
            if SequenceMatcher(None, term, word).ratio() >= threshold:
                hits.append(Hit(term=match.group(0), start=match.start(), end=match.end()))
                break
    return hits


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
