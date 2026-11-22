"""NVIDIA GPU power telemetry via NVML, with an nvidia-smi fallback."""

from __future__ import annotations

import csv
import subprocess
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Callable

from app.config import GpuConfig
from app.domain import GpuSnapshot, Measurement, measured, unavailable


def _as_str(value: Any) -> str:
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace").strip()
    return str(value).strip()


def _nvml_error(exc: Exception) -> str:
    text = str(exc).strip()
    return text or exc.__class__.__name__


@dataclass(frozen=True, slots=True)
class GpuInfo:
    uuid: str
    index: int
    name: str
    pci_bus_id: str | None


class NvidiaAdapter:
    """NVML reader keyed by GPU UUID.

    Initialization failures never raise to the caller: the adapter keeps the
    error, retries with a backoff, and returns no GPUs in the meantime.
    """

    def __init__(
        self,
        cfg: GpuConfig,
        *,
        module: Any | None = None,
        clock: Callable[[], float] = time.monotonic,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self.cfg = cfg
        self._module = module
        self._clock = clock
        self._now = now or (lambda: datetime.now(UTC))
        self._initialized = False
        self._last_attempt = float("-inf")
        self._handles: dict[str, Any] = {}
        self._infos: dict[str, GpuInfo] = {}
        self._enumeration_errors: list[str] = []
        self.init_error: str | None = None

    @property
    def initialized(self) -> bool:
        return self._initialized

    @property
    def gpus(self) -> tuple[GpuInfo, ...]:
        return tuple(sorted(self._infos.values(), key=lambda item: item.index))

    @property
    def enumeration_errors(self) -> tuple[str, ...]:
        return tuple(self._enumeration_errors)

    def _load_module(self) -> Any | None:
        if self._module is not None:
            return self._module
        try:
            import pynvml  # type: ignore[import-not-found]
        except ImportError as exc:
            self.init_error = f"pynvml (nvidia-ml-py) not installed: {exc}"
            return None
        self._module = pynvml
        return pynvml

    def ensure_initialized(self) -> bool:
        if self._initialized:
            return True
        now = self._clock()
        if self.init_error is not None and now - self._last_attempt < self.cfg.reinit_interval:
            return False
        self._last_attempt = now

        module = self._load_module()
        if module is None:
            return False
        try:
            module.nvmlInit()
        except Exception as exc:
            self.init_error = _nvml_error(exc)
            return False

        self._initialized = True
        self.init_error = None
        self._enumerate(module)
        return True

    def _enumerate(self, module: Any) -> None:
        infos: dict[str, GpuInfo] = {}
        handles: dict[str, Any] = {}
        enumeration_errors: list[str] = []
        try:
            count = int(module.nvmlDeviceGetCount())
        except Exception as exc:
            self._initialized = False
            self.init_error = _nvml_error(exc)
            return

        for index in range(count):
            try:
                handle = module.nvmlDeviceGetHandleByIndex(index)
                uuid = _as_str(module.nvmlDeviceGetUUID(handle))
                name = _as_str(module.nvmlDeviceGetName(handle))
                try:
                    pci_bus_id = _as_str(module.nvmlDeviceGetPciInfo(handle).busId) or None
                except Exception:
                    pci_bus_id = None
                infos[uuid] = GpuInfo(uuid=uuid, index=index, name=name, pci_bus_id=pci_bus_id)
                handles[uuid] = handle
            except Exception as exc:
                enumeration_errors.append(f"GPU index {index}: {_nvml_error(exc)}")

        self._infos = infos
        self._handles = handles
        self._enumeration_errors = enumeration_errors

    def _metric(
        self,
        fn: Callable[[], Any],
        transform: Callable[[Any], Any],
        unit: str,
        source: str,
        timestamp: datetime,
    ) -> Measurement[Any]:
        try:
            value = transform(fn())
        except Exception as exc:
            return unavailable(unit, source, timestamp, error=_nvml_error(exc))
        if value is None:
            return unavailable(unit, source, timestamp, error="not supported by this GPU")
        return measured(value, unit, source, timestamp)

    def sample(self) -> tuple[GpuSnapshot, ...]:
        if not self.ensure_initialized():
            return ()
        module = self._module
        assert module is not None
        timestamp = self._now()
        snapshots: list[GpuSnapshot] = []

        for info in self.gpus:
            handle = self._handles.get(info.uuid)
            source = f"nvml:{info.uuid}"
            if handle is None:
                error = "GPU handle unavailable (device may have been removed)"
                snapshots.append(
                    GpuSnapshot(
                        uuid=info.uuid,
                        index=info.index,
                        name=info.name,
                        pci_bus_id=info.pci_bus_id,
                        power=unavailable("W", source, timestamp, error=error),
                        power_limit=unavailable("W", source, timestamp, error=error),
                        temperature=unavailable("°C", source, timestamp, error=error),
                        utilization=unavailable("%", source, timestamp, error=error),
                        memory_used=unavailable("bytes", source, timestamp, error=error),
                        memory_total=unavailable("bytes", source, timestamp, error=error),
                        fan_speed=unavailable("%", source, timestamp, error=error),
                        core_clock=unavailable("MHz", source, timestamp, error=error),
                        memory_clock=unavailable("MHz", source, timestamp, error=error),
                    )
                )
                continue

            def power_limit() -> Any:
                last_error: Exception | None = None
                for name in ("nvmlDeviceGetPowerManagementLimit", "nvmlDeviceGetEnforcedPowerLimit"):
                    fn = getattr(module, name, None)
                    if fn is None:
                        continue
                    try:
                        return fn(handle)
                    except Exception as exc:
                        last_error = exc
                if last_error is not None:
                    raise last_error
                raise RuntimeError("power limit API unavailable")

            def clock(clock_type: Any) -> Callable[[], Any]:
                return lambda: module.nvmlDeviceGetClockInfo(handle, clock_type)

            snapshots.append(
                GpuSnapshot(
                    uuid=info.uuid,
                    index=info.index,
                    name=info.name,
                    pci_bus_id=info.pci_bus_id,
                    power=self._metric(
                        lambda: module.nvmlDeviceGetPowerUsage(handle),
                        lambda mw: float(mw) / 1000.0,
                        "W",
                        source,
                        timestamp,
                    ),
                    power_limit=self._metric(
                        power_limit,
                        lambda mw: float(mw) / 1000.0,
                        "W",
                        source,
                        timestamp,
                    ),
                    temperature=self._metric(
                        lambda: module.nvmlDeviceGetTemperature(handle, module.NVML_TEMPERATURE_GPU),
                        float,
                        "°C",
                        source,
                        timestamp,
                    ),
                    utilization=self._metric(
                        lambda: module.nvmlDeviceGetUtilizationRates(handle).gpu,
                        float,
                        "%",
                        source,
                        timestamp,
                    ),
                    memory_used=self._metric(
                        lambda: module.nvmlDeviceGetMemoryInfo(handle).used,
                        int,
                        "bytes",
                        source,
                        timestamp,
                    ),
                    memory_total=self._metric(
                        lambda: module.nvmlDeviceGetMemoryInfo(handle).total,
                        int,
                        "bytes",
                        source,
                        timestamp,
                    ),
                    fan_speed=self._metric(
                        lambda: module.nvmlDeviceGetFanSpeed(handle),
                        float,
                        "%",
                        source,
                        timestamp,
                    ),
                    core_clock=self._metric(
                        clock(module.NVML_CLOCK_SM),
                        float,
                        "MHz",
                        source,
                        timestamp,
                    ),
                    memory_clock=self._metric(
                        clock(module.NVML_CLOCK_MEM),
                        float,
                        "MHz",
                        source,
                        timestamp,
                    ),
                )
            )

        return tuple(snapshots)

    def close(self) -> None:
        if self._initialized and self._module is not None:
            try:
                self._module.nvmlShutdown()
            except Exception:
                pass
        self._initialized = False


SMI_FIELDS = (
    "uuid",
    "name",
    "power.draw",
    "power.limit",
    "temperature.gpu",
    "utilization.gpu",
    "memory.used",
    "memory.total",
    "fan.speed",
    "clocks.sm",
    "clocks.mem",
    "pci.bus_id",
)

_NULL_CELLS = {"", "n/a", "[n/a]", "[not supported]", "[unknown error]", "not supported"}


def _cell_float(value: str) -> float | None:
    text = value.strip()
    if text.lower() in _NULL_CELLS:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def _cell_int(value: str) -> int | None:
    number = _cell_float(value)
    return None if number is None else int(number)


def parse_nvidia_smi_csv(text: str, timestamp: datetime) -> tuple[GpuSnapshot, ...]:
    """Parse ``nvidia-smi --query-gpu=... --format=csv,noheader,nounits``."""
    snapshots: list[GpuSnapshot] = []
    for index, row in enumerate(csv.reader(text.splitlines())):
        if len(row) < len(SMI_FIELDS):
            continue
        cells = dict(zip(SMI_FIELDS, (cell.strip() for cell in row), strict=False))
        uuid = cells["uuid"]
        name = cells["name"]
        if not uuid or uuid.lower() in _NULL_CELLS:
            continue
        source = f"nvidia-smi:{uuid}"

        def power(cell: str, field_name: str) -> Measurement[float]:
            value = _cell_float(cells[cell])
            if value is None:
                return unavailable("W", source, timestamp, error=f"{field_name} not supported by nvidia-smi")
            return measured(value, "W", source, timestamp)

        def temperature() -> Measurement[float]:
            value = _cell_float(cells["temperature.gpu"])
            if value is None:
                return unavailable("°C", source, timestamp, error="temperature not supported by nvidia-smi")
            return measured(value, "°C", source, timestamp)

        def utilization() -> Measurement[float]:
            value = _cell_float(cells["utilization.gpu"])
            if value is None:
                return unavailable("%", source, timestamp, error="utilization not supported by nvidia-smi")
            return measured(value, "%", source, timestamp)

        def memory(cell: str, field_name: str) -> Measurement[int]:
            value = _cell_int(cells[cell])
            if value is None:
                return unavailable("bytes", source, timestamp, error=f"{field_name} not supported by nvidia-smi")
            return measured(value * 1024 * 1024, "bytes", source, timestamp)

        def percent() -> Measurement[float]:
            value = _cell_float(cells["fan.speed"])
            if value is None:
                return unavailable("%", source, timestamp, error="fan speed not supported by nvidia-smi")
            return measured(value, "%", source, timestamp)

        def clock(cell: str, field_name: str) -> Measurement[float]:
            value = _cell_float(cells[cell])
            if value is None:
                return unavailable("MHz", source, timestamp, error=f"{field_name} not supported by nvidia-smi")
            return measured(value, "MHz", source, timestamp)

        snapshots.append(
            GpuSnapshot(
                uuid=uuid,
                index=index,
                name=name,
                pci_bus_id=cells["pci.bus_id"] or None,
                power=power("power.draw", "power.draw"),
                power_limit=power("power.limit", "power.limit"),
                temperature=temperature(),
                utilization=utilization(),
                memory_used=memory("memory.used", "memory.used"),
                memory_total=memory("memory.total", "memory.total"),
                fan_speed=percent(),
                core_clock=clock("clocks.sm", "clocks.sm"),
                memory_clock=clock("clocks.mem", "clocks.mem"),
            )
        )
    return tuple(snapshots)


class NvidiaSmiAdapter:
    """Fallback reader; only used when NVML cannot initialize."""

    def __init__(
        self,
        cfg: GpuConfig,
        *,
        now: Callable[[], datetime] | None = None,
        runner: Callable[[], str] | None = None,
    ) -> None:
        self.cfg = cfg
        self._now = now or (lambda: datetime.now(UTC))
        self._runner = runner
        self.error: str | None = None

    def _run(self) -> str:
        if self._runner is not None:
            return self._runner()
        query = ",".join(SMI_FIELDS)
        try:
            proc = subprocess.run(
                ["nvidia-smi", f"--query-gpu={query}", "--format=csv,noheader,nounits"],
                capture_output=True,
                text=True,
                timeout=3.0,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise RuntimeError(f"nvidia-smi failed: {exc}") from exc
        if proc.returncode != 0:
            detail = (proc.stderr or proc.stdout).strip() or f"exit {proc.returncode}"
            raise RuntimeError(f"nvidia-smi failed: {detail}")
        return proc.stdout

    def sample(self) -> tuple[GpuSnapshot, ...]:
        try:
            output = self._run()
        except RuntimeError as exc:
            self.error = str(exc)
            return ()
        self.error = None
        return parse_nvidia_smi_csv(output, self._now())
