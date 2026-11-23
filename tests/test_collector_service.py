from __future__ import annotations

import asyncio

import pytest

from app.config import AppConfig, CollectorConfig, DatabaseConfig, PowerConfig
from app.domain import CpuSnapshot, DiskUsage, Quality, measured, unavailable
from app.power.adapters.base import MeterReading
from app.power.calculator import MeterState
from app.services.collector_service import CollectorService
from app.state import EventBus, StateStore
from tests.fakes import (
    FakeCollector,
    FakeCpuCollector,
    FakeDatabase,
    FakeGpuCollector,
    FakeWriter,
    NOW,
    cpu_snapshot,
    gpu_snapshot,
    memory_snapshot,
)


def disk_snapshot(device: str = "/dev/nvme0n1") -> DiskUsage:
    value = measured(0, "B", "test", NOW)
    rate = measured(0.0, "MB/s", "test", NOW)
    return DiskUsage(
        device=device,
        mountpoint="/",
        total=value,
        used=value,
        free=value,
        usage_percent=measured(0.0, "%", "test", NOW),
        read_mbps=rate,
        write_mbps=rate,
        read_iops=rate,
        write_iops=rate,
    )


def make_service(
    *,
    clock,
    cpu=None,
    gpus=(),
    gpu_available=True,
    gpu_error=None,
    writer=None,
    cfg=None,
    store=None,
    bus=None,
) -> tuple[CollectorService, StateStore, EventBus, FakeWriter]:
    cfg = cfg or AppConfig(
        collector=CollectorConfig(system_interval=0.05),
        database=DatabaseConfig(path="unused.db", write_interval=1.0),
        power=PowerConfig(estimated_other_power_w=150.0),
    )
    store = store or StateStore(clock=clock, now=lambda: NOW)
    bus = bus or EventBus()
    writer = writer or FakeWriter()
    service = CollectorService(
        cfg,
        store,
        bus,
        writer,
        FakeDatabase(),
        cpu=FakeCpuCollector(cpu),
        gpu=FakeGpuCollector(gpus, available=gpu_available, error=gpu_error),
        memory=FakeCollector(memory_snapshot()),
        disk=FakeCollector(()),
        network=FakeCollector(()),
        host=FakeCollector({"hostname": "test-host"}),
        clock=clock,
        now=lambda: NOW,
    )
    return service, store, bus, writer


def meter_reading(power: float, energy_kwh: float, timestamp=NOW) -> MeterReading:
    return MeterReading(
        timestamp=timestamp,
        power=measured(power, "W", "shelly:test", timestamp),
        energy_total=measured(energy_kwh, "kWh", "shelly:test", timestamp),
        voltage=unavailable("V", "shelly:test", timestamp),
        current=unavailable("A", "shelly:test", timestamp),
        power_factor=unavailable("", "shelly:test", timestamp),
        source="shelly:test",
    )


def healthy_meter(store: StateStore) -> None:
    store.update(
        "meter_state",
        MeterState(enabled=True, healthy=True, last_success=NOW, status="ok"),
    )


def test_snapshot_estimates_system_power_from_cpu_and_gpu(fake_clock) -> None:
    service, store, _, _ = make_service(
        clock=fake_clock,
        cpu=cpu_snapshot(100.0),
        gpus=(gpu_snapshot(power_w=50.0),),
    )
    store.update("cpu", cpu_snapshot(100.0))
    store.update("gpu", (gpu_snapshot(power_w=50.0),))
    store.update("gpu_available", True)

    snapshot = service.build_snapshot()

    assert snapshot.system_power.mode == "estimated"
    assert snapshot.system_power.power.quality is Quality.ESTIMATED
    assert snapshot.system_power.power.value == pytest.approx(100.0 + 50.0 + 150.0)
    assert snapshot.system_power.scope == "dc_components"
    assert snapshot.system_power.missing_components == ()
    assert snapshot.energy_wh is None  # first interval has no delta


