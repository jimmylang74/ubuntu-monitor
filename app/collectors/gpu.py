"""GPU collector with independent NVIDIA and AMD backends.

NVML (or nvidia-smi) and amdgpu hwmon are sampled separately.  This supports
NVIDIA-only, AMD-only and mixed hosts; a failure in one backend is reported in
``error`` but does not hide GPUs discovered by another backend.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from typing import Callable

from app.config import AppConfig
from app.domain import GpuSnapshot
from app.power.adapters.amdgpu import AmdgpuAdapter
from app.power.adapters.nvidia import NvidiaAdapter, NvidiaSmiAdapter


class GpuCollector:
    name = "gpu"

    def __init__(
        self,
        cfg: AppConfig,
        *,
        nvml: NvidiaAdapter | None = None,
        smi: NvidiaSmiAdapter | None = None,
        amd: AmdgpuAdapter | None = None,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self.cfg = cfg
        self.interval_s = cfg.collector.gpu_interval
        clock_now = now or (lambda: datetime.now(UTC))
        self._nvml = nvml or NvidiaAdapter(cfg.gpu, now=clock_now)
        self._smi = smi or NvidiaSmiAdapter(cfg.gpu, now=clock_now)
        self._amd = amd or AmdgpuAdapter(cfg.gpu, now=clock_now)
        self.error: str | None = None
        self.available = False

    @property
    def nvml(self) -> NvidiaAdapter:
        return self._nvml

    @property
    def amdgpu(self) -> AmdgpuAdapter:
        return self._amd

    def _collect_nvidia(self) -> tuple[tuple[GpuSnapshot, ...], bool, str | None]:
        gpus = self._nvml.sample()
        if gpus or self._nvml.initialized:
            # NVML enumerated successfully; zero devices is authoritative.
            return gpus, True, None

        if self.cfg.gpu.fallback_nvidia_smi:
            fallback = self._smi.sample()
            if self._smi.error is None:
                return fallback, True, None
            return fallback, False, self._nvml.init_error or self._smi.error

        return (), False, self._nvml.init_error

    def collect(self) -> tuple[GpuSnapshot, ...]:
        collected: list[GpuSnapshot] = []
        healthy = 0
        total = 0
        errors: list[str] = []

        if "nvidia" in self.cfg.gpu.backends:
            total += 1
            gpus, available, error = self._collect_nvidia()
            collected.extend(gpus)
            if available:
                healthy += 1
            elif error:
                errors.append(f"nvidia: {error}")

        if "amdgpu" in self.cfg.gpu.backends:
            total += 1
            gpus = self._amd.sample()
            collected.extend(gpus)
            # An empty scan means no AMD GPU is installed, which is not a
            # failure.  init_error is available for transport-level problems.
            healthy += 1
            if self._amd.init_error:
                errors.append(f"amdgpu: {self._amd.init_error}")

        if total == 0:
            self.available = False
            self.error = "no GPU backend enabled in config"
            return ()

        if self.cfg.gpu.require_all_backends:
            self.available = healthy == total
        else:
            self.available = healthy > 0
        self.error = "; ".join(errors) or None

        # Backends number from zero independently; make indices unique and
        # contiguous for the UI and database rows.
        return tuple(replace(gpu, index=index) for index, gpu in enumerate(collected))

    def close(self) -> None:
        self._nvml.close()
        self._amd.close()
