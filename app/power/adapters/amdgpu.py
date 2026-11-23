"""AMD GPU telemetry via the amdgpu/radeon hwmon sysfs interface.

NVIDIA has NVML; AMD GPUs expose equivalent power, temperature and fan data in
``/sys/class/drm/cardN/device/hwmon/hwmon*/``.  This adapter produces the same
:class:`~app.domain.GpuSnapshot` shape as the NVIDIA adapters so collectors,
API, database and UI need no vendor-specific branches.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Callable

from app.config import GpuConfig
from app.domain import GpuSnapshot, Measurement, measured, unavailable

DEFAULT_DRM_ROOT = Path("/sys/class/drm")
_CARD_RE = re.compile(r"^card(\d+)$")
_PCI_RE = re.compile(r"^\d{4}:[0-9a-fA-F]{2}:[0-9a-fA-F]{2}\.[0-7]$")


def _read_text(path: Path) -> str | None:
    try:
        return path.read_text(encoding="ascii", errors="replace").strip()
    except OSError:
        return None


def _read_int(path: Path) -> int | None:
    raw = _read_text(path)
    if raw is None:
        return None
    try:
        return int(raw)
    except ValueError:
        return None


def _read_clock_mhz(path: Path) -> float | None:
    """Read the active ``pp_dpm_*`` line (marked with ``*``)."""
    raw = _read_text(path)
    if raw is None:
        return None
    for line in raw.splitlines():
        if "*" not in line:
            continue
        match = re.search(r"(\d+(?:\.\d+)?)\s*MHz", line, re.IGNORECASE)
        if match:
            return float(match.group(1))
    return None


def _pci_bus_id(device: Path) -> str | None:
    uevent = _read_text(device / "uevent") or ""
    for line in uevent.splitlines():
        if line.startswith("PCI_SLOT_NAME="):
            return line.split("=", 1)[1].strip() or None
    try:
        name = device.resolve().name
    except (OSError, RuntimeError):
        return None
    return name if _PCI_RE.match(name) else None


def _driver_name(device: Path) -> str | None:
    try:
        return (device / "driver").resolve().name
    except (OSError, RuntimeError):
        return None


def _is_amd(device: Path, hwmon_name: str | None) -> bool:
    vendor = (_read_text(device / "vendor") or "").lower()
    if vendor in {"0x1002", "1002"}:
        return True
    driver = (_driver_name(device) or "").lower()
    if "amdgpu" in driver or "radeon" in driver:
        return True
    uevent = (_read_text(device / "uevent") or "").lower()
    if "driver=amdgpu" in uevent or "driver=radeon" in uevent:
        return True
    return hwmon_name in {"amdgpu", "radeon"}


def _card_name(device: Path, index: int, uevent: str) -> str:
    for filename in ("product_name", "board_name", "label"):
        value = _read_text(device / filename)
        if value:
            return value
    for line in uevent.splitlines():
        if line.startswith("PCI_DEVICE="):
            return f"AMD GPU {line.split('=', 1)[1].strip()}"
    return f"AMD Radeon card{index}"


def _looks_like_apu(name: str, vram_bytes: int | None, cfg: GpuConfig) -> bool:
    policy = cfg.amdgpu.apu_policy
    if policy == "exclude":
        return True
    if policy == "include":
        return False

    lower = name.lower()
    if "rx " in lower or "radeon pro" in lower:
        return False
    limit = int(cfg.amdgpu.apu_vram_limit_gb * 1024**3)
    if vram_bytes is None or vram_bytes <= limit:
        return True
    return "radeon graphics" in lower or ("vega" in lower and "graphics" in lower)


def _number(
    path: Path,
    *,
    transform: Callable[[int], float],
    unit: str,
    metric: str,
    source: str,
    timestamp: datetime,
) -> Measurement[float]:
    value = _read_int(path)
    if value is None:
        return unavailable(unit, source, timestamp, error=f"{metric} not exposed by hwmon")
    try:
        return measured(transform(value), unit, source, timestamp)
    except (OverflowError, ValueError):
        return unavailable(unit, source, timestamp, error=f"invalid {metric} value")


class AmdgpuAdapter:
    """Discover and sample AMD GPUs without any external Python dependency."""

    def __init__(
        self,
        cfg: GpuConfig,
        *,
        root: Path = DEFAULT_DRM_ROOT,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self.cfg = cfg
        self.root = root
        self._now = now or (lambda: datetime.now(UTC))
        self._initialized = True
        self.init_error: str | None = None

    @property
    def initialized(self) -> bool:
        return self._initialized

    def _hwmon_dir(self, device: Path) -> tuple[Path, str | None] | None:
        hwmon_root = device / "hwmon"
        if not hwmon_root.is_dir():
            return None
        candidates = sorted(hwmon_root.glob("hwmon*"))
        # Prefer a directory that actually exposes power.
        for hwmon in candidates:
            if (hwmon / "power1_average").is_file() or (hwmon / "power1_input").is_file():
                return hwmon, _read_text(hwmon / "name")
        if candidates:
            return candidates[0], _read_text(candidates[0] / "name")
        return None

    def sample(self) -> tuple[GpuSnapshot, ...]:
        if not self.root.is_dir():
            return ()
        snapshots: list[GpuSnapshot] = []
        for card in sorted(self.root.glob("card[0-9]*")):
            match = _CARD_RE.match(card.name)
            if not match:
                continue
            index = int(match.group(1))
            device = card / "device"
            if not device.exists():
                continue
            hwmon_info = self._hwmon_dir(device)
            if hwmon_info is None:
                continue
            hwmon, hwmon_name = hwmon_info
            if not _is_amd(device, hwmon_name):
                continue

            timestamp = self._now()
            pci_id = _pci_bus_id(device)
            uuid = f"amdgpu:{pci_id or f'card{index}'}"
            source = f"amdgpu:{pci_id or f'card{index}'}"
            uevent = _read_text(device / "uevent") or ""
            name = _card_name(device, index, uevent)

            power_file = hwmon / "power1_average"
            power_metric = "power1_average"
            if not power_file.is_file():
                power_file = hwmon / "power1_input"
                power_metric = "power1_input"
            power = _number(
                power_file,
                transform=lambda raw: raw / 1_000_000.0,
                unit="W",
                metric=power_metric,
                source=f"{source}:{power_metric}",
                timestamp=timestamp,
            )
            power_limit = _number(
                hwmon / "power1_cap",
                transform=lambda raw: raw / 1_000_000.0,
                unit="W",
                metric="power1_cap",
                source=f"{source}:power1_cap",
                timestamp=timestamp,
            )
            temperature = _number(
                hwmon / "temp1_input",
                transform=lambda raw: raw / 1000.0,
                unit="°C",
                metric="temp1_input",
                source=f"{source}:temp1_input",
                timestamp=timestamp,
            )
            fan_rpm = _number(
                hwmon / "fan1_input",
                transform=float,
                unit="rpm",
                metric="fan1_input",
                source=f"{source}:fan1_input",
                timestamp=timestamp,
            )
            if not self.cfg.expose_fan:
                fan_speed: Measurement[float] = unavailable(
                    "%", f"{source}:fan", timestamp, error="fan reporting disabled in config"
                )
                fan_rpm = unavailable(
                    "rpm", f"{source}:fan", timestamp, error="fan reporting disabled in config"
                )
            elif fan_rpm.value is None:
                fan_speed = unavailable(
                    "%", f"{source}:fan", timestamp, error="fan1_input not exposed by hwmon"
                )
            else:
                fan_max = _read_int(hwmon / "fan1_max")
                if fan_max and fan_max > 0:
                    fan_speed = measured(
                        min(100.0, float(fan_rpm.value) / fan_max * 100.0),
                        "%",
                        f"{source}:fan1_input/fan1_max",
                        timestamp,
                    )
                else:
                    fan_speed = unavailable(
                        "%", f"{source}:fan", timestamp, error="fan1_max unavailable; see fan_speed_rpm"
                    )

            vram_total = _read_int(device / "mem_info_vram_total")
            vram_used = _read_int(device / "mem_info_vram_used")
            memory_total = (
                measured(vram_total, "bytes", f"{source}:vram", timestamp)
                if vram_total is not None
                else unavailable("bytes", f"{source}:vram", timestamp, error="not exposed (shared system memory?)")
            )
            memory_used = (
                measured(vram_used, "bytes", f"{source}:vram", timestamp)
                if vram_used is not None
                else unavailable("bytes", f"{source}:vram", timestamp, error="not exposed")
            )

            utilization_raw = _read_int(device / "gpu_busy_percent")
            utilization = (
                measured(float(utilization_raw), "%", f"{source}:busy", timestamp)
                if utilization_raw is not None
                else unavailable("%", f"{source}:busy", timestamp, error="gpu_busy_percent not exposed")
            )

            core_clock = _read_clock_mhz(device / "pp_dpm_sclk")
            memory_clock = _read_clock_mhz(device / "pp_dpm_mclk")
            clock = (
                measured(core_clock, "MHz", f"{source}:pp_dpm_sclk", timestamp)
                if core_clock is not None
                else unavailable("MHz", f"{source}:pp_dpm_sclk", timestamp, error="no active clock found")
            )
            mem_clock = (
                measured(memory_clock, "MHz", f"{source}:pp_dpm_mclk", timestamp)
                if memory_clock is not None
                else unavailable("MHz", f"{source}:pp_dpm_mclk", timestamp, error="no active clock found")
            )

            snapshots.append(
                GpuSnapshot(
                    uuid=uuid,
                    index=index,
                    name=name,
                    pci_bus_id=pci_id,
                    power=power,
                    power_limit=power_limit,
                    temperature=temperature,
                    utilization=utilization,
                    memory_used=memory_used,
                    memory_total=memory_total,
                    fan_speed=fan_speed,
                    core_clock=clock,
                    memory_clock=mem_clock,
                    fan_speed_rpm=fan_rpm,
                    include_in_system_total=not _looks_like_apu(name, vram_total, self.cfg),
                )
            )
        return tuple(snapshots)

    def close(self) -> None:
        return None