def test_energy_integrates_using_monotonic_delta(fake_clock) -> None:
    service, store, _, _ = make_service(
        clock=fake_clock,
        cpu=cpu_snapshot(100.0),
        gpus=(gpu_snapshot(power_w=50.0),),
    )
    store.update("cpu", cpu_snapshot(100.0))
    store.update("gpu", (gpu_snapshot(power_w=50.0),))
    store.update("gpu_available", True)

    assert service.publish_snapshot().energy_wh is None
    fake_clock.advance(2.0)
    snapshot = service.publish_snapshot()

    assert snapshot.energy_wh is not None
    assert snapshot.energy_wh.quality is Quality.ESTIMATED
    assert snapshot.energy_wh.unit == "Wh"
    assert snapshot.energy_wh.value == pytest.approx(300.0 * 2.0 / 3600.0)


def test_energy_is_unavailable_when_system_power_is_unavailable(fake_clock) -> None:
    service, store, _, _ = make_service(
        clock=fake_clock,
        cpu=cpu_snapshot(100.0),
        gpus=(),
        gpu_available=False,
        gpu_error="driver/library version mismatch",
    )
    store.update("cpu", cpu_snapshot(100.0))
    store.update("gpu", ())
    store.update("gpu_available", False)
    store.update("gpu_error", "driver/library version mismatch")

    first = service.publish_snapshot()
    fake_clock.advance(2.0)
    second = service.publish_snapshot()

    assert first.system_power.mode == "unavailable"
    assert second.system_power.power.value is None
    assert second.system_power.missing_components == ("gpu",)
    assert second.energy_wh is None


def test_meter_wall_power_and_cumulative_energy(fake_clock) -> None:
    service, store, _, _ = make_service(
        clock=fake_clock,
        cpu=cpu_snapshot(100.0),
        gpus=(gpu_snapshot(power_w=50.0),),
    )
    store.update("cpu", cpu_snapshot(100.0))
    store.update("gpu", (gpu_snapshot(power_w=50.0),))
    store.update("gpu_available", True)
    store.update("meter", meter_reading(400.0, 1.0))
    healthy_meter(store)

    first = service.publish_snapshot()

    assert first.system_power.mode == "measured"
    assert first.system_power.scope == "wall"
    assert first.system_power.power.value == pytest.approx(400.0)
    assert first.system_power.power.source == "shelly:test"
    assert first.system_power.meter_status == "ok"
    # The first reading only establishes the counter baseline.
    assert first.energy_wh is None

    fake_clock.advance(2.0)
    store.update("meter", meter_reading(410.0, 1.01))
    second = service.publish_snapshot()

    assert second.energy_wh is not None
    assert second.energy_wh.value == pytest.approx(10.0)
    assert second.energy_wh.quality is Quality.MEASURED
    assert second.energy_wh.source == "meter-energy:shelly:test"


def test_meter_counter_reset_and_jump_do_not_fabricate_energy(fake_clock) -> None:
    service, store, _, _ = make_service(
        clock=fake_clock,
        cpu=cpu_snapshot(100.0),
        gpus=(gpu_snapshot(power_w=50.0),),
    )
    store.update("cpu", cpu_snapshot(100.0))
    store.update("gpu", (gpu_snapshot(power_w=50.0),))
    store.update("gpu_available", True)
    store.update("meter", meter_reading(400.0, 5.0))
    healthy_meter(store)
    service.publish_snapshot()

    fake_clock.advance(2.0)
    store.update("meter", meter_reading(400.0, 4.9))  # counter reset
    reset = service.publish_snapshot()
    # The counter delta is discarded; energy falls back to power integration.
    assert reset.energy_wh is not None
    assert reset.energy_wh.source.startswith("integrated:")

    fake_clock.advance(2.0)
    store.update("meter", meter_reading(400.0, 12.0))  # 7.1 kWh jump
    jump = service.publish_snapshot()
    assert jump.energy_wh is not None
    assert jump.energy_wh.source.startswith("integrated:")


