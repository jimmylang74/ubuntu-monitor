"""External power-meter polling, failover and recovery.

The service owns exactly one ``PowerMeterAdapter``.  A failing meter never
touches CPU/GPU collection: it simply becomes unhealthy, the calculator falls
back to the component estimate and the UI can display the last successful
reading and the failure count.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from typing import Any, Callable

from app.config import AppConfig
from app.database.writer import DatabaseWriter
from app.power.adapters.base import (
    MeterCapabilities,
    MeterError,
    MeterReading,
    PowerMeterAdapter,
    build_adapter,
)
from app.power.calculator import MeterState
from app.state import StateStore

logger = logging.getLogger(__name__)

METER_SAMPLE_INTERVAL_S = 1.0


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


class MeterService:
    def __init__(
        self,
        cfg: AppConfig,
        store: StateStore,
        writer: DatabaseWriter,
        *,
        adapter: PowerMeterAdapter | None = None,
        now: Callable[[], datetime] | None = None,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self.cfg = cfg
        self.store = store
        self.writer = writer
        self.meter_cfg = cfg.power.external_meter
        self._now = now or (lambda: datetime.now(UTC))
        self._clock = clock or (lambda: asyncio.get_running_loop().time())

        self.adapter = adapter
        self.adapter_error: str | None = None
        if self.meter_cfg.enabled and self.adapter is None:
            try:
                self.adapter = build_adapter(self.meter_cfg)
            except (MeterError, ValueError) as exc:
                self.adapter_error = f"{exc.__class__.__name__}: {exc}"
                logger.error("external meter disabled: %s", self.adapter_error)

        self.capabilities: MeterCapabilities | None = None
        self.last_reading: MeterReading | None = None
        self.last_success: datetime | None = None
        self.last_attempt: datetime | None = None
        self.last_error: str | None = self.adapter_error
        self.failures = 0
        self.state = "starting" if self.meter_cfg.enabled and self.adapter else "disabled"
        if self.meter_cfg.enabled and self.adapter is None:
            self.state = "error"

        self._stop = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        self._offline_logged = False
        self._last_persist_mono: float | None = None
        self._publish()

    # ------------------------------------------------------------------ lifecycle

    async def start(self) -> None:
        if not self.meter_cfg.enabled or self.adapter is None:
            self._publish()
            return
        if self._task is None:
            self._task = asyncio.create_task(self._run(), name="meter-poller")

    async def stop(self) -> None:
        self._stop.set()
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
            self._task = None
        if self.adapter is not None:
            try:
                await self.adapter.close()
            except Exception:  # noqa: BLE001 - shutdown must never fail
                logger.exception("closing external meter adapter failed")

    # ------------------------------------------------------------------ polling

    async def _run(self) -> None:
        interval = self.meter_cfg.interval
        while not self._stop.is_set():
            started = self._clock()
            await self.poll_once()
            await self._sleep(max(0.0, interval - (self._clock() - started)))

    async def _sleep(self, seconds: float) -> None:
        if seconds <= 0:
            await asyncio.sleep(0)
            return
        try:
            await asyncio.wait_for(self._stop.wait(), timeout=seconds)
        except TimeoutError:
            pass

    async def poll_once(self) -> MeterReading | None:
        """Read once, updating health and the latest status payload.

        Kept public so tests and the CLI can trigger a read without starting a
        background task.
        """
        if not self.meter_cfg.enabled:
            self.state = "disabled"
            self._publish()
            return None
        if self.adapter is None:
            self.state = "error"
            self.last_error = self.adapter_error or "no meter adapter"
            self._publish()
            return None

        timeout = self.meter_cfg.timeout + 2.0
        self.last_attempt = self._now()
        try:
            if self.capabilities is None:
                self.capabilities = await asyncio.wait_for(self.adapter.probe(), timeout=timeout)
            reading = await asyncio.wait_for(self.adapter.read(), timeout=timeout)
        except Exception as exc:  # noqa: BLE001 - meter failures are isolated
            self._record_failure(exc)
            return None

        self._record_success(reading)
        return reading

    def _record_failure(self, exc: BaseException) -> None:
        self.failures += 1
        self.last_error = f"{exc.__class__.__name__}: {exc}"
        if self.failures >= self.meter_cfg.failover_after:
            self.state = "offline"
            if not self._offline_logged:
                self._offline_logged = True
                self._event(
                    "warning",
                    f"external meter offline after {self.failures} consecutive failures: "
                    f"{self.last_error}",
                )
        elif self.last_reading is None:
            self.state = "starting"
        else:
            self.state = "retrying"
        logger.warning("external meter read failed (%d/%d): %s", self.failures,
                       self.meter_cfg.failover_after, self.last_error)
        self._publish()

    def _record_success(self, reading: MeterReading) -> None:
        recovered = self._offline_logged or self.state == "offline"
        self.failures = 0
        self.state = "ok"
        self.last_error = None
        self.last_reading = reading
        self.last_success = reading.timestamp
        self._offline_logged = False
        if recovered:
            self._event("info", f"external meter recovered: {reading.source}")
        self._persist(reading)
        self._publish()

    def _event(self, level: str, message: str) -> None:
        self.writer.submit_event(level, "meter", message)

    def _persist(self, reading: MeterReading) -> None:
        mono = self._clock()
        if (
            self._last_persist_mono is not None
            and mono - self._last_persist_mono < METER_SAMPLE_INTERVAL_S
        ):
            return
        self._last_persist_mono = mono
        self.writer.submit_meter(
            {
                "ts_ms": int(reading.timestamp.timestamp() * 1000),
                "power_w": reading.power.value,
                "energy_kwh": reading.energy_total.value,
                "voltage_v": reading.voltage.value,
                "current_a": reading.current.value,
                "power_factor": reading.power_factor.value,
                "source": reading.source,
            }
        )

    # ------------------------------------------------------------------ state

    @property
    def healthy(self) -> bool:
        if not self.meter_cfg.enabled or self.adapter is None:
            return False
        return self.last_reading is not None and self.failures < self.meter_cfg.failover_after

    def meter_state(self) -> MeterState:
        return MeterState(
            enabled=self.meter_cfg.enabled,
            healthy=self.healthy,
            last_success=self.last_success,
            consecutive_failures=self.failures,
            status=self.state,
        )

    def status(self) -> dict[str, Any]:
        reading = self.last_reading
        return {
            "enabled": self.meter_cfg.enabled,
            "type": self.meter_cfg.type,
            "adapter_id": self.capabilities.adapter_id if self.capabilities else None,
            "state": self.state,
            "healthy": self.healthy,
            "scope": "wall" if reading and reading.power.value is not None else None,
            "energy_source": (
                "meter"
                if self.healthy and reading and reading.energy_total.value is not None
                else ("integration" if reading else None)
            ),
            "interval_s": self.meter_cfg.interval,
            "failover_after": self.meter_cfg.failover_after,
            "consecutive_failures": self.failures,
            "last_attempt": _iso(self.last_attempt),
            "last_success": _iso(self.last_success),
            "last_error": self.last_error,
            "capabilities": self.capabilities.to_dict() if self.capabilities else None,
            "reading": reading.to_dict() if reading else None,
        }

    def _publish(self) -> None:
        self.store.update("meter", self.last_reading)
        self.store.update("meter_state", self.meter_state())
        self.store.update("meter_status", self.status())
