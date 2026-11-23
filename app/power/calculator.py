"""Decide between measured wall power and a component-sum estimate."""

from __future__ import annotations

import re
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


def _disk_parent(name: str) -> str:
    """Collapse common partition names to their parent block device."""
    name = name.rsplit("/", 1)[-1].strip()
    if not name:
        return ""
    if name.startswith(("nvme", "mmcblk", "md")):
        return re.sub(r"p\d+$", "", name)
    if name.startswith(("sd", "vd")):
        return re.sub(r"\d+$", "", name)
    return name


@dataclass(frozen=True, slots=True)
class OtherPowerSuggestion:
    """A transparent, hardware-derived suggestion for unmeasured components.

    This is deliberately an *estimate input*, never a measurement.  Every
    component of the value is exposed so the UI/API can show what it contains.
    """

    watts: float
    basis: str
    breakdown: dict[str, float]

    def to_dict(self) -> dict[str, object]:
        return {"watts": self.watts, "basis": self.basis, "breakdown": dict(self.breakdown)}


def suggest_other_power_w(
    *,
    sockets: int = 1,
    memory_bytes: int | None = None,
    disk_devices: tuple[str, ...] = (),
    gpu_count: int = 0,
) -> OtherPowerSuggestion:
    """Suggest watts for motherboard, memory, fans, disks and NICs.

    The formula is intentionally simple and documented, not a claim of
    measurement:

    * 60 W per CPU socket (VRM, board, chassis fans, NIC, miscellaneous)
    * 0.12 W per GiB of RAM (capped at 30 W)
    * 4 W per NVMe device, 6 W per other disk (capped at 30 W); partitions are
      collapsed to their parent block device so a disk is counted once
    * 8 W if no disk inventory is available (conservative fallback)

    GPU package power is *not* added: NVML/NVML-equivalent readings already
    cover the whole card.  The result is capped at 400 W.
    """
    socket_count = max(1, int(sockets))
    board = round(60.0 * socket_count, 1)
    memory_gib = (memory_bytes or 0) / (1024 ** 3)
    memory = round(min(30.0, memory_gib * 0.12), 1)
    devices = tuple(sorted({parent for name in disk_devices if (parent := _disk_parent(name))}))
    if devices:
        disks = round(
            min(30.0, sum(4.0 if name.startswith("nvme") else 6.0 for name in devices)),
            1,
        )
        disk_basis = f"{len(devices)} detected device(s)"
    else:
        disks = 8.0
        disk_basis = "no disk inventory; conservative fallback"
    watts = round(min(400.0, board + memory + disks), 1)
    breakdown = {
        "board_and_fans": board,
        "memory": memory,
        "disks": disks,
    }
    basis = (
        f"{socket_count} socket(s), {memory_gib:.1f} GiB RAM, {disk_basis}, "
        f"{gpu_count} GPU(s) measured separately"
    )
    return OtherPowerSuggestion(watts=watts, basis=basis, breakdown=breakdown)


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
    other_components_w: float | None = None,
    other_power_source: str | None = None,
) -> SystemPower:
    """Return system power with an explicit, non-ambiguous source.

    Measured (wall) power is only available from an external meter.  Anything
    derived from CPU/GPU readings is an estimate of DC component power and can
    never be reported as measured.

    ``gpu_available`` distinguishes "no GPU installed" (True, zero watts)
    from "GPU enumeration failed" (False, missing component).  GPUs with
    ``include_in_system_total=False`` (e.g. an APU whose hwmon reading already
    includes the CPU) are reported but never added to the component sum.
    """
    other_w = cfg.estimated_other_power_w if other_components_w is None else other_components_w
    other_source = other_power_source or "config"
    if other_w is None:
        other_w = 0.0
        other_source = other_power_source or "unavailable"

    if cfg.mode != "estimated" and meter_state.healthy and meter_power is not None and meter_power.value is not None:
        return SystemPower(
            power=measured(
                float(meter_power.value),
                "W",
                meter_power.source,
                meter_power.timestamp,
            ),
            mode="measured",
            other_components_w=other_w,
            other_power_source=other_source,
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
            if not gpu.include_in_system_total:
                continue
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
            other_components_w=other_w,
            other_power_source=other_source,
            missing_components=tuple(missing),
            meter_status=meter_status,
            last_meter_at=meter_state.last_success,
            scope=None,
        )

    cpu_component = float(cpu_power) if cpu_power is not None else 0.0
    total = cpu_component + gpu_total + other_w
    return SystemPower(
        power=estimated(
            total,
            "W",
            "calculator:components",
            timestamp,
            missing=tuple(missing),
        ),
        mode="estimated",
        other_components_w=other_w,
        other_power_source=other_source,
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
            "W", "gpu", timestamp, error="GPU enumeration failed", missing=("gpu",)
        )
    counted = tuple(gpu for gpu in gpus if gpu.include_in_system_total)
    if not counted:
        # Zero GPUs, or only APUs whose power is already inside the CPU reading.
        return measured(0.0, "W", "gpu:none-in-total", timestamp)
    missing = tuple(
        gpu.uuid for gpu in counted if gpu.power.value is None or gpu.power.quality is not Quality.MEASURED
    )
    if missing:
        return unavailable(
            "W",
            "gpu",
            timestamp,
            error="missing GPU power: " + ", ".join(missing),
            missing=missing,
        )
    total = sum(float(gpu.power.value) for gpu in counted if gpu.power.value is not None)
    return measured(total, "W", "gpus", timestamp)
