"""Core data model.

Every value carries provenance: unit, quality, source, timestamp and, for
aggregates, the inputs that were missing.  ``unavailable`` always means
``value is None`` -- never zero.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any, Generic, TypeVar

T = TypeVar("T")


class Quality(StrEnum):
    MEASURED = "measured"
    ESTIMATED = "estimated"
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True, slots=True)
class Measurement(Generic[T]):
    value: T | None
    unit: str
    quality: Quality
    source: str
    timestamp: datetime
    error: str | None = None
    missing: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "value": self.value,
            "unit": self.unit,
            "quality": self.quality.value,
            "source": self.source,
            "timestamp": self.timestamp.isoformat(),
            "error": self.error,
            "missing": list(self.missing),
        }


def measured(
    value: T,
    unit: str,
    source: str,
    timestamp: datetime,
) -> Measurement[T]:
    return Measurement(value=value, unit=unit, quality=Quality.MEASURED, source=source, timestamp=timestamp)


def estimated(
    value: T,
    unit: str,
    source: str,
    timestamp: datetime,
    missing: tuple[str, ...] = (),
) -> Measurement[T]:
    return Measurement(
        value=value,
        unit=unit,
        quality=Quality.ESTIMATED,
        source=source,
        timestamp=timestamp,
        missing=missing,
    )


def unavailable(
    unit: str,
    source: str,
    timestamp: datetime,
    error: str | None = None,
    missing: tuple[str, ...] = (),
) -> Measurement[T]:
    return Measurement(
        value=None,
        unit=unit,
        quality=Quality.UNAVAILABLE,
        source=source,
        timestamp=timestamp,
        error=error,
        missing=missing,
    )


@dataclass(frozen=True, slots=True)
class CpuPackage:
    package_id: int
    name: str
    power: Measurement[float]
    energy_j: Measurement[float]
    temperature: Measurement[float]
    frequency_mhz: Measurement[float]
    usage_percent: Measurement[float]
    core_count: int
    thread_count: int


@dataclass(frozen=True, slots=True)
class CpuSnapshot:
    timestamp: datetime
    model: str
    socket_count: int
    packages: tuple[CpuPackage, ...]
    total_power: Measurement[float]
    usage_percent: Measurement[float]
    max_temperature: Measurement[float]


@dataclass(frozen=True, slots=True)
class GpuSnapshot:
    uuid: str
    index: int
    name: str
    pci_bus_id: str | None
    power: Measurement[float]
    power_limit: Measurement[float]
    temperature: Measurement[float]
    utilization: Measurement[float]
    memory_used: Measurement[int]
    memory_total: Measurement[int]
    fan_speed: Measurement[float]
    core_clock: Measurement[float]
    memory_clock: Measurement[float]


@dataclass(frozen=True, slots=True)
class MemorySnapshot:
    timestamp: datetime
    total: Measurement[int]
    used: Measurement[int]
    available: Measurement[int]
    usage_percent: Measurement[float]
    swap_used: Measurement[int]
    swap_total: Measurement[int]


@dataclass(frozen=True, slots=True)
class DiskUsage:
    device: str
    mountpoint: str
    total: Measurement[int]
    used: Measurement[int]
    free: Measurement[int]
    usage_percent: Measurement[float]
    read_mbps: Measurement[float]
    write_mbps: Measurement[float]
    read_iops: Measurement[float]
    write_iops: Measurement[float]


@dataclass(frozen=True, slots=True)
class NetworkInterface:
    name: str
    rx_mbps: Measurement[float]
    tx_mbps: Measurement[float]
    rx_total: Measurement[int]
    tx_total: Measurement[int]


@dataclass(frozen=True, slots=True)
class SystemPower:
    power: Measurement[float]
    mode: str
    other_components_w: float
    missing_components: tuple[str, ...]
    meter_status: str | None
    last_meter_at: datetime | None
    scope: str | None


@dataclass(frozen=True, slots=True)
class CollectorHealth:
    name: str
    state: str
    last_success: datetime | None
    last_error: str | None
    consecutive_failures: int
    age_ms: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "state": self.state,
            "last_success": self.last_success.isoformat() if self.last_success else None,
            "last_error": self.last_error,
            "consecutive_failures": self.consecutive_failures,
            "age_ms": self.age_ms,
        }
