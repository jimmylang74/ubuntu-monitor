"""Shared fakes for Phase 2 service/API tests (no real hardware)."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from app.domain import (
    CpuPackage,
    CpuSnapshot,
    GpuSnapshot,
    Measurement,
    MemorySnapshot,
    measured,
    unavailable,
)

NOW = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)


def cpu_snapshot(power_w: float | None = 100.0, *, timestamp: datetime = NOW) -> CpuSnapshot:
    power = (
        unavailable("W", "rapl", timestamp, error="no RAPL")
        if power_w is None
        else measured(power_w, "W", "rapl:packages", timestamp)
    )
    spare = measured(0.0, "W", "none", timestamp)
    package = CpuPackage(
        package_id=0,
        name="package-0",
        power=power,
        energy_j=spare,
        temperature=measured(40.0, "°C", "hwmon", timestamp),
        frequency_mhz=measured(2000.0, "MHz", "sysfs", timestamp),
        usage_percent=measured(10.0, "%", "psutil", timestamp),
        core_count=4,
        thread_count=8,
    )
    return CpuSnapshot(
        timestamp=timestamp,
        model="Test CPU",
        socket_count=1,
        packages=(package,),
        total_power=power,
        usage_percent=measured(10.0, "%", "psutil", timestamp),
        max_temperature=measured(40.0, "°C", "hwmon", timestamp),
    )


def gpu_snapshot(
    uuid: str = "GPU-1", power_w: float | None = 50.0, *, timestamp: datetime = NOW
) -> GpuSnapshot:
    power = (
        unavailable("W", f"nvml:{uuid}", timestamp, error="GPU lost")
        if power_w is None
        else measured(power_w, "W", f"nvml:{uuid}", timestamp)
    )
    def metric(value: Any, unit: str) -> Measurement[Any]:
        return measured(value, unit, f"nvml:{uuid}", timestamp)

    return GpuSnapshot(
        uuid=uuid,
        index=0,
        name="Tesla V100",
        pci_bus_id="0000:02:00.0",
        power=power,
        power_limit=metric(250.0, "W"),
        temperature=metric(55.0, "°C"),
        utilization=metric(65.0, "%"),
        memory_used=metric(1024**3, "bytes"),
        memory_total=metric(16 * 1024**3, "bytes"),
        fan_speed=unavailable("%", f"nvml:{uuid}", timestamp, error="not supported"),
        core_clock=metric(1530.0, "MHz"),
        memory_clock=metric(877.0, "MHz"),
    )


def memory_snapshot(*, timestamp: datetime = NOW) -> MemorySnapshot:
    return MemorySnapshot(
        timestamp=timestamp,
        total=measured(64 * 1024**3, "bytes", "psutil", timestamp),
        used=measured(16 * 1024**3, "bytes", "psutil", timestamp),
        available=measured(48 * 1024**3, "bytes", "psutil", timestamp),
        usage_percent=measured(25.0, "%", "psutil", timestamp),
        swap_used=measured(0, "bytes", "psutil", timestamp),
        swap_total=measured(8 * 1024**3, "bytes", "psutil", timestamp),
    )


class FakeCollector:
    name = "fake"

    def __init__(self, result: Any) -> None:
        self.result = result
        self.calls = 0

    def collect(self) -> Any:
        self.calls += 1
        return self.result


class FakeRapl:
    source_mode = "helper"
    helper_error = None
    all_domains: tuple[Any, ...] = ()


class FakeNvml:
    initialized = False
    init_error = "driver/library version mismatch"


class FakeCpuCollector(FakeCollector):
    rapl = FakeRapl()


class FakeGpuCollector(FakeCollector):
    def __init__(self, result: Any, *, available: bool, error: str | None = None) -> None:
        super().__init__(result)
        self.available = available
        self.error = error
        self.nvml = FakeNvml()

    def close(self) -> None:
        pass


class FakeDatabase:
    """Duck-typed replacement for AppDatabase in writer tests."""

    def __init__(self) -> None:
        self.fail = False
        self.calls = 0
        self.events: list[tuple[str, str, str]] = []

    def write_snapshot(self, record: dict[str, Any], gpu_rows: list[dict[str, Any]]) -> None:
        self.calls += 1
        if self.fail:
            raise RuntimeError("disk on fire")

    def write_meter_sample(self, row: dict[str, Any]) -> None:
        self.calls += 1
        if self.fail:
            raise RuntimeError("disk on fire")

    def write_event(self, level: str, component: str, message: str) -> None:
        self.events.append((level, component, message))

    def status(self) -> dict[str, Any]:
        return {"state": "degraded" if self.fail else "ok"}


@dataclass
class FakeWriter:
    written: int = 0
    dropped: int = 0
    meter_rows: list[dict[str, Any]] = field(default_factory=list)
    events: list[tuple[str, str, str]] = field(default_factory=list)

    def submit(self, record: dict[str, Any], gpu_rows: list[dict[str, Any]]) -> None:
        self.written += 1

    def submit_meter(self, row: dict[str, Any]) -> None:
        self.meter_rows.append(row)

    def submit_event(self, level: str, component: str, message: str) -> None:
        self.events.append((level, component, message))

    def status(self) -> dict[str, Any]:
        return {"state": "ok", "written": self.written, "dropped": self.dropped}
