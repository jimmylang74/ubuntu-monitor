"""Static host information and uptime."""

from __future__ import annotations

import platform
import socket
from datetime import UTC, datetime
from typing import Callable

import psutil

from app.config import AppConfig


class SystemCollector:
    name = "system"

    def __init__(
        self,
        cfg: AppConfig,
        *,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self.interval_s = cfg.collector.system_interval
        self._now = now or (lambda: datetime.now(UTC))

    def collect(self) -> dict[str, str]:
        timestamp = self._now()
        boot = datetime.fromtimestamp(psutil.boot_time(), tz=UTC)
        return {
            "hostname": socket.gethostname(),
            "platform": platform.platform(),
            "kernel": platform.release(),
            "python": platform.python_version(),
            "boot_time": boot.isoformat(),
            "collected_at": timestamp.isoformat(),
            "uptime_s": f"{max(0.0, timestamp.timestamp() - psutil.boot_time()):.1f}",
        }
