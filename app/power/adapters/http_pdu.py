"""Generic JSON-over-HTTP PDU/meter adapter.

Configuration is intentionally small: a URL plus dotted JSON paths.  It covers
simple LAN PDUs that expose a read-only status endpoint but do not implement
the Shelly API.
"""

from __future__ import annotations

import math
from datetime import UTC, datetime
from typing import Any, Callable

import httpx

from app.domain import Measurement, measured, unavailable
from app.power.adapters.base import MeterCapabilities, MeterError, MeterReading


def _lookup(payload: Any, path: str) -> Any:
    current = payload
    for part in path.split("."):
        if not part:
            continue
        if isinstance(current, list):
            try:
                current = current[int(part)]
            except (ValueError, IndexError):
                return None
        elif isinstance(current, dict):
            current = current.get(part)
        else:
            return None
    return current


def _number(value: Any, scale: float = 1.0) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value) * scale
    return number if math.isfinite(number) else None


class HttpPduAdapter:
    adapter_id = "http_pdu"

    def __init__(
        self,
        cfg: dict[str, Any],
        *,
        timeout: float = 2.0,
        now: Callable[[], datetime] | None = None,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        url = str(cfg.get("url", "")).strip()
        if not url:
            raise MeterError("external_meter.http_pdu.url is required")
        self.url = url
        self.power_path = str(cfg.get("power_path", "power"))
        self.energy_path = cfg.get("energy_path")
        self.voltage_path = cfg.get("voltage_path")
        self.current_path = cfg.get("current_path")
        self.power_factor_path = cfg.get("power_factor_path")
        self.scale = float(cfg.get("scale", 1.0))
        self.energy_scale = float(cfg.get("energy_scale", 1.0))
        self._now = now or (lambda: datetime.now(UTC))
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(timeout=timeout, follow_redirects=True)
        self._source = f"http_pdu:{self.url}"
        self._capabilities: MeterCapabilities | None = None

    async def _payload(self) -> dict[str, Any]:
        try:
            response = await self._client.get(self.url)
            response.raise_for_status()
            payload = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise MeterError(f"{exc.__class__.__name__}: {exc}") from exc
        if not isinstance(payload, dict):
            raise MeterError("expected a JSON object")
        return payload

    def _measurement(
        self,
        payload: dict[str, Any],
        path: str | None,
        unit: str,
        timestamp: datetime,
        scale: float = 1.0,
    ) -> Measurement[float]:
        if not path:
            return unavailable(unit, self._source, timestamp, error="not configured")
        value = _number(_lookup(payload, str(path)), scale)
        if value is None:
            return unavailable(unit, self._source, timestamp, error=f"missing path '{path}'")
        return measured(value, unit, self._source, timestamp)

    def _parse(self, payload: dict[str, Any], timestamp: datetime) -> MeterReading:
        power = self._measurement(payload, self.power_path, "W", timestamp, self.scale)
        if power.value is None:
            raise MeterError(f"power path '{self.power_path}' is missing or invalid")
        return MeterReading(
            timestamp=timestamp,
            power=power,
            energy_total=self._measurement(
                payload, str(self.energy_path) if self.energy_path else None, "kWh", timestamp,
                self.energy_scale,
            ),
            voltage=self._measurement(
                payload, str(self.voltage_path) if self.voltage_path else None, "V", timestamp
            ),
            current=self._measurement(
                payload, str(self.current_path) if self.current_path else None, "A", timestamp
            ),
            power_factor=self._measurement(
                payload,
                str(self.power_factor_path) if self.power_factor_path else None,
                "",
                timestamp,
            ),
            source=self._source,
        )

    async def probe(self) -> MeterCapabilities:
        reading = self._parse(await self._payload(), self._now())
        self._capabilities = MeterCapabilities(
            adapter_id="http_pdu",
            supports_power=True,
            supports_energy=reading.energy_total.value is not None,
            supports_voltage=reading.voltage.value is not None,
            supports_current=reading.current.value is not None,
            supports_power_factor=reading.power_factor.value is not None,
            min_interval_s=1.0,
        )
        return self._capabilities

    async def read(self) -> MeterReading:
        return self._parse(await self._payload(), self._now())

    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()
