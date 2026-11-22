"""Filesystem usage and block-device throughput."""

from __future__ import annotations

import re
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Callable

import psutil

from app.config import AppConfig
from app.domain import DiskUsage, Measurement, measured, unavailable
from app.power.base import RateMeter


def _parent_device(name: str, known: set[str]) -> str | None:
    if name in known:
        return name
    candidates: list[str] = []
    match = re.match(r"^(.*)p\d+$", name)
    if match:
        candidates.append(match.group(1))
    match = re.match(r"^([a-zA-Z]+)\d+$", name)
    if match:
        candidates.append(match.group(1))
    for candidate in candidates:
        if candidate in known:
            return candidate
    return None


class DiskCollector:
    name = "disk"

    def __init__(
        self,
        cfg: AppConfig,
        *,
        now: Callable[[], datetime] | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.interval_s = cfg.collector.disk_interval
        self._now = now or (lambda: datetime.now(UTC))
        self._clock = clock
        self._rates = RateMeter()

    def collect(self) -> tuple[DiskUsage, ...]:
        timestamp = self._now()
        io = psutil.disk_io_counters(perdisk=True) or {}
        known_devices = {name for name in io if not name.startswith(("loop", "ram"))}

        counters: dict[str, float] = {}
        for name in known_devices:
            stats = io[name]
            counters[f"{name}:read_bytes"] = float(stats.read_bytes)
            counters[f"{name}:write_bytes"] = float(stats.write_bytes)
            counters[f"{name}:read_count"] = float(stats.read_count)
            counters[f"{name}:write_count"] = float(stats.write_count)
        rates = self._rates.update(counters, self._clock())

        selected: dict[str, tuple[psutil.sdiskpart, psutil._common.sdiskusage]] = {}
        for partition in psutil.disk_partitions(all=False):
            if not partition.device or partition.fstype in {"", "squashfs"}:
                continue
            try:
                usage = psutil.disk_usage(partition.mountpoint)
            except OSError:
                continue
            device_name = Path(partition.device).name
            if not device_name:
                continue
            device = _parent_device(device_name, known_devices) or device_name
            current = selected.get(device)
            if current is None or partition.mountpoint == "/":
                selected[device] = (partition, usage)

        results: list[DiskUsage] = []
        for device, (partition, usage) in sorted(selected.items()):
            results.append(
                DiskUsage(
                    device=device,
                    mountpoint=partition.mountpoint,
                    total=measured(int(usage.total), "bytes", "psutil:disk_usage", timestamp),
                    used=measured(int(usage.used), "bytes", "psutil:disk_usage", timestamp),
                    free=measured(int(usage.free), "bytes", "psutil:disk_usage", timestamp),
                    usage_percent=measured(float(usage.percent), "%", "psutil:disk_usage", timestamp),
                    read_mbps=_rate(rates, f"{device}:read_bytes", 1_000_000.0, timestamp),
                    write_mbps=_rate(rates, f"{device}:write_bytes", 1_000_000.0, timestamp),
                    read_iops=_rate(rates, f"{device}:read_count", 1.0, timestamp),
                    write_iops=_rate(rates, f"{device}:write_count", 1.0, timestamp),
                )
            )
        return tuple(results)


def _rate(rates: dict[str, float | None], key: str, divisor: float, timestamp: datetime) -> Measurement[float]:
    value = rates.get(key)
    unit = "MB/s" if divisor != 1.0 else "IOPS"
    if value is None:
        return unavailable(unit, "psutil:disk_io_counters", timestamp, error="rate warm-up or counter reset")
    return measured(value / divisor, unit, "psutil:disk_io_counters", timestamp)
