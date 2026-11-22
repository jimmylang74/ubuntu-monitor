"""Modbus TCP power-meter adapter.

Only a single 32-bit value is read (power), which covers the common energy
meter register layouts.  Word order and float/uint decoding are configurable;
invalid responses are reported as unavailable rather than fabricated values.
"""

from __future__ import annotations

import asyncio
import math
import struct
from datetime import UTC, datetime
from typing import Any, Callable

from app.config import ModbusConfig
from app.domain import unavailable, measured
from app.power.adapters.base import MeterCapabilities, MeterError, MeterReading


def decode_registers(
    registers: list[int],
    register_type: str,
    word_order: str,
    scale: float,
) -> float:
    """Decode two 16-bit registers into a scaled float."""

    if len(registers) < 2:
        raise MeterError(f"expected 2 registers, got {len(registers)}")
    first, second = int(registers[0]) & 0xFFFF, int(registers[1]) & 0xFFFF
    if word_order == "little":
        first, second = second, first
    combined = (first << 16) | second
    if register_type == "float32":
        value = struct.unpack(">f", combined.to_bytes(4, "big"))[0]
    elif register_type == "uint32":
        value = float(combined)
    else:
        raise MeterError(f"unsupported register type '{register_type}'")
    value *= scale
    if not math.isfinite(value):
        raise MeterError("register value is not finite")
    return value


class ModbusAdapter:
    adapter_id = "modbus"

    def __init__(
        self,
        cfg: ModbusConfig,
        *,
        timeout: float = 2.0,
        now: Callable[[], datetime] | None = None,
        client: Any | None = None,
        client_factory: Callable[[], Any] | None = None,
    ) -> None:
        self.cfg = cfg
        self.timeout = timeout
        self._now = now or (lambda: datetime.now(UTC))
        self._client = client
        self._client_factory = client_factory
        self._source = f"modbus:{cfg.host}:{cfg.port}/{cfg.unit_id}"
        self._capabilities: MeterCapabilities | None = None

    # ------------------------------------------------------------------ client

    def _make_client(self) -> Any:
        if self._client is not None:
            return self._client
        if self._client_factory is not None:
            self._client = self._client_factory()
            return self._client
        try:
            from pymodbus.client import AsyncModbusTcpClient
        except ImportError as exc:  # pragma: no cover - dependency is declared
            raise MeterError(
                "pymodbus is required for the modbus adapter; install pymodbus>=3.16,<4"
            ) from exc
        self._client = AsyncModbusTcpClient(
            self.cfg.host,
            port=self.cfg.port,
            timeout=self.timeout,
            retries=1,
        )
        return self._client

    async def _ensure_connected(self) -> Any:
        client = self._make_client()
        if getattr(client, "connected", False):
            return client
        try:
            connected = await asyncio.wait_for(client.connect(), timeout=self.timeout + 1.0)
        except (TimeoutError, OSError) as exc:
            self._client = None
            raise MeterError(f"connect failed: {exc.__class__.__name__}: {exc}") from exc
        if not connected:
            self._client = None
            raise MeterError(f"cannot connect to {self.cfg.host}:{self.cfg.port}")
        return client

    async def _read_power(self) -> float:
        client = await self._ensure_connected()
        try:
            method = (
                client.read_holding_registers
                if self.cfg.function_code == 3
                else client.read_input_registers
            )
            try:
                response = await asyncio.wait_for(
                    method(
                        self.cfg.register_address,
                        count=2,
                        device_id=self.cfg.unit_id,
                    ),
                    timeout=self.timeout,
                )
            except TypeError:
                # pymodbus < 3.10 used ``slave`` instead of ``device_id``.
                response = await asyncio.wait_for(
                    method(self.cfg.register_address, count=2, slave=self.cfg.unit_id),
                    timeout=self.timeout,
                )
        except (TimeoutError, OSError) as exc:
            self._client = None
            raise MeterError(f"read failed: {exc.__class__.__name__}: {exc}") from exc
        except Exception as exc:  # pymodbus raises a variety of transport errors
            self._client = None
            raise MeterError(f"read failed: {exc.__class__.__name__}: {exc}") from exc

        if response is None or response.isError():
            raise MeterError(f"modbus error response: {response}")
        registers = getattr(response, "registers", None)
        if not isinstance(registers, list):
            raise MeterError("modbus response has no registers")
        return decode_registers(
            registers,
            self.cfg.register_type,
            self.cfg.word_order,
            self.cfg.scale,
        )

    # ------------------------------------------------------------------ adapter

    async def probe(self) -> MeterCapabilities:
        await self._read_power()
        self._capabilities = MeterCapabilities(
            adapter_id="modbus",
            supports_power=True,
            supports_energy=False,
            supports_voltage=False,
            supports_current=False,
            supports_power_factor=False,
            min_interval_s=self.cfg.min_interval_s,
        )
        return self._capabilities

    async def read(self) -> MeterReading:
        value = await self._read_power()
        timestamp = self._now()
        blank = lambda unit: unavailable(  # noqa: E731 - concise local helper
            unit, self._source, timestamp, error="not exposed by this register"
        )
        return MeterReading(
            timestamp=timestamp,
            power=measured(value, "W", self._source, timestamp),
            energy_total=blank("kWh"),
            voltage=blank("V"),
            current=blank("A"),
            power_factor=blank(""),
            source=self._source,
        )

    async def close(self) -> None:
        client = self._client
        self._client = None
        if client is not None and getattr(client, "connected", False):
            try:
                client.close()
            except Exception:  # noqa: BLE001 - close must never fail the service
                pass
