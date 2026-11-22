"""Memory, swap usage via psutil."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Callable

import psutil

from app.config import AppConfig
from app.domain import MemorySnapshot, measured


class MemoryCollector:
    name = "memory"

    def __init__(
        self,
        cfg: AppConfig,
        *,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self.interval_s = cfg.collector.memory_interval
        self._now = now or (lambda: datetime.now(UTC))

    def collect(self) -> MemorySnapshot:
        timestamp = self._now()
        memory = psutil.virtual_memory()
        swap = psutil.swap_memory()
        return MemorySnapshot(
            timestamp=timestamp,
            total=measured(int(memory.total), "bytes", "psutil:virtual_memory", timestamp),
            used=measured(int(memory.used), "bytes", "psutil:virtual_memory", timestamp),
            available=measured(int(memory.available), "bytes", "psutil:virtual_memory", timestamp),
            usage_percent=measured(float(memory.percent), "%", "psutil:virtual_memory", timestamp),
            swap_used=measured(int(swap.used), "bytes", "psutil:swap_memory", timestamp),
            swap_total=measured(int(swap.total), "bytes", "psutil:swap_memory", timestamp),
        )
