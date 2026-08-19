from __future__ import annotations

from datetime import UTC, datetime

from app.core.clock import FrozenClock, SystemClock, get_clock, set_clock, utcnow


def test_system_clock_is_timezone_aware() -> None:
    now = SystemClock().now()
    assert now.tzinfo is not None
    assert now.utcoffset() == UTC.utcoffset(None)


def test_frozen_clock_does_not_move_on_its_own() -> None:
    clock = FrozenClock(datetime(2026, 5, 1, tzinfo=UTC))
    assert clock.now() == clock.now()
    assert clock.monotonic() == 0.0


def test_frozen_clock_advances_both_scales() -> None:
    clock = FrozenClock(datetime(2026, 5, 1, tzinfo=UTC))
    clock.advance(90)
    assert clock.now() == datetime(2026, 5, 1, 0, 1, 30, tzinfo=UTC)
    assert clock.monotonic() == 90.0


def test_set_clock_swaps_global_source() -> None:
    frozen = FrozenClock(datetime(2030, 1, 1, tzinfo=UTC))
    previous = set_clock(frozen)
    try:
        assert utcnow() == datetime(2030, 1, 1, tzinfo=UTC)
        assert get_clock() is frozen
    finally:
        set_clock(previous)
    assert get_clock() is previous
