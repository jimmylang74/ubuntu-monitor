from __future__ import annotations

import struct
from datetime import UTC, datetime

import httpx
import pytest

from app.config import ModbusConfig, ShellyConfig
from app.domain import Quality
from app.power.adapters.base import MeterError
from app.power.adapters.http_pdu import HttpPduAdapter
from app.power.adapters.modbus import ModbusAdapter, decode_registers
from app.power.adapters.shelly import ShellyAdapter

NOW = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)


def client_for(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def shelly_gen2_payload(power: float = 123.4, energy_wh: float = 12_345.6) -> dict:
    return {
        "switch:0": {
            "apower": power,
            "voltage": 230.1,
            "current": 0.54,
            "pf": 0.98,
            "aenergy": {"total": energy_wh},
        }
    }


# ---------------------------------------------------------------------- shelly


async def test_shelly_gen2_reads_power_and_energy() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/rpc/Shelly.GetStatus"
        return httpx.Response(200, json=shelly_gen2_payload())

    client = client_for(handler)
    adapter = ShellyAdapter(ShellyConfig(url="http://meter"), client=client, now=lambda: NOW)
    try:
        capabilities = await adapter.probe()
        assert capabilities.adapter_id == "shelly:gen2"
        assert capabilities.supports_power is True
        assert capabilities.supports_energy is True
        assert capabilities.supports_voltage is True

        reading = await adapter.read()
    finally:
        await adapter.close()

    assert reading.power.value == pytest.approx(123.4)
    assert reading.power.quality is Quality.MEASURED
    assert reading.energy_total.value == pytest.approx(12.3456)
    assert reading.energy_total.unit == "kWh"
    assert reading.voltage.value == pytest.approx(230.1)
    assert reading.source == "shelly:http://meter"


async def test_shelly_gen1_reads_power_without_energy() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/status"
        return httpx.Response(200, json={"meters": [{"power": 42.0}]})

    client = client_for(handler)
    adapter = ShellyAdapter(
        ShellyConfig(url="http://meter", generation="gen1"), client=client, now=lambda: NOW
    )
    try:
        capabilities = await adapter.probe()
        reading = await adapter.read()
    finally:
        await adapter.close()

    assert capabilities.adapter_id == "shelly:gen1"
    assert capabilities.supports_energy is False
    assert reading.power.value == pytest.approx(42.0)
    # Gen1 Watt-minute totals must never be guessed into kWh.
    assert reading.energy_total.value is None
    assert reading.energy_total.quality is Quality.UNAVAILABLE


async def test_shelly_auto_falls_back_to_gen1() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/status":
            return httpx.Response(200, json={"meters": [{"power": 11.0}]})
        return httpx.Response(404, text="not found")

    client = client_for(handler)
    adapter = ShellyAdapter(ShellyConfig(url="http://meter"), client=client, now=lambda: NOW)
    try:
        capabilities = await adapter.probe()
        reading = await adapter.read()
    finally:
        await adapter.close()

    assert capabilities.adapter_id == "shelly:gen1"
    assert reading.power.value == pytest.approx(11.0)


async def test_shelly_invalid_payload_raises_meter_error() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"unexpected": True})

    client = client_for(handler)
    adapter = ShellyAdapter(ShellyConfig(url="http://meter"), client=client, now=lambda: NOW)
    try:
        with pytest.raises(MeterError):
            await adapter.probe()
    finally:
        await adapter.close()


async def test_shelly_http_failure_raises_meter_error() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="boom")

    client = client_for(handler)
    adapter = ShellyAdapter(
        ShellyConfig(url="http://meter", generation="gen2"), client=client, now=lambda: NOW
    )
    try:
        with pytest.raises(MeterError):
            await adapter.read()
    finally:
        await adapter.close()


# ---------------------------------------------------------------------- modbus


def test_decode_registers_uint32_and_scale() -> None:
    assert decode_registers([0x0001, 0x0002], "uint32", "big", 1.0) == pytest.approx(65_538.0)
    assert decode_registers([0x0001, 0x0002], "uint32", "little", 1.0) == pytest.approx(
        0x0002_0001
    )
    assert decode_registers([0x0001, 0x0002], "uint32", "big", 0.5) == pytest.approx(32_769.0)


def test_decode_registers_float32_both_word_orders() -> None:
    raw = struct.pack(">f", 230.5)
    registers = [
        int.from_bytes(raw[0:2], "big"),
        int.from_bytes(raw[2:4], "big"),
    ]
    assert decode_registers(registers, "float32", "big", 1.0) == pytest.approx(230.5)
    assert decode_registers(list(reversed(registers)), "float32", "little", 2.0) == pytest.approx(
        461.0
    )


def test_decode_registers_rejects_invalid_input() -> None:
    with pytest.raises(MeterError):
        decode_registers([1], "uint32", "big", 1.0)
    with pytest.raises(MeterError):
        decode_registers([1, 2], "int16", "big", 1.0)
    nan = struct.pack(">f", float("nan"))
    with pytest.raises(MeterError):
        decode_registers(
            [int.from_bytes(nan[0:2], "big"), int.from_bytes(nan[2:4], "big")],
            "float32",
            "big",
            1.0,
        )


class FakeResponse:
    def __init__(self, registers: list[int] | None = None, *, error: bool = False) -> None:
        self.registers = registers if registers is not None else []
        self._error = error

    def isError(self) -> bool:
        return self._error


