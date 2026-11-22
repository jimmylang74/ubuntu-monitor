"""Per-interface network throughput with configurable filtering."""

from __future__ import annotations

import fnmatch
import time
from collections.abc import Iterable
from datetime import UTC, datetime
from typing import Callable

import psutil

from app.config import AppConfig, NetworkConfig
from app.domain import NetworkInterface, measured, unavailable
from app.power.base import RateMeter


def filter_interfaces(names: Iterable[str], cfg: NetworkConfig) -> list[str]:
    ordered = sorted(names)
    if cfg.include_interfaces:
        return [
            name
            for name in ordered
            if any(fnmatch.fnmatchcase(name, pattern) for pattern in cfg.include_interfaces)
        ]
    return [
        name
        for name in ordered
        if not any(fnmatch.fnmatchcase(name, pattern) for pattern in cfg.exclude_interfaces)
    ]


class NetworkCollector:
    name = "network"

    def __init__(
        self,
        cfg: AppConfig,
        *,
        now: Callable[[], datetime] | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.interval_s = cfg.collector.network_interval
        self._cfg = cfg.network
        self._now = now or (lambda: datetime.now(UTC))
        self._clock = clock
        self._rates = RateMeter()

    def collect(self) -> tuple[NetworkInterface, ...]:
        timestamp = self._now()
        counters = psutil.net_io_counters(pernic=True) or {}
        selected = filter_interfaces(counters, self._cfg)

        raw: dict[str, float] = {}
        for name in selected:
            stats = counters[name]
            raw[f"{name}:rx"] = float(stats.bytes_recv)
            raw[f"{name}:tx"] = float(stats.bytes_sent)
        rates = self._rates.update(raw, self._clock())

        results: list[NetworkInterface] = []
        for name in selected:
            stats = counters[name]
            rx = rates.get(f"{name}:rx")
            tx = rates.get(f"{name}:tx")
            results.append(
                NetworkInterface(
                    name=name,
                    rx_mbps=(
                        unavailable("MB/s", "psutil:net_io_counters", timestamp, error="rate warm-up or counter reset")
                        if rx is None
                        else measured(rx / 1_000_000.0, "MB/s", "psutil:net_io_counters", timestamp)
                    ),
                    tx_mbps=(
                        unavailable("MB/s", "psutil:net_io_counters", timestamp, error="rate warm-up or counter reset")
                        if tx is None
                        else measured(tx / 1_000_000.0, "MB/s", "psutil:net_io_counters", timestamp)
                    ),
                    rx_total=measured(int(stats.bytes_recv), "bytes", "psutil:net_io_counters", timestamp),
                    tx_total=measured(int(stats.bytes_sent), "bytes", "psutil:net_io_counters", timestamp),
                )
            )
        return tuple(results)
