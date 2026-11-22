from __future__ import annotations

from datetime import UTC, datetime

import pytest

from app.config import AppConfig


@pytest.fixture
def now() -> datetime:
    return datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)


@pytest.fixture
def cfg() -> AppConfig:
    return AppConfig()


class FakeClock:
    def __init__(self, value: float = 1000.0) -> None:
        self.value = value

    def __call__(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += seconds


@pytest.fixture
def fake_clock() -> FakeClock:
    return FakeClock()