def test_offline_meter_falls_back_to_estimate_and_exposes_status(fake_clock) -> None:
    service, store, _, _ = make_service(
        clock=fake_clock,
        cpu=cpu_snapshot(100.0),
        gpus=(gpu_snapshot(power_w=50.0),),
    )
    store.update("cpu", cpu_snapshot(100.0))
    store.update("gpu", (gpu_snapshot(power_w=50.0),))
    store.update("gpu_available", True)
    store.update("meter", meter_reading(400.0, 1.0))
    store.update(
        "meter_state",
        MeterState(
            enabled=True,
            healthy=False,
            last_success=NOW,
            consecutive_failures=3,
            status="offline",
        ),
    )
    store.update(
        "meter_status",
        {"enabled": True, "type": "shelly", "state": "offline", "healthy": False,
         "last_error": "connect failed"},
    )

    snapshot = service.build_snapshot()

    assert snapshot.system_power.mode != "measured"
    assert snapshot.system_power.power.quality is Quality.ESTIMATED
    assert snapshot.system_power.scope == "dc_components"
    assert snapshot.system_power.meter_status == "offline"
    assert snapshot.meter["state"] == "offline"



    service, store, _, _ = make_service(
        clock=fake_clock,
        cpu=None,
        gpus=(gpu_snapshot(power_w=50.0),),
    )
    store.update("cpu", None)
    store.update("gpu", (gpu_snapshot(power_w=50.0),))
    store.update("gpu_available", True)

    snapshot = service.build_snapshot()

    assert snapshot.system_power.mode == "unavailable"
    assert snapshot.system_power.power.value is None
    assert "cpu" in snapshot.system_power.missing_components


def test_database_projection_marks_unavailable_instead_of_zero(fake_clock) -> None:
    service, store, _, _ = make_service(
        clock=fake_clock,
        cpu=cpu_snapshot(None),
        gpus=(),
        gpu_available=False,
        gpu_error="no nvml",
    )
    store.update("cpu", cpu_snapshot(None))
    store.update("gpu", ())
    store.update("gpu_available", False)

    record, gpu_rows = service.build_snapshot().to_record()

    assert record["system_w"] is None
    assert record["system_quality"] == "unavailable"
    assert record["cpu_w"] is None
    assert record["cpu_quality"] == "unavailable"
    assert record["gpu_w"] is None
    assert record["gpu_quality"] == "unavailable"
    assert record["energy_wh"] is None
    assert record["energy_quality"] == "unavailable"
    assert gpu_rows == []


def test_publish_pushes_snapshot_to_subscribers(fake_clock) -> None:
    service, store, bus, _ = make_service(
        clock=fake_clock,
        cpu=cpu_snapshot(100.0),
        gpus=(gpu_snapshot(power_w=50.0),),
    )
    store.update("cpu", cpu_snapshot(100.0))
    store.update("gpu", (gpu_snapshot(power_w=50.0),))
    store.update("gpu_available", True)
    subscription = bus.subscribe()

    snapshot = service.publish_snapshot()

    assert subscription.queue.qsize() == 1
    payload = subscription.queue.get_nowait()
    assert payload["type"] == "snapshot"
    assert payload["system"]["power"]["value"] == pytest.approx(300.0)
    assert payload["gpu"][0]["uuid"] == "GPU-1"
    assert store.get("snapshot") is snapshot


def test_persistence_respects_write_interval(fake_clock) -> None:
    cfg = AppConfig(
        collector=CollectorConfig(system_interval=0.05),
        database=DatabaseConfig(path="unused.db", write_interval=1.0),
    )
    service, store, _, writer = make_service(
        clock=fake_clock,
        cpu=cpu_snapshot(100.0),
        gpus=(gpu_snapshot(power_w=50.0),),
        cfg=cfg,
    )
    store.update("cpu", cpu_snapshot(100.0))
    store.update("gpu", (gpu_snapshot(power_w=50.0),))
    store.update("gpu_available", True)

    service.publish_snapshot()
    assert writer.written == 1

    fake_clock.advance(0.5)
    service.publish_snapshot()
    assert writer.written == 1

    fake_clock.advance(0.6)
    service.publish_snapshot()
    assert writer.written == 2


