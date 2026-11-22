from __future__ import annotations

from datetime import UTC, datetime

import pytest

from app.config import AppConfig, ExternalMeterConfig, PowerConfig
from app.domain import Quality, measured, unavailable
from app.power.adapters.base import (
    MeterCapabilities,
    MeterError,
    MeterReading,
)
from app.services.meter_service import MeterService
from app.state import StateStore
from tests.fakes import FakeWriter

NOW = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)


def reading(
    power: float = 200.0,
    *,
    energy_kwh: float | None = None,
    timestamp: datetime = NOW,
    source: str = "shelly:test",
) -> MeterReading:
    return MeterReading(
        timestamp=timestamp,
        power=measured(power, "W", source, timestamp),
        energy_total=(
            measured(energy_kwh, "kWh", source, timestamp)
            if energy_kwh is not None
            else unavailable("kWh", source, timestamp, error="no cumulative register")
        ),
        voltage=unavailable("V", source, timestamp),
        current=unavailable("A", source, timestamp),
        power_factor=unavailable("", source, timestamp),
        source=source,
    )


class FakeAdapter:
    adapter_id = "fake"

    def __init__(self, results: list) -> None:
        self.results = list(results)
        self.probes = 0
        self.closed = False

    async def probe(self) -> MeterCapabilities:
        self.probes += 1
        return MeterCapabilities(adapter_id="fake", supports_energy=True)

    async def read(self) -> MeterReading:
        result = self.results.pop(0)
        if isinstance(result, BaseException):
            raise result
        return result

    async def close(self) -> None:
        self.closed = True


class Clock:
    def __init__(self, value: float = 1000.0) -> None:
        self.value = value

    def __call__(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += seconds


def make_service(
    *results,
    failover_after: int = 2,
    enabled: bool = True,
) -> tuple[MeterService, StateStore, FakeWriter, FakeAdapter, Clock]:
    cfg = AppConfig(
        power=PowerConfig(
            external_meter=ExternalMeterConfig(
                enabled=enabled,
                type="shelly",
                interval=0.01,
                failover_after=failover_after,
            )
        )
    )
    store = StateStore()
    writer = FakeWriter()
    adapter = FakeAdapter(list(results))
    clock = Clock()
    service = MeterService(
        cfg,
        store,
        writer,
        adapter=adapter,
        now=lambda: NOW,
        clock=clock,
    )
    return service, store, writer, adapter, clock


async def test_successful_poll_publishes_state_and_persists_sample() -> None:
    sample = reading(energy_kwh=1.5)
    service, store, writer, adapter, _ = make_service(sample)

    result = await service.poll_once()

    assert result is sample
    assert service.healthy is True
    assert service.state == "ok"
    assert adapter.probes == 1
    assert store.get("meter") is sample
    state = store.get("meter_state")
    assert state.enabled is True
    assert state.healthy is True
    assert state.status == "ok"
    status = store.get("meter_status")
    assert status["scope"] == "wall"
    assert status["energy_source"] == "meter"
    assert status["reading"]["power"]["value"] == pytest.approx(200.0)
    assert len(writer.meter_rows) == 1
    assert writer.meter_rows[0]["power_w"] == pytest.approx(200.0)
    assert writer.meter_rows[0]["energy_kwh"] == pytest.approx(1.5)


async def test_meter_sample_persistence_is_throttled() -> None:
    service, _, writer, _, clock = make_service(reading(), reading(), reading())

    await service.poll_once()
    await service.poll_once()
    assert len(writer.meter_rows) == 1

    clock.advance(1.5)
    await service.poll_once()
    assert len(writer.meter_rows) == 2


async def test_failover_after_consecutive_failures_and_single_event() -> None:
    service, store, writer, _, _ = make_service(
        MeterError("first"),
        MeterError("second"),
        MeterError("third"),
        failover_after=2,
    )

    await service.poll_once()
    assert service.state == "starting"
    assert service.healthy is False

    await service.poll_once()
    assert service.state == "offline"
    assert service.failures == 2
    offline_events = [item for item in writer.events if item[0] == "warning"]
    assert len(offline_events) == 1
    assert "offline after 2 consecutive failures" in offline_events[0][2]

    await service.poll_once()
    assert service.state == "offline"
    assert service.failures == 3
    # The offline event is emitted once, not on every subsequent failure.
    assert len([item for item in writer.events if item[0] == "warning"]) == 1
    assert store.get("meter_state").healthy is False
    status = store.get("meter_status")
    assert status["state"] == "offline"
    # No reading ever succeeded, so there is no energy source to report.
    assert status["energy_source"] is None


async def test_last_good_reading_is_retained_during_retry() -> None:
    sample = reading()
    service, store, _, _, _ = make_service(sample, MeterError("blip"), failover_after=3)

    await service.poll_once()
    await service.poll_once()

    assert service.state == "retrying"
    assert service.failures == 1
    assert service.healthy is True  # still below the failover threshold
    assert store.get("meter") is sample
    status = store.get("meter_status")
    assert status["reading"]["power"]["value"] == pytest.approx(200.0)
    assert status["last_success"] == NOW.isoformat()


async def test_recovery_resets_failures_and_emits_info_event() -> None:
    service, store, writer, _, _ = make_service(
        MeterError("one"),
        MeterError("two"),
        reading(),
        failover_after=2,
    )

    await service.poll_once()
    await service.poll_once()
    assert service.state == "offline"
    assert service.healthy is False

    recovered = await service.poll_once()

    assert recovered is not None
    assert service.state == "ok"
    assert service.healthy is True
    assert service.failures == 0
    assert service.last_error is None
    info_events = [item for item in writer.events if item[0] == "info"]
    assert len(info_events) == 1
    assert "recovered" in info_events[0][2]
    assert store.get("meter_state").healthy is True


async def test_offline_meter_with_retained_reading_reports_integration_energy() -> None:
    service, store, _, _, _ = make_service(
        reading(energy_kwh=1.5),
        MeterError("one"),
        MeterError("two"),
        failover_after=2,
    )

    await service.poll_once()
    await service.poll_once()
    assert service.state == "retrying"
    assert store.get("meter_status")["energy_source"] == "meter"

    await service.poll_once()
    status = store.get("meter_status")
    assert status["state"] == "offline"
    assert status["healthy"] is False
    # The last reading is still exposed, but cumulative meter energy is no
    # longer used while the meter is unhealthy.
    assert status["reading"]["power"]["value"] == pytest.approx(200.0)
    assert status["energy_source"] == "integration"


async def test_disabled_meter_never_polls() -> None:
    service, store, writer, adapter, _ = make_service(reading(), enabled=False)

    result = await service.poll_once()

    assert result is None
    assert service.state == "disabled"
    assert service.healthy is False
    assert adapter.probes == 0
    assert writer.meter_rows == []
    assert store.get("meter_status")["enabled"] is False
    assert store.get("meter_status")["state"] == "disabled"


async def test_close_releases_adapter() -> None:
    service, _, _, adapter, _ = make_service(reading())

    await service.start()
    await service.stop()

    assert adapter.closed is True
    assert service._task is None
