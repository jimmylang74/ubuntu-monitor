"""Decide between measured wall power and a component-sum estimate."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from app.config import PowerConfig
from app.domain import (
    CpuSnapshot,
    GpuSnapshot,
    Measurement,
    Quality,
    SystemPower,
    estimated,
    measured,
    unavailable,
)


@dataclass(frozen=True, slots=True)
class MeterState:
    enabled: bool = False
    healthy: bool = False
    last_success: datetime | None = None
    consecutive_failures: int = 0
    status: str | None = None


def _is_measured(value: Measurement[float] | Measurement[int]) -> bool:
    return value.value is not None and value.quality is Quality.MEASURED


def compute_system_power(
    cpu: CpuSnapshot | None,
    gpus: tuple[GpuSnapshot, ...],
    meter_power: Measurement[float] | None,
    meter_state: MeterState,
    cfg: PowerConfig,
    timestamp: datetime,
    *,
    gpu_available: bool = True,
) -> SystemPower:
    """Return system power with an explicit, non-ambiguous source.

    Measured (wall) power is only available from an external meter.  Anything
    derived from CPU/GPU readings is an estimate of DC component power and can
    never be reported as measured.

    ``gpu_available`` distinguishes "no NVIDIA GPU installed" (True, zero
    watts) from "GPU enumeration failed" (False, missing component).
    """
    if cfg.mode != "estimated" and meter_state.healthy and meter_power is not None and meter_power.value is not None:
        return SystemPower(
            power=measured(
                float(meter_power.value),
                "W",
                meter_power.source,
                meter_power.timestamp,
            ),
            mode="measured",
            other_components_w=cfg.estimated_other_power_w,
            missing_components=(),
            meter_status="ok",
            last_meter_at=meter_state.last_success,
            scope="wall",
        )

    if not meter_state.enabled:
        meter_status = "disabled"
    elif meter_state.healthy:
        meter_status = "ok"
    elif meter_state.status in {"starting", "retrying", "offline", "error"}:
        meter_status = meter_state.status
    else:
        meter_status = "offline"

    missing: list[str] = []
    if cpu is None:
        cpu_power = None
        missing.append("cpu")
    else:
        cpu_power = cpu.total_power.value
        if cpu_power is None or cpu.total_power.quality is not Quality.MEASURED:
            missing.append("cpu")

    gpu_total = 0.0
    if not gpu_available:
        missing.append("gpu")
    else:
        for gpu in gpus:
            if gpu.power.value is None or gpu.power.quality is not Quality.MEASURED:
                missing.append(f"gpu:{gpu.uuid}")
            else:
                gpu_total += float(gpu.power.value)

    if missing and cfg.strict_estimate:
        return SystemPower(
            power=unavailable(
                "W",
                "calculator",
                timestamp,
                error="missing components: " + ", ".join(missing),
                missing=tuple(missing),
            ),
            mode="unavailable",
            other_components_w=cfg.estimated_other_power_w,
            missing_components=tuple(missing),
            meter_status=meter_status,
            last_meter_at=meter_state.last_success,
            scope=None,
        )

    cpu_component = float(cpu_power) if cpu_power is not None else 0.0
    total = cpu_component + gpu_total + cfg.estimated_other_power_w
    return SystemPower(
        power=estimated(
            total,
            "W",
            "calculator:rapl+nvml",
            timestamp,
            missing=tuple(missing),
        ),
        mode="estimated",
        other_components_w=cfg.estimated_other_power_w,
        missing_components=tuple(missing),
        meter_status=meter_status,
        last_meter_at=meter_state.last_success,
        scope="dc_components",
    )


def aggregate_gpu_power(
    gpus: tuple[GpuSnapshot, ...],
    gpu_available: bool,
    timestamp: datetime,
) -> Measurement[float]:
    """Total GPU power.

    ``gpu_available=False`` means enumeration failed, which is *not* the same
    as a machine without GPUs.  Zero GPUs is a measured 0 W; failed
    enumeration is ``unavailable``.
    """
    if not gpu_available:
        return unavailable(
            "W", "nvml", timestamp, error="GPU enumeration failed", missing=("gpu",)
        )
    if not gpus:
        return measured(0.0, "W", "nvml:no-gpus", timestamp)
    missing = tuple(
        gpu.uuid for gpu in gpus if gpu.power.value is None or gpu.power.quality is not Quality.MEASURED
    )
    if missing:
        return unavailable(
            "W",
            "nvml",
            timestamp,
            error="missing GPU power: " + ", ".join(missing),
            missing=missing,
        )
    total = sum(float(gpu.power.value) for gpu in gpus if gpu.power.value is not None)
    return measured(total, "W", "nvml:gpus", timestamp)
