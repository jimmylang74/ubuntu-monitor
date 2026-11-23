"""API/WebSocket snapshot plus its database projection.

The snapshot is the single immutable view shared by REST, WebSocket and the
database writer.  Serialization lives here so every transport reports values
with the same provenance fields (value/unit/quality/source/timestamp).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from app.domain import (
    CollectorHealth,
    CpuSnapshot,
    DiskUsage,
    GpuSnapshot,
    Measurement,
    MemorySnapshot,
    NetworkInterface,
    Quality,
    SystemPower,
    unavailable,
)


def empty_system_power(timestamp: datetime) -> SystemPower:
    return SystemPower(
        power=unavailable("W", "calculator", timestamp, error="waiting for first sample"),
        mode="unavailable",
        other_components_w=0.0,
        missing_components=("cpu", "gpu"),
        meter_status="disabled",
        last_meter_at=None,
        scope=None,
    )


def _measurement(value: Measurement[Any] | None) -> dict[str, Any]:
    if value is None:
        return {
            "value": None,
            "unit": "W",
            "quality": Quality.UNAVAILABLE.value,
            "source": "snapshot",
            "timestamp": None,
            "error": "not_collected",
            "missing": [],
        }
    return value.to_dict()


def _cpu_dict(cpu: CpuSnapshot | None) -> dict[str, Any] | None:
    if cpu is None:
        return None
    return {
        "timestamp": cpu.timestamp.isoformat(),
        "model": cpu.model,
        "socket_count": cpu.socket_count,
        "total_power": cpu.total_power.to_dict(),
        "usage_percent": cpu.usage_percent.to_dict(),
        "max_temperature": cpu.max_temperature.to_dict(),
        "packages": [
            {
                "package_id": package.package_id,
                "name": package.name,
                "power": package.power.to_dict(),
                "energy_j": package.energy_j.to_dict(),
                "temperature": package.temperature.to_dict(),
                "frequency_mhz": package.frequency_mhz.to_dict(),
                "usage_percent": package.usage_percent.to_dict(),
                "core_count": package.core_count,
                "thread_count": package.thread_count,
            }
            for package in cpu.packages
        ],
    }


def _gpu_dict(gpu: GpuSnapshot) -> dict[str, Any]:
    return {
        "uuid": gpu.uuid,
        "index": gpu.index,
        "name": gpu.name,
        "pci_bus_id": gpu.pci_bus_id,
        "power": gpu.power.to_dict(),
        "power_limit": gpu.power_limit.to_dict(),
        "temperature": gpu.temperature.to_dict(),
        "utilization": gpu.utilization.to_dict(),
        "memory_used": gpu.memory_used.to_dict(),
        "memory_total": gpu.memory_total.to_dict(),
        "fan_speed": gpu.fan_speed.to_dict(),
        "fan_speed_rpm": gpu.fan_speed_rpm.to_dict(),
        "core_clock": gpu.core_clock.to_dict(),
        "memory_clock": gpu.memory_clock.to_dict(),
        "include_in_system_total": gpu.include_in_system_total,
    }


def _memory_dict(memory: MemorySnapshot | None) -> dict[str, Any] | None:
    if memory is None:
        return None
    return {
        "timestamp": memory.timestamp.isoformat(),
        "total": memory.total.to_dict(),
        "used": memory.used.to_dict(),
        "available": memory.available.to_dict(),
        "usage_percent": memory.usage_percent.to_dict(),
        "swap_used": memory.swap_used.to_dict(),
        "swap_total": memory.swap_total.to_dict(),
    }


def _disk_dict(disk: DiskUsage) -> dict[str, Any]:
    return {
        "device": disk.device,
        "mountpoint": disk.mountpoint,
        "total": disk.total.to_dict(),
        "used": disk.used.to_dict(),
        "free": disk.free.to_dict(),
        "usage_percent": disk.usage_percent.to_dict(),
        "read_mbps": disk.read_mbps.to_dict(),
        "write_mbps": disk.write_mbps.to_dict(),
        "read_iops": disk.read_iops.to_dict(),
        "write_iops": disk.write_iops.to_dict(),
    }


def _network_dict(interface: NetworkInterface) -> dict[str, Any]:
    return {
        "name": interface.name,
        "rx_mbps": interface.rx_mbps.to_dict(),
        "tx_mbps": interface.tx_mbps.to_dict(),
        "rx_total": interface.rx_total.to_dict(),
        "tx_total": interface.tx_total.to_dict(),
    }


def _value(measurement: Measurement[Any] | None) -> Any:
    return None if measurement is None else measurement.value


def _quality(measurement: Measurement[Any] | None) -> str | None:
    return None if measurement is None else measurement.quality.value


def _average_measured(measurements: list[Measurement[Any]]) -> float | None:
    values = [
        float(item.value)
        for item in measurements
        if item.value is not None and item.quality is Quality.MEASURED
    ]
    if not values:
        return None
    return sum(values) / len(values)


def _max_measured(measurements: list[Measurement[Any]]) -> float | None:
    values = [
        float(item.value)
        for item in measurements
        if item.value is not None and item.quality is Quality.MEASURED
    ]
    if not values:
        return None
    return max(values)


@dataclass(frozen=True, slots=True)
class Snapshot:
    timestamp: datetime
    system_power: SystemPower
    cpu: CpuSnapshot | None
    gpus: tuple[GpuSnapshot, ...]
    memory: MemorySnapshot | None
    disks: tuple[DiskUsage, ...]
    network: tuple[NetworkInterface, ...]
    host: dict[str, str]
    gpu_available: bool
    gpu_error: str | None
    health: tuple[CollectorHealth, ...]
    database: dict[str, Any]
    energy_wh: Measurement[float] | None = None
    started_at: datetime | None = None
    meter: dict[str, Any] | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    # ------------------------------------------------------------------ JSON

    def to_dict(self) -> dict[str, Any]:
        system_power = self.system_power
        power = system_power.power.to_dict()
        power["scope"] = system_power.scope
        power["mode"] = system_power.mode
        power["missing_components"] = list(system_power.missing_components)
        power["meter_status"] = system_power.meter_status
        power["last_meter_at"] = (
            system_power.last_meter_at.isoformat() if system_power.last_meter_at else None
        )
        return {
            "type": "snapshot",
            "protocol": 1,
            "timestamp": self.timestamp.isoformat(),
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "system": {
                "power": power,
                "other_components_w": system_power.other_components_w,
                "other_power_source": system_power.other_power_source,
                "energy": _measurement(self.energy_wh),
            },
            "cpu": _cpu_dict(self.cpu),
            "gpu": [_gpu_dict(gpu) for gpu in self.gpus],
            "gpu_available": self.gpu_available,
            "gpu_error": self.gpu_error,
            "memory": _memory_dict(self.memory),
            "disks": [_disk_dict(disk) for disk in self.disks],
            "network": [_network_dict(interface) for interface in self.network],
            "host": self.host,
            "health": {item.name: item.to_dict() for item in self.health},
            "database": self.database,
            "meter": self.meter,
            **self.extra,
        }

    # ------------------------------------------------------------------ database

    def to_record(self) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        """Flatten into ``sample`` + ``gpu_sample`` rows.

        Missing measurements are stored as SQL NULL together with an
        ``unavailable`` quality marker -- never as 0.
        """
        ts_ms = int(self.timestamp.timestamp() * 1000)
        cpu_power = self.cpu.total_power if self.cpu is not None else None
        gpu_power = sum(
            float(gpu.power.value)
            for gpu in self.gpus
            if gpu.power.value is not None and gpu.power.quality is Quality.MEASURED
        )
        gpus_complete = self.gpu_available and all(
            gpu.power.value is not None and gpu.power.quality is Quality.MEASURED for gpu in self.gpus
        )
        meter_power = ((self.meter or {}).get("reading") or {}).get("power") or {}
        meter_w = meter_power.get("value")
        if meter_power.get("quality") != Quality.MEASURED.value:
            meter_w = None

        record = {
            "ts_ms": ts_ms,
            "system_w": self.system_power.power.value,
            "system_quality": self.system_power.power.quality.value,
            "system_source": self.system_power.power.source,
            "system_scope": self.system_power.scope,
            "cpu_w": _value(cpu_power),
            "cpu_quality": _quality(cpu_power),
            "gpu_w": gpu_power if gpus_complete else None,
            "gpu_quality": (
                Quality.MEASURED.value if gpus_complete else Quality.UNAVAILABLE.value
            ),
            "cpu_usage_pct": _value(self.cpu.usage_percent) if self.cpu else None,
            "cpu_temp_c": _value(self.cpu.max_temperature) if self.cpu else None,
            "gpu_usage_pct": _average_measured([gpu.utilization for gpu in self.gpus]),
            "gpu_temp_c": _max_measured([gpu.temperature for gpu in self.gpus]),
            "mem_used_b": _value(self.memory.used) if self.memory else None,
            "mem_total_b": _value(self.memory.total) if self.memory else None,
            "mem_usage_pct": _value(self.memory.usage_percent) if self.memory else None,
            "swap_used_b": _value(self.memory.swap_used) if self.memory else None,
            "energy_wh": _value(self.energy_wh),
            "energy_quality": _quality(self.energy_wh) or Quality.UNAVAILABLE.value,
            "meter_w": meter_w,
        }

        gpu_rows = [
            {
                "ts_ms": ts_ms,
                "uuid": gpu.uuid,
                "power_w": _value(gpu.power),
                "power_quality": _quality(gpu.power),
                "temp_c": _value(gpu.temperature),
                "util_pct": _value(gpu.utilization),
                "mem_used_b": _value(gpu.memory_used),
                "mem_total_b": _value(gpu.memory_total),
                "power_limit_w": _value(gpu.power_limit),
                "fan_pct": _value(gpu.fan_speed),
                "clock_mhz": _value(gpu.core_clock),
                "mem_clock_mhz": _value(gpu.memory_clock),
            }
            for gpu in self.gpus
        ]
        return record, gpu_rows


@dataclass(frozen=True, slots=True)
class HealthView:
    """Aggregated collector/database health for REST responses."""

    collectors: tuple[CollectorHealth, ...]
    database: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "collectors": {item.name: item.to_dict() for item in self.collectors},
            "database": self.database,
        }
