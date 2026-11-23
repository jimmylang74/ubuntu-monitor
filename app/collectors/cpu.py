"""CPU package power, temperature, frequency, topology and usage."""

from __future__ import annotations

import platform
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Callable

import psutil

from app.config import AppConfig
from app.domain import (
    CpuPackage,
    CpuSnapshot,
    Measurement,
    measured,
    unavailable,
)
from app.power.adapters.rapl import RaplAdapter, RaplSample

CPU_ROOT = Path("/sys/devices/system/cpu")
HWMON_ROOT = Path("/sys/class/hwmon")


@dataclass(frozen=True, slots=True)
class PackageTopology:
    package_id: int
    cpu_indices: tuple[int, ...]
    core_ids: tuple[int, ...]


@dataclass(frozen=True, slots=True)
class PackageTemperature:
    value: float
    source: str


def read_topology(root: Path = CPU_ROOT) -> dict[int, PackageTopology]:
    packages: dict[int, dict[str, object]] = {}
    if not root.is_dir():
        return {}
    for cpu_dir in sorted(root.glob("cpu[0-9]*")):
        match = cpu_dir.name[3:]
        if not match.isdigit():
            continue
        topology = cpu_dir / "topology"
        try:
            package_id = int((topology / "physical_package_id").read_text().strip())
            core_id = int((topology / "core_id").read_text().strip())
        except (OSError, ValueError):
            continue
        entry = packages.setdefault(package_id, {"cpus": [], "cores": set()})
        cpus = entry["cpus"]
        cores = entry["cores"]
        assert isinstance(cpus, list) and isinstance(cores, set)
        cpus.append(int(match))
        cores.add(core_id)

    result: dict[int, PackageTopology] = {}
    for package_id, entry in packages.items():
        cpus = sorted(entry["cpus"])  # type: ignore[arg-type]
        cores = tuple(sorted(entry["cores"]))  # type: ignore[arg-type]
        result[package_id] = PackageTopology(
            package_id=package_id,
            cpu_indices=tuple(cpus),
            core_ids=cores,
        )
    return result


def read_package_temperature_sources(
    hwmon_root: Path = HWMON_ROOT,
    package_ids: tuple[int, ...] | None = None,
) -> dict[int, PackageTemperature]:
    """Read Intel ``coretemp`` and AMD ``k10temp`` package temperatures.

    Intel labels carry an explicit package id.  AMD exposes one ``k10temp``
    hwmon device per socket with ``Tctl``/``Tdie`` labels, so multiple devices
    are mapped to sorted package ids from the CPU topology.
    """
    temperatures: dict[int, PackageTemperature] = {}
    if not hwmon_root.is_dir():
        return temperatures

    k10_devices: list[float] = []
    for hwmon in sorted(hwmon_root.glob("hwmon*")):
        try:
            sensor_name = (hwmon / "name").read_text().strip()
        except OSError:
            continue

        if sensor_name == "coretemp":
            for label_file in sorted(hwmon.glob("temp*_label")):
                try:
                    label = label_file.read_text().strip()
                except OSError:
                    continue
                if not label.startswith("Package id "):
                    continue
                try:
                    package_id = int(label.removeprefix("Package id "))
                    raw = int(
                        label_file.with_name(label_file.name.replace("_label", "_input")).read_text().strip()
                    )
                except (OSError, ValueError):
                    continue
                value = raw / 1000.0
                current = temperatures.get(package_id)
                if current is None or value > current.value:
                    temperatures[package_id] = PackageTemperature(value, "hwmon:coretemp")
        elif sensor_name == "k10temp":
            values: list[float] = []
            for label_file in sorted(hwmon.glob("temp*_label")):
                try:
                    label = label_file.read_text().strip()
                except OSError:
                    continue
                if label not in {"Tctl", "Tdie", "Tccd"} and not label.startswith("Tccd"):
                    continue
                try:
                    raw = int(
                        label_file.with_name(label_file.name.replace("_label", "_input")).read_text().strip()
                    )
                except (OSError, ValueError):
                    continue
                values.append(raw / 1000.0)
            if values:
                k10_devices.append(max(values))

    if k10_devices:
        ids = package_ids or tuple(range(len(k10_devices)))
        for index, value in enumerate(k10_devices):
            package_id = ids[index] if index < len(ids) else index
            current = temperatures.get(package_id)
            if current is None or value > current.value:
                temperatures[package_id] = PackageTemperature(value, "hwmon:k10temp")
    return temperatures


def read_package_temperatures(hwmon_root: Path = HWMON_ROOT) -> dict[int, float]:
    """Backwards-compatible value-only view used by CLI diagnostics."""
    return {
        package_id: item.value
        for package_id, item in read_package_temperature_sources(hwmon_root).items()
    }


def read_cpu_model() -> str:
    try:
        for line in Path("/proc/cpuinfo").read_text().splitlines():
            if line.lower().startswith("model name"):
                return line.split(":", 1)[1].strip()
    except OSError:
        pass
    return platform.processor() or "unknown CPU"


def _read_frequency_mhz(cpu_indices: tuple[int, ...], root: Path = CPU_ROOT) -> float | None:
    values: list[float] = []
    for index in cpu_indices:
        try:
            khz = int((root / f"cpu{index}" / "cpufreq" / "scaling_cur_freq").read_text().strip())
        except (OSError, ValueError):
            continue
        values.append(khz / 1000.0)
    if not values:
        return None
    return sum(values) / len(values)