async def test_start_runs_collectors_and_emits_snapshot() -> None:
    clock = _Clock()
    service, store, bus, _ = make_service(
        clock=clock,
        cpu=cpu_snapshot(100.0),
        gpus=(gpu_snapshot(power_w=50.0),),
    )
    subscription = bus.subscribe()
    await service.start()
    try:
        for _ in range(100):
            if (
                store.get("cpu") is not None
                and store.get("gpu")
                and subscription.queue.qsize() > 0
            ):
                break
            await asyncio.sleep(0.01)
        else:
            raise AssertionError("collectors did not produce a snapshot")

        assert isinstance(store.get("cpu"), CpuSnapshot)
        health = service.health_dict()
        assert health["cpu"]["state"] == "ok"
        assert health["gpu"]["state"] == "ok"
    finally:
        await service.stop()

    assert service._tasks == []
    assert store.get("gpu_available") is True


async def test_gpu_enumeration_failure_marks_health_error(fake_clock) -> None:
    service, store, _, _ = make_service(
        clock=fake_clock,
        cpu=cpu_snapshot(100.0),
        gpus=(),
        gpu_available=False,
        gpu_error="driver/library version mismatch",
    )

    service._apply("gpu", ())

    assert store.get("gpu_available") is False
    assert store.get("gpu_error") == "driver/library version mismatch"
    state = {item.name: item for item in service.health()}["gpu"]
    assert state.state == "error"
    assert "version mismatch" in (state.last_error or "")


def test_diagnostics_reports_probe_state(fake_clock) -> None:
    service, store, _, _ = make_service(
        clock=fake_clock,
        cpu=cpu_snapshot(100.0),
        gpus=(gpu_snapshot(power_w=50.0),),
    )
    store.update("gpu", (gpu_snapshot(power_w=50.0),))
    store.update("gpu_available", True)

    diagnostics = service.diagnostics()

    assert diagnostics["gpu"]["available"] is True
    assert diagnostics["gpu"]["nvml_initialized"] is False
    assert diagnostics["gpu"]["gpus"][0]["uuid"] == "GPU-1"
    assert diagnostics["rapl"]["source_mode"] == "helper"
    assert "collectors" in diagnostics
    assert "database" in diagnostics
    assert diagnostics["gpu"]["amdgpu_initialized"] is True
    assert diagnostics["other_power"]["configured_w"] == 150.0


def test_auto_other_power_suggestion_is_used_and_exposed(fake_clock) -> None:
    cfg = AppConfig(
        collector=CollectorConfig(system_interval=0.05),
        database=DatabaseConfig(path="unused.db", write_interval=1.0),
        power=PowerConfig(),
    )
    service, store, _, _ = make_service(clock=fake_clock, cfg=cfg)
    store.update("cpu", cpu_snapshot(100.0))
    store.update("gpu", (gpu_snapshot(power_w=50.0),))
    store.update("gpu_available", True)
    store.update("memory", memory_snapshot())
    store.update("disk", (disk_snapshot("/dev/nvme0n1"),))

    snapshot = service.build_snapshot()

    assert snapshot.system_power.other_power_source.startswith("auto:")
    assert snapshot.system_power.other_components_w == pytest.approx(71.7)
    assert snapshot.system_power.power.value == pytest.approx(221.7)
    suggestion = store.get("other_power_suggestion")
    assert suggestion["breakdown"]["board_and_fans"] == 60.0
    assert suggestion["breakdown"]["disks"] == 4.0
    payload = snapshot.to_dict()
    assert payload["system"]["other_power_source"].startswith("auto:")
    assert "fan_speed_rpm" in payload["gpu"][0]
    assert payload["gpu"][0]["include_in_system_total"] is True


def test_empty_snapshot_is_safe_before_first_collection(fake_clock) -> None:
    service, _, _, _ = make_service(clock=fake_clock)
    snapshot = service.empty_snapshot()
    assert snapshot.system_power.mode == "unavailable"
    assert snapshot.system_power.power.value is None
    assert snapshot.cpu is None
    assert snapshot.to_dict()["system"]["power"]["quality"] == "unavailable"


class _Clock:
    def __init__(self) -> None:
        self.value = 1000.0

    def __call__(self) -> float:
        self.value += 0.001
        return self.value