class FakeModbusClient:
    def __init__(self, response: FakeResponse | Exception) -> None:
        self.response = response
        self.connected = True
        self.calls: list[dict] = []
        self.closed = False

    async def read_holding_registers(self, address, *, count, device_id=None):
        self.calls.append({"address": address, "count": count, "device_id": device_id})
        if isinstance(self.response, Exception):
            raise self.response
        return self.response

    async def read_input_registers(self, address, *, count, device_id=None):
        self.calls.append(
            {"address": address, "count": count, "device_id": device_id, "input": True}
        )
        return self.response

    def close(self) -> None:
        self.closed = True


def modbus_cfg(**kwargs) -> ModbusConfig:
    values = {"host": "192.168.1.50", "unit_id": 7, "register_type": "float32"}
    values.update(kwargs)
    return ModbusConfig(**values)


async def test_modbus_adapter_reads_and_probes() -> None:
    raw = struct.pack(">f", 321.5)
    registers = [int.from_bytes(raw[0:2], "big"), int.from_bytes(raw[2:4], "big")]
    fake = FakeModbusClient(FakeResponse(registers))
    adapter = ModbusAdapter(modbus_cfg(register=12), client=fake, now=lambda: NOW)

    capabilities = await adapter.probe()
    reading = await adapter.read()

    assert capabilities.adapter_id == "modbus"
    assert capabilities.supports_power is True
    assert capabilities.supports_energy is False
    assert reading.power.value == pytest.approx(321.5)
    assert reading.power.quality is Quality.MEASURED
    assert reading.energy_total.value is None
    assert fake.calls[0] == {"address": 12, "count": 2, "device_id": 7}

    await adapter.close()
    assert fake.closed is True


async def test_modbus_adapter_uses_slave_keyword_for_old_pymodbus() -> None:
    class OldClient(FakeModbusClient):
        async def read_holding_registers(self, address, *, count, slave):  # noqa: ARG002
            self.calls.append({"address": address, "count": count, "slave": slave})
            return self.response

    fake = OldClient(FakeResponse([0x0000, 0x0064]))
    adapter = ModbusAdapter(
        modbus_cfg(register_type="uint32"), client=fake, now=lambda: NOW
    )

    reading = await adapter.read()

    assert reading.power.value == pytest.approx(100.0)
    assert fake.calls[0]["slave"] == 7


async def test_modbus_adapter_reports_error_responses() -> None:
    adapter = ModbusAdapter(
        modbus_cfg(), client=FakeModbusClient(FakeResponse(error=True)), now=lambda: NOW
    )
    with pytest.raises(MeterError):
        await adapter.read()


async def test_modbus_adapter_reports_transport_failures_and_reconnects() -> None:
    fake = FakeModbusClient(OSError("network down"))
    adapter = ModbusAdapter(
        modbus_cfg(register_type="uint32"), client=fake, now=lambda: NOW
    )

    with pytest.raises(MeterError):
        await adapter.read()
    # A failed client is dropped so the next poll re-connects instead of reusing it.
    assert adapter._client is None

    replacement = FakeModbusClient(FakeResponse([0x0000, 0x0064]))
    adapter._client = replacement
    reading = await adapter.read()
    assert reading.power.value == pytest.approx(100.0)


# ---------------------------------------------------------------------- http pdu


async def test_http_pdu_reads_nested_paths_and_scales() -> None:
    payload = {
        "data": [{"power": 250.0}],
        "energy": {"total": 1234.0},
        "line": {"voltage": 229.4},
    }

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=payload)

    client = client_for(handler)
    adapter = HttpPduAdapter(
        {
            "url": "http://pdu/status",
            "power_path": "data.0.power",
            "energy_path": "energy.total",
            "voltage_path": "line.voltage",
            "scale": 0.5,
            "energy_scale": 0.001,
        },
        client=client,
        now=lambda: NOW,
    )
    try:
        capabilities = await adapter.probe()
        reading = await adapter.read()
    finally:
        await adapter.close()

    assert capabilities.adapter_id == "http_pdu"
    assert capabilities.supports_energy is True
    assert capabilities.supports_voltage is True
    assert capabilities.supports_current is False
    assert reading.power.value == pytest.approx(125.0)
    assert reading.energy_total.value == pytest.approx(1.234)
    assert reading.voltage.value == pytest.approx(229.4)
    assert reading.current.value is None


async def test_http_pdu_missing_power_raises_and_missing_energy_is_unavailable() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"energy": {"total": 1.0}})

    client = client_for(handler)
    adapter = HttpPduAdapter(
        {"url": "http://pdu/status", "power_path": "power", "energy_path": "energy.total"},
        client=client,
        now=lambda: NOW,
    )
    try:
        with pytest.raises(MeterError):
            await adapter.probe()
    finally:
        await adapter.close()


async def test_http_pdu_missing_energy_is_unavailable_not_zero() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"power": 80.0})

    client = client_for(handler)
    adapter = HttpPduAdapter(
        {"url": "http://pdu/status", "energy_path": "energy.total"},
        client=client,
        now=lambda: NOW,
    )
    try:
        reading = await adapter.read()
    finally:
        await adapter.close()

    assert reading.power.value == pytest.approx(80.0)
    assert reading.energy_total.value is None
    assert reading.energy_total.quality is Quality.UNAVAILABLE


def test_http_pdu_requires_url() -> None:
    with pytest.raises(MeterError):
        HttpPduAdapter({})
