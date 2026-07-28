"""Injectable time source.

Every timestamp decision in the trading domain — data freshness, order expiry,
holding periods, daily-loss windows, backtest advancement — goes through a
`Clock`. Nothing calls `datetime.now()` directly.

Why this matters more here than elsewhere: a backtester that reads the wall
clock is a backtester with look-ahead bias. Making the clock an explicit
dependency means the *same* strategy, risk, and OMS code runs in backtest,
paper, and live with only the clock swapped — which is the property the whole
"no leakage" claim rests on.

All clocks return timezone-aware UTC datetimes. Naive datetimes are rejected
everywhere in this package (`contracts.ensure_utc`).
"""

from __future__ import annotations

import threading
from datetime import datetime, timedelta, timezone


class Clock:
    """Wall-clock time. The production implementation."""

    def now(self) -> datetime:
        return datetime.now(timezone.utc)

    def monotonic_ns(self) -> int:
        """Monotonic counter for latency measurement (never for timestamps)."""
        import time
        return time.monotonic_ns()

    def __repr__(self) -> str:  # pragma: no cover - trivial
        return "Clock()"


class FixedClock(Clock):
    """A clock the caller advances by hand.

    Used by the backtester and by every test that asserts time-dependent
    behaviour (staleness, daily-loss rollover, holding-period expiry). Thread
    safe because the OMS and monitor may read it from another thread.
    """

    def __init__(self, start: datetime):
        if start.tzinfo is None:
            raise ValueError("FixedClock requires a timezone-aware datetime")
        self._now = start.astimezone(timezone.utc)
        self._mono = 0
        self._lock = threading.Lock()

    def now(self) -> datetime:
        with self._lock:
            return self._now

    def monotonic_ns(self) -> int:
        with self._lock:
            return self._mono

    def advance(self, delta: timedelta) -> datetime:
        """Move forward. Refuses to go backwards — time travel breaks causality
        checks that the whole leakage story depends on."""
        if delta < timedelta(0):
            raise ValueError("clock cannot move backwards")
        with self._lock:
            self._now = self._now + delta
            self._mono += int(delta.total_seconds() * 1_000_000_000)
            return self._now

    def set(self, when: datetime) -> datetime:
        if when.tzinfo is None:
            raise ValueError("FixedClock.set requires a timezone-aware datetime")
        when = when.astimezone(timezone.utc)
        with self._lock:
            if when < self._now:
                raise ValueError("clock cannot move backwards")
            self._mono += max(0, int((when - self._now).total_seconds() * 1e9))
            self._now = when
            return self._now

    def __repr__(self) -> str:  # pragma: no cover - trivial
        return f"FixedClock({self._now.isoformat()})"


_default = Clock()


def default_clock() -> Clock:
    """The process-wide wall clock.

    Deliberately a plain module function rather than a mutable global that
    tests monkeypatch: components take a `clock` argument, and this is only the
    fallback for callers that genuinely want wall time.
    """
    return _default


__all__ = ["Clock", "FixedClock", "default_clock"]
