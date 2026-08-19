"""Инъекция времени.

Cooldown, аренда аккаунтов и дедупликация завязаны на часы. Прямые вызовы
``datetime.now()`` делают эти механизмы непроверяемыми, поэтому время берётся
из Clock, который в тестах подменяется на управляемый.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta
from typing import Protocol


class Clock(Protocol):
    def now(self) -> datetime:
        """Текущее время в UTC (timezone-aware)."""

    def monotonic(self) -> float:
        """Монотонные секунды — для измерения длительностей."""


class SystemClock:
    __slots__ = ()

    def now(self) -> datetime:
        return datetime.now(UTC)

    def monotonic(self) -> float:
        return time.monotonic()


class FrozenClock:
    """Управляемые часы для тестов."""

    __slots__ = ("_monotonic", "_now")

    def __init__(self, start: datetime | None = None) -> None:
        self._now = start or datetime(2026, 1, 1, tzinfo=UTC)
        self._monotonic = 0.0

    def now(self) -> datetime:
        return self._now

    def monotonic(self) -> float:
        return self._monotonic

    def advance(self, seconds: float) -> None:
        self._now += timedelta(seconds=seconds)
        self._monotonic += seconds


_clock: Clock = SystemClock()


def get_clock() -> Clock:
    return _clock


def set_clock(clock: Clock) -> Clock:
    """Подменяет глобальные часы, возвращая прежние (для восстановления)."""
    global _clock
    previous = _clock
    _clock = clock
    return previous


def utcnow() -> datetime:
    return _clock.now()
