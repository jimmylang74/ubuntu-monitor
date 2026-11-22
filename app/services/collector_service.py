"""Periodic collection and snapshot assembly.

Each collector runs in its own asyncio task.  Blocking hardware reads are
executed with ``asyncio.to_thread`` under a timeout; a hung read keeps at most
one thread alive per component and never blocks the event loop or other
collectors.  Failed reads keep the previous value and mark the component
degraded/stale.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Callable

from app.collectors.cpu import CpuCollector
from app.collectors.disk import DiskCollector
from app.collectors.gpu import GpuCollector
from app.collectors.memory import MemoryCollector
from app.collectors.network import NetworkCollector
from app.collectors.system import SystemCollector
from app.config import AppConfig
from app.database.database import AppDatabase
from app.database.writer import DatabaseWriter
from app.domain import (
    CollectorHealth,
    CpuSnapshot,
    GpuSnapshot,
    Measurement,
    Quality,
    SystemPower,
)
from app.power.calculator import MeterState, compute_system_power
from app.power.adapters.base import MeterReading
from app.snapshot import Snapshot, empty_system_power
from app.state import EventBus, StateStore

logger = logging.getLogger(__name__)

MAX_ENERGY_DT_S = 30.0
MAX_METER_ENERGY_DELTA_WH = 5_000.0


@dataclass(slots=True)
class _Health:
    name: str
    interval_s: float
    state: str = "starting"
    last_success: datetime | None = None
    last_success_mono: float | None = None
    last_error: str | None = None
    failures: int = 0
    inflight: bool = False

    def mark_ok(self, now: datetime, mono: float) -> None:
        self.state = "ok"
        self.last_success = now
        self.last_success_mono = mono
        self.last_error = None
        self.failures = 0
        self.inflight = False

    def mark_error(self, exc: BaseException | str) -> None:
        self.state = "error"
        self.last_error = str(exc)
        self.failures += 1
        self.inflight = False

    def mark_timeout(self, timeout_s: float) -> None:
        self.state = "stale"
        self.last_error = f"collection timed out after {timeout_s:.1f}s"
        self.failures += 1

    def view(self, mono: float) -> CollectorHealth:
        state = self.state
        age_ms: int | None = None
        if self.last_success_mono is not None:
            age_ms = max(0, int((mono - self.last_success_mono) * 1000))
            stale_after = max(self.interval_s * 3.0, 5.0)
            if state == "ok" and mono - self.last_success_mono > stale_after:
                state = "stale"
        return CollectorHealth(
            name=self.name,
            state=state,
            last_success=self.last_success,
            last_error=self.last_error,
            consecutive_failures=self.failures,
            age_ms=age_ms,
        )


class CollectorService:
    def __init__(
        self,
        cfg: AppConfig,
        store: StateStore,
        bus: EventBus,
        writer: DatabaseWriter,
        database: AppDatabase,
        *,
        cpu: CpuCollector | None = None,
        gpu: GpuCollector | None = None,
        memory: MemoryCollector | None = None,
        disk: DiskCollector | None = None,
        network: NetworkCollector | None = None,
        host: SystemCollector | None = None,
        clock: Callable[[], float] = time.monotonic,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self.cfg = cfg
        self.store = store
        self.bus = bus
        self.writer = writer
        self.database = database
        self._clock = clock
        self._now = now or (lambda: datetime.now(UTC))

        self.cpu = cpu or CpuCollector(cfg, now=self._now)
        self.gpu = gpu or GpuCollector(cfg, now=self._now)
        self.memory = memory or MemoryCollector(cfg, now=self._now)
        self.disk = disk or DiskCollector(cfg, now=self._now, clock=clock)
        self.network = network or NetworkCollector(cfg, now=self._now, clock=clock)
        self.host = host or SystemCollector(cfg, now=self._now)

        self._health: dict[str, _Health] = {
            "cpu": _Health("cpu", cfg.collector.cpu_interval),
            "gpu": _Health("gpu", cfg.collector.gpu_interval),
            "memory": _Health("memory", cfg.collector.memory_interval),
            "disk": _Health("disk", cfg.collector.disk_interval),
            "network": _Health("network", cfg.collector.network_interval),
            "host": _Health("host", max(cfg.collector.system_interval, 5.0)),
            "power": _Health("power", cfg.collector.system_interval),
        }

        self._stop = asyncio.Event()
        self._tasks: list[asyncio.Task[None]] = []
        self.started_at = self._now()
        self._energy_mono: float | None = None
        self._last_persist_mono: float | None = None
        self._meter = MeterState(enabled=cfg.power.external_meter.enabled, healthy=False)
        self._meter_energy_last: tuple[str, float] | None = None

        self.store.update("gpu", ())
        self.store.update("gpu_available", False)
        self.store.update("gpu_error", None)

    # ------------------------------------------------------------------ lifecycle

    async def start(self) -> None:
        self.started_at = self._now()
        self._tasks = [
            asyncio.create_task(
                self._run_periodic("cpu", self.cpu.collect, self.cfg.collector.cpu_interval),
                name="collector-cpu",
            ),
            asyncio.create_task(
                self._run_periodic("gpu", self.gpu.collect, self.cfg.collector.gpu_interval),
                name="collector-gpu",
            ),
            asyncio.create_task(
                self._run_periodic(
                    "memory", self.memory.collect, self.cfg.collector.memory_interval
                ),
                name="collector-memory",
            ),
            asyncio.create_task(
                self._run_periodic("disk", self.disk.collect, self.cfg.collector.disk_interval),
                name="collector-disk",
            ),
            asyncio.create_task(
                self._run_periodic(
                    "network", self.network.collect, self.cfg.collector.network_interval
                ),
                name="collector-network",
            ),
            asyncio.create_task(
                self._run_periodic(
                    "host", self.host.collect, max(self.cfg.collector.system_interval, 5.0)
                ),
                name="collector-host",
            ),
            asyncio.create_task(self._run_power(), name="collector-power"),
        ]
        # Emit an initial snapshot immediately so the UI never waits in silence.
        self.publish_snapshot()

    async def stop(self) -> None:
        self._stop.set()
        for task in self._tasks:
            task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks = []
        self.gpu.close()

    # ------------------------------------------------------------------ scheduling

    async def _sleep(self, seconds: float) -> None:
        if seconds <= 0:
            await asyncio.sleep(0)
            return
        try:
            await asyncio.wait_for(self._stop.wait(), timeout=seconds)
        except TimeoutError:
            pass

    async def _run_periodic(
        self, name: str, collect: Callable[[], Any], interval: float
    ) -> None:
        health = self._health[name]
        timeout = self.cfg.collector.timeout
        while not self._stop.is_set():
            started = self._clock()
            if health.inflight:
                logger.warning("%s collector still busy; skipping this cycle", name)
                await self._sleep(interval)
                continue

            health.inflight = True
            task = asyncio.create_task(asyncio.to_thread(collect), name=f"collect-{name}")
            try:
                result = await asyncio.wait_for(asyncio.shield(task), timeout=timeout)
            except TimeoutError:
                health.mark_timeout(timeout)
                logger.warning("%s collection timed out after %.1fs", name, timeout)
                task.add_done_callback(
                    lambda finished, component=name: self._finish_late(component, finished)
                )
                await self._sleep(interval)
                continue
            except Exception as exc:  # noqa: BLE001 - collector errors are isolated
                health.mark_error(exc)
                logger.warning("%s collection failed: %s", name, exc)
            else:
                health.mark_ok(self._now(), self._clock())
                self._apply(name, result)
            await self._sleep(max(0.0, interval - (self._clock() - started)))

    def _finish_late(self, name: str, task: asyncio.Task[Any]) -> None:
        health = self._health[name]
        health.inflight = False
        if self._stop.is_set():
            return
        try:
            result = task.result()
        except asyncio.CancelledError:
            return
        except Exception as exc:  # noqa: BLE001
            health.mark_error(exc)
            logger.warning("%s late collection failed: %s", name, exc)
            return
        health.mark_ok(self._now(), self._clock())
        self._apply(name, result)

    def _apply(self, name: str, result: Any) -> None:
        if name == "cpu":
            self.store.update("cpu", result)
        elif name == "gpu":
            available = bool(getattr(self.gpu, "available", False))
            error = getattr(self.gpu, "error", None)
            self.store.update("gpu", result)
            self.store.update("gpu_available", available)
            self.store.update("gpu_error", error)
            if not available:
                health = self._health["gpu"]
                health.state = "error"
                health.last_error = error or "GPU enumeration failed"
        elif name in {"memory", "disk", "network"}:
            self.store.update(name, result)
        elif name == "host":
            self.store.update("host", result)

    # ------------------------------------------------------------------ power

    async def _run_power(self) -> None:
        interval = max(self.cfg.collector.system_interval, 0.2)
        while not self._stop.is_set():
            started = self._clock()
            try:
                self.publish_snapshot()
            except Exception as exc:  # noqa: BLE001
                self._health["power"].mark_error(exc)
                logger.exception("snapshot assembly failed")
            else:
                self._health["power"].mark_ok(self._now(), self._clock())
            await self._sleep(max(0.0, interval - (self._clock() - started)))

    def build_snapshot(self) -> Snapshot:
        timestamp = self._now()
        cpu: CpuSnapshot | None = self.store.get("cpu")
        gpus: tuple[GpuSnapshot, ...] = tuple(self.store.get("gpu") or ())
        gpu_available = bool(self.store.get("gpu_available", False))
        gpu_error: str | None = self.store.get("gpu_error")

        meter_reading: MeterReading | None = self.store.get("meter")
        meter_state: MeterState = self.store.get("meter_state") or self._meter
        meter_status: dict[str, Any] | None = self.store.get("meter_status")
        meter_power = meter_reading.power if meter_reading is not None else None

        system_power = compute_system_power(
            cpu,
            gpus,
            meter_power,
            meter_state,
            self.cfg.power,
            timestamp,
            gpu_available=gpu_available,
        )
        energy = self._integrate_energy(system_power, timestamp, meter_reading)

        return Snapshot(
            timestamp=timestamp,
            system_power=system_power,
            cpu=cpu,
            gpus=gpus,
            memory=self.store.get("memory"),
            disks=tuple(self.store.get("disk") or ()),
            network=tuple(self.store.get("network") or ()),
            host=dict(self.store.get("host") or {}),
            gpu_available=gpu_available,
            gpu_error=gpu_error,
            health=self.health(),
            database=self.writer.status(),
            energy_wh=energy,
            started_at=self.started_at,
            meter=meter_status,
        )

    def publish_snapshot(self) -> Snapshot:
        snapshot = self.build_snapshot()
        self.store.update("system", snapshot.system_power)
        self.store.update("snapshot", snapshot)
        self.bus.publish(snapshot.to_dict())
        self._maybe_persist(snapshot)
        return snapshot

    def _integrate_energy(
        self,
        system_power: SystemPower,
        timestamp: datetime,
        meter_reading: MeterReading | None = None,
    ) -> Measurement[float] | None:
        mono = self._clock()
        previous = self._energy_mono
        self._energy_mono = mono

        meter_energy = self._meter_energy_delta(system_power, timestamp, meter_reading)
        if meter_energy is not None:
            return meter_energy

        if previous is None:
            return None
        dt = mono - previous
        value = system_power.power.value
        if value is None or system_power.power.quality is Quality.UNAVAILABLE:
            return None
        if dt <= 0 or dt > MAX_ENERGY_DT_S:
            return None
        quality = (
            Quality.MEASURED
            if system_power.power.quality is Quality.MEASURED
            else Quality.ESTIMATED
        )
        return Measurement(
            value=float(value) * dt / 3600.0,
            unit="Wh",
            quality=quality,
            source=f"integrated:{system_power.power.source}",
            timestamp=timestamp,
        )

    def _meter_energy_delta(
        self,
        system_power: SystemPower,
        timestamp: datetime,
        reading: MeterReading | None,
    ) -> Measurement[float] | None:
        """Prefer the meter's cumulative register when it is trustworthy.

        A cumulative counter is only used while the meter is the measured wall
        source and reports a measured energy total.  The first reading creates
        a baseline; later readings contribute only their positive delta.  A
        reset or an implausibly large jump drops the baseline instead of
        fabricating energy.  When meter energy is unavailable, callers fall
        back to integrating power.
        """
        usable = (
            reading is not None
            and system_power.scope == "wall"
            and reading.energy_total.value is not None
            and reading.energy_total.quality is Quality.MEASURED
        )
        if not usable:
            self._meter_energy_last = None
            return None

        current = float(reading.energy_total.value)  # kWh
        source = reading.source
        previous = self._meter_energy_last
        self._meter_energy_last = (source, current)

        if previous is None or previous[0] != source:
            return None
        delta_kwh = current - previous[1]
        if delta_kwh < 0:
            logger.info("meter energy counter reset detected on %s", source)
            return None
        delta_wh = delta_kwh * 1000.0
        if delta_wh > MAX_METER_ENERGY_DELTA_WH:
            logger.warning(
                "ignoring implausible meter energy jump of %.1f Wh on %s",
                delta_wh,
                source,
            )
            return None
        return Measurement(
            value=delta_wh,
            unit="Wh",
            quality=Quality.MEASURED,
            source=f"meter-energy:{source}",
            timestamp=timestamp,
        )

    def _maybe_persist(self, snapshot: Snapshot) -> None:
        mono = self._clock()
        interval = self.cfg.database.write_interval
        # Allow a little scheduling jitter: with a publish loop that runs at
        # exactly ``write_interval``, a 1 ms undershoot would otherwise skip a
        # whole cycle and halve the effective write rate.
        tolerance = min(0.1, interval * 0.1)
        if (
            self._last_persist_mono is not None
            and mono - self._last_persist_mono < interval - tolerance
        ):
            return
        self._last_persist_mono = mono
        record, gpu_rows = snapshot.to_record()
        self.writer.submit(record, gpu_rows)

    # ------------------------------------------------------------------ health

    def health(self) -> tuple[CollectorHealth, ...]:
        mono = self._clock()
        return tuple(health.view(mono) for health in self._health.values())

    def health_dict(self) -> dict[str, Any]:
        return {item.name: item.to_dict() for item in self.health()}

    def diagnostics(self) -> dict[str, Any]:
        """Hardware probe details for ``/api/v1/diagnostics``.

        Kept on the service (not in ``api/``) so the API layer never imports a
        collector directly.
        """
        rapl = self.cpu.rapl
        nvml = self.gpu.nvml
        gpus = [
            {
                "uuid": gpu.uuid,
                "index": gpu.index,
                "name": gpu.name,
                "pci_bus_id": gpu.pci_bus_id,
                "power_w": gpu.power.value,
                "power_quality": gpu.power.quality.value,
            }
            for gpu in (self.store.get("gpu") or ())
        ]
        return {
            "config_path": str(self.cfg.source_path) if self.cfg.source_path else None,
            "rapl": {
                "helper": self.cfg.rapl.helper,
                "source_mode": rapl.source_mode,
                "helper_error": rapl.helper_error,
                "domains": [
                    {
                        "id": domain.domain_id,
                        "name": domain.name,
                        "package": domain.is_package,
                        "child": domain.is_child,
                        "enabled": domain.enabled,
                        "max_energy_range_uj": domain.max_energy_uj,
                    }
                    for domain in rapl.all_domains
                ],
            },
            "gpu": {
                "available": bool(self.store.get("gpu_available", False)),
                "error": self.store.get("gpu_error"),
                "nvml_initialized": nvml.initialized,
                "nvml_error": nvml.init_error,
                "gpus": gpus,
            },
            "collectors": self.health_dict(),
            "database": self.writer.status(),
        }

    def empty_snapshot(self) -> Snapshot:
        timestamp = self._now()
        return Snapshot(
            timestamp=timestamp,
            system_power=empty_system_power(timestamp),
            cpu=None,
            gpus=(),
            memory=None,
            disks=(),
            network=(),
            host={},
            gpu_available=bool(self.store.get("gpu_available", False)),
            gpu_error=None,
            health=self.health(),
            database=self.writer.status(),
            started_at=self.started_at,
            meter=self.store.get("meter_status"),
        )