def aggregate_package_power(packages: tuple[CpuPackage, ...], timestamp: datetime) -> Measurement[float]:
    if not packages:
        return unavailable("W", "rapl", timestamp, error="no CPU packages detected")
    missing = tuple(p.name for p in packages if p.power.value is None)
    if missing:
        return unavailable(
            "W",
            "rapl",
            timestamp,
            error="missing package power: " + ", ".join(missing),
            missing=missing,
        )
    total = sum(float(p.power.value) for p in packages if p.power.value is not None)
    return measured(total, "W", "rapl:packages", timestamp)


class CpuCollector:
    name = "cpu"

    def __init__(
        self,
        cfg: AppConfig,
        *,
        adapter: RaplAdapter | None = None,
        now: Callable[[], datetime] | None = None,
        clock: Callable[[], float] = time.monotonic,
        cpu_root: Path = CPU_ROOT,
        hwmon_root: Path = HWMON_ROOT,
        usage_provider: Callable[[], list[float]] | None = None,
    ) -> None:
        self.cfg = cfg
        self.interval_s = cfg.collector.cpu_interval
        self._now = now or (lambda: datetime.now(UTC))
        self._clock = clock
        self._cpu_root = cpu_root
        self._hwmon_root = hwmon_root
        self._usage_provider = usage_provider or (lambda: psutil.cpu_percent(percpu=True))
        self._adapter = adapter or RaplAdapter(cfg.rapl, clock=clock, now=self._now)
        self._topology = read_topology(cpu_root)
        self._model = read_cpu_model()
        # Prime psutil; the first non-blocking call returns zeros.
        try:
            self._usage_provider()
        except Exception:
            pass

    @property
    def rapl(self) -> RaplAdapter:
        return self._adapter

    def collect(self) -> CpuSnapshot:
        timestamp = self._now()
        rapl_samples = {sample.name: sample for sample in self._adapter.sample()}
        temperatures = read_package_temperature_sources(
            self._hwmon_root, tuple(sorted(self._topology))
        )
        usage_values = list(self._usage_provider())

        package_ids = sorted(set(self._topology) | _package_ids(rapl_samples))
        packages: list[CpuPackage] = []
        for package_id in package_ids:
            topology = self._topology.get(package_id)
            cpu_indices = topology.cpu_indices if topology else ()
            sample = rapl_samples.get(f"package-{package_id}")

            if sample is not None:
                power = _sample_power(sample, timestamp)
                energy = _sample_energy(sample, timestamp)
            else:
                power = unavailable(
                    "W", "rapl", timestamp, error=f"no RAPL package domain for package {package_id}"
                )
                energy = unavailable(
                    "J", "rapl", timestamp, error=f"no RAPL package domain for package {package_id}"
                )

            if package_id in temperatures:
                item = temperatures[package_id]
                temperature = measured(item.value, "°C", item.source, timestamp)
            else:
                temperature = unavailable(
                    "°C",
                    "hwmon",
                    timestamp,
                    error=f"no coretemp/k10temp reading for package {package_id}",
                )

            frequency_value = _read_frequency_mhz(cpu_indices, self._cpu_root)
            if frequency_value is None:
                frequency = unavailable(
                    "MHz", "sysfs:scaling_cur_freq", timestamp, error="CPU frequency unavailable"
                )
            else:
                frequency = measured(frequency_value, "MHz", "sysfs:scaling_cur_freq", timestamp)

            package_usage = [usage_values[i] for i in cpu_indices if i < len(usage_values)]
            if package_usage:
                usage = measured(sum(package_usage) / len(package_usage), "%", "psutil:cpu_percent", timestamp)
            else:
                usage = unavailable("%", "psutil:cpu_percent", timestamp, error="no CPU usage samples")

            packages.append(
                CpuPackage(
                    package_id=package_id,
                    name=f"package-{package_id}",
                    power=power,
                    energy_j=energy,
                    temperature=temperature,
                    frequency_mhz=frequency,
                    usage_percent=usage,
                    core_count=len(topology.core_ids) if topology else 0,
                    thread_count=len(cpu_indices),
                )
            )

        package_tuple = tuple(packages)
        total_power = aggregate_package_power(package_tuple, timestamp)

        if usage_values:
            usage_total = measured(
                sum(usage_values) / len(usage_values), "%", "psutil:cpu_percent", timestamp
            )
        else:
            usage_total = unavailable("%", "psutil:cpu_percent", timestamp, error="no CPU usage samples")

        temp_packages = [p for p in package_tuple if p.temperature.value is not None]
        if temp_packages:
            hottest = max(temp_packages, key=lambda p: float(p.temperature.value or 0.0))
            max_temp = measured(
                float(hottest.temperature.value or 0.0),
                "°C",
                hottest.temperature.source,
                timestamp,
            )
        else:
            max_temp = unavailable("°C", "hwmon", timestamp, error="no CPU package temperature")

        return CpuSnapshot(
            timestamp=timestamp,
            model=self._model,
            socket_count=len(self._topology) or len(package_ids),
            packages=package_tuple,
            total_power=total_power,
            usage_percent=usage_total,
            max_temperature=max_temp,
        )


def _package_ids(samples: dict[str, RaplSample]) -> set[int]:
    ids: set[int] = set()
    for name in samples:
        suffix = name.removeprefix("package-")
        if suffix.isdigit():
            ids.add(int(suffix))
    return ids


def _sample_power(sample: RaplSample, timestamp: datetime) -> Measurement[float]:
    if sample.power_w is None or sample.error is not None:
        return unavailable("W", sample.source, timestamp, error=sample.error or "RAPL power unavailable")
    return measured(sample.power_w, "W", sample.source, sample.timestamp)


def _sample_energy(sample: RaplSample, timestamp: datetime) -> Measurement[float]:
    if sample.energy_j is None:
        return unavailable("J", sample.source, timestamp, error=sample.error or "RAPL energy unavailable")
    return measured(sample.energy_j, "J", sample.source, sample.timestamp)
