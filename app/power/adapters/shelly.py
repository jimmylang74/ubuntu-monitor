"""Shelly Gen1/Gen2 power-meter adapter (read-only HTTP)."""

from __future__ import annotations

import math
from datetime import UTC, datetime
from typing import Any, Callable

import httpx

from app.config import ShellyConfig
from app.domain import Measurement, measured, unavailable
from app.power.adapters.base import MeterCapabilities, MeterError, MeterReading


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


class ShellyAdapter:
    adapter_id = "shelly"

    def __init__(
        self,
        cfg: ShellyConfig,
        *,
        timeout: float = 2.0,
        now: Callable[[], datetime] | None = None,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.url = cfg.url.rstrip("/")
        self.generation = cfg.generation
        self._now = now or (lambda: datetime.now(UTC))
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(timeout=timeout, follow_redirects=True)
        self._capabilities: MeterCapabilities | None = None
        self._source = f"shelly:{self.url}"

    # ------------------------------------------------------------------ HTTP

    async def _get_json(self, path: str) -> dict[str, Any]:
        try:
            response = await self._client.get(f"{self.url}{path}")
            response.raise_for_status()
            payload = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise MeterError(f"{path}: {exc.__class__.__name__}: {exc}") from exc
        if not isinstance(payload, dict):
            raise MeterError(f"{path}: expected a JSON object")
        return payload

    # ------------------------------------------------------------------ parsing

    def _measured(self, value: Any, unit: str, timestamp: datetime) -> Measurement[float]:
        number = _number(value)
        if number is None:
            return unavailable(unit, self._source, timestamp, error="field missing or invalid")
        return measured(number, unit, self._source, timestamp)

    def _parse_gen2(self, payload: dict[str, Any], timestamp: datetime) -> MeterReading:
        channel = payload.get("switch:0")
        if not isinstance(channel, dict):
            raise MeterError("Shelly Gen2 status has no 'switch:0' channel")
        energy_block = channel.get("aenergy")
        energy_wh = energy_block.get("total") if isinstance(energy_block, dict) else None
        energy_kwh = _number(energy_wh)
        energy = (
            measured(energy_kwh / 1000.0, "kWh", self._source, timestamp)
            if energy_kwh is not None
            else unavailable("kWh", self._source, timestamp, error="aenergy.total missing")
        )
        return MeterReading(
            timestamp=timestamp,
            power=self._measured(channel.get("apower"), "W", timestamp),
            energy_total=energy,
            voltage=self._measured(channel.get("voltage"), "V", timestamp),
            current=self._measured(channel.get("current"), "A", timestamp),
            power_factor=self._measured(
                channel.get("pf", channel.get("power_factor")), "", timestamp
            ),
            source=self._source,
        )

    def _parse_gen1(self, payload: dict[str, Any], timestamp: datetime) -> MeterReading:
        meters = payload.get("meters")
        if not isinstance(meters, list) or not meters or not isinstance(meters[0], dict):
            raise MeterError("Shelly Gen1 status has no 'meters[0]' channel")
        channel = meters[0]
        return MeterReading(
            timestamp=timestamp,
            power=self._measured(channel.get("power"), "W", timestamp),
            # Gen1 ``total`` uses Watt-minutes in some firmware generations; the
            # unit is not unambiguous enough to report as kWh.  Power
            # integration is used instead until the device model is known.
            energy_total=unavailable(
                "kWh",
                self._source,
                timestamp,
                error="Shelly Gen1 total units are ambiguous; using power integration",
            ),
            voltage=unavailable("V", self._source, timestamp, error="not exposed by Gen1"),
            current=unavailable("A", self._source, timestamp, error="not exposed by Gen1"),
            power_factor=unavailable("", self._source, timestamp, error="not exposed by Gen1"),
            source=self._source,
        )

    # ------------------------------------------------------------------ adapter

    async def probe(self) -> MeterCapabilities:
        errors: list[str] = []
        if self.generation in {"auto", "gen2"}:
            try:
                reading = self._parse_gen2(
                    await self._get_json("/rpc/Shelly.GetStatus"), self._now()
                )
            except MeterError as exc:
                errors.append(f"gen2: {exc}")
            else:
                self.generation = "gen2"
                self._capabilities = self._capabilities_for("gen2", reading)
                return self._capabilities
        if self.generation in {"auto", "gen1"}:
            try:
                reading = self._parse_gen1(await self._get_json("/status"), self._now())
            except MeterError as exc:
                errors.append(f"gen1: {exc}")
            else:
                self.generation = "gen1"
                self._capabilities = self._capabilities_for("gen1", reading)
                return self._capabilities
        raise MeterError("Shelly probe failed: " + "; ".join(errors))

    @staticmethod
    def _capabilities_for(generation: str, reading: MeterReading) -> MeterCapabilities:
        return MeterCapabilities(
            adapter_id=f"shelly:{generation}",
            supports_power=True,
            supports_energy=reading.energy_total.value is not None,
            supports_voltage=reading.voltage.value is not None,
            supports_current=reading.current.value is not None,
            supports_power_factor=reading.power_factor.value is not None,
            min_interval_s=0.2,
        )

    async def read(self) -> MeterReading:
        if self.generation == "auto":
            await self.probe()
        timestamp = self._now()
        if self.generation == "gen2":
            return self._parse_gen2(await self._get_json("/rpc/Shelly.GetStatus"), timestamp)
        return self._parse_gen1(await self._get_json("/status"), timestamp)

    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()
