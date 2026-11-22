"""External power-meter adapter contract.

Meter adapters are deliberately isolated from CPU/GPU collection: they only
return :class:`MeterReading` values with full provenance.  A failing or missing
meter can therefore never take down the rest of the service.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol, runtime_checkable

from app.config import ExternalMeterConfig
from app.domain import Measurement


class MeterError(RuntimeError):
    """Raised when a meter cannot be reached or returns invalid data."""


@dataclass(frozen=True, slots=True)
class MeterCapabilities:
    adapter_id: str
    supports_power: bool = True
    supports_energy: bool = False
    supports_voltage: bool = False
    supports_current: bool = False
    supports_power_factor: bool = False
    min_interval_s: float = 1.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "adapter_id": self.adapter_id,
            "supports_power": self.supports_power,
            "supports_energy": self.supports_energy,
            "supports_voltage": self.supports_voltage,
            "supports_current": self.supports_current,
            "supports_power_factor": self.supports_power_factor,
            "min_interval_s": self.min_interval_s,
        }


@dataclass(frozen=True, slots=True)
class MeterReading:
    timestamp: datetime
    power: Measurement[float]
    energy_total: Measurement[float]
    voltage: Measurement[float]
    current: Measurement[float]
    power_factor: Measurement[float]
    source: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "timestamp": self.timestamp.isoformat(),
            "source": self.source,
            "power": self.power.to_dict(),
            "energy_total": self.energy_total.to_dict(),
            "voltage": self.voltage.to_dict(),
            "current": self.current.to_dict(),
            "power_factor": self.power_factor.to_dict(),
        }


@runtime_checkable
class PowerMeterAdapter(Protocol):
    adapter_id: str

    async def probe(self) -> MeterCapabilities: ...

    async def read(self) -> MeterReading: ...

    async def close(self) -> None: ...


def build_adapter(cfg: ExternalMeterConfig) -> PowerMeterAdapter | None:
    """Build the configured adapter, or ``None`` when metering is disabled.

    Imports are local so an unused adapter (and its optional dependency) never
    slows down normal startup.
    """

    if not cfg.enabled:
        return None
    if cfg.type == "shelly":
        from app.power.adapters.shelly import ShellyAdapter

        if not cfg.shelly.url.strip():
            raise MeterError("external_meter.shelly.url is required for the shelly adapter")
        return ShellyAdapter(cfg.shelly, timeout=cfg.timeout)
    if cfg.type == "modbus":
        from app.power.adapters.modbus import ModbusAdapter

        if cfg.modbus is None:
            raise MeterError("external_meter.modbus is required for the modbus adapter")
        if not cfg.modbus.host.strip():
            raise MeterError("external_meter.modbus.host is required for the modbus adapter")
        return ModbusAdapter(cfg.modbus, timeout=cfg.timeout)
    if cfg.type == "http_pdu":
        from app.power.adapters.http_pdu import HttpPduAdapter

        return HttpPduAdapter(cfg.http_pdu, timeout=cfg.timeout)
    raise MeterError(f"unsupported external meter type '{cfg.type}'")
