"""Shared helpers for counters and rates."""

from __future__ import annotations

from collections.abc import Mapping


class RateMeter:
    """Turn cumulative counters into per-second rates.

    Counter resets (or interfaces disappearing) only reset the baseline; they
    never produce negative rates.  The first observation and observations
    closer than ``min_dt`` return ``None``.
    """

    def __init__(self, min_dt: float = 0.2) -> None:
        self._min_dt = min_dt
        self._previous: dict[str, float] = {}
        self._at: float | None = None

    def update(self, counters: Mapping[str, float], now: float) -> dict[str, float | None]:
        previous = self._previous
        previous_at = self._at
        rates: dict[str, float | None] = {}

        if previous_at is None:
            rates = {key: None for key in counters}
        else:
            dt = now - previous_at
            for key, current in counters.items():
                old = previous.get(key)
                if old is None or dt < self._min_dt or current < old:
                    rates[key] = None
                else:
                    rates[key] = (current - old) / dt

        self._previous = dict(counters)
        self._at = now
        return rates

    def reset(self) -> None:
        self._previous.clear()
        self._at = None
