"""Collector protocol."""

from __future__ import annotations

from typing import Protocol


class Collector(Protocol):
    name: str
    interval_s: float

    def collect(self) -> object:
        """Read hardware once. Must not block for long and may raise."""
