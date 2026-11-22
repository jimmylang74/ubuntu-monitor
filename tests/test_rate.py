from __future__ import annotations

import pytest

from app.power.base import RateMeter


def test_rate_meter_warmup_then_rate() -> None:
    meter = RateMeter(min_dt=0.2)
    first = meter.update({"a": 100.0}, 10.0)
    assert first == {"a": None}

    second = meter.update({"a": 300.0}, 12.0)
    assert second["a"] == pytest.approx(100.0)


def test_rate_meter_counter_reset() -> None:
    meter = RateMeter(min_dt=0.2)
    meter.update({"a": 1000.0}, 10.0)
    reset = meter.update({"a": 10.0}, 11.0)
    assert reset["a"] is None

    recovered = meter.update({"a": 110.0}, 12.0)
    assert recovered["a"] == pytest.approx(100.0)


def test_rate_meter_small_dt() -> None:
    meter = RateMeter(min_dt=5.0)
    meter.update({"a": 100.0}, 10.0)
    assert meter.update({"a": 200.0}, 11.0)["a"] is None
