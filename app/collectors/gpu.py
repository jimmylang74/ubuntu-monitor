"""GPU collector: NVML first, nvidia-smi fallback when NVML cannot initialize."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Callable

from app.config import AppConfig
from app.domain import GpuSnapshot
from app.power.adapters.nvidia import NvidiaAdapter, NvidiaSmiAdapter


class GpuCollector:
    name = "gpu"

    def __init__(
        self,
        cfg: AppConfig,
        *,
        nvml: NvidiaAdapter | None = None,
        smi: NvidiaSmiAdapter | None = None,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self.cfg = cfg
        self.interval_s = cfg.collector.gpu_interval
        clock_now = now or (lambda: datetime.now(UTC))
        self._nvml = nvml or NvidiaAdapter(cfg.gpu, now=clock_now)
        self._smi = smi or NvidiaSmiAdapter(cfg.gpu, now=clock_now)
        self.error: str | None = None
        self.available = False

    @property
    def nvml(self) -> NvidiaAdapter:
        return self._nvml

    def collect(self) -> tuple[GpuSnapshot, ...]:
        gpus = self._nvml.sample()
        if gpus:
            self.error = None
            self.available = True
            return gpus

        if self._nvml.initialized:
            # NVML enumerated successfully and reported zero devices.
            self.error = None
            self.available = True
            return ()

        if self.cfg.gpu.fallback_nvidia_smi:
            fallback = self._smi.sample()
            if self._smi.error is None:
                # nvidia-smi succeeded; zero rows means zero GPUs.
                self.error = None
                self.available = True
            else:
                self.error = self._nvml.init_error or self._smi.error
                self.available = False
            return fallback

        self.error = self._nvml.init_error
        self.available = False
        return ()

    def close(self) -> None:
        self._nvml.close()
