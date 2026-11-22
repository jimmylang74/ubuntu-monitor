from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from app.config import GpuConfig
from app.domain import Quality
from app.power.adapters.nvidia import NvidiaAdapter, parse_nvidia_smi_csv

NOW = datetime(2026, 1, 1, tzinfo=UTC)


class FakeNvml:
    NVML_TEMPERATURE_GPU = 0
    NVML_CLOCK_SM = 1
    NVML_CLOCK_MEM = 2

    def __init__(self, *, fail_init: bool = False, fail_gpu_index: int | None = None) -> None:
        self.fail_init = fail_init
        self.fail_gpu_index = fail_gpu_index
        self.init_calls = 0
        self.shutdown_calls = 0

    def nvmlInit(self) -> None:
        self.init_calls += 1
        if self.fail_init:
            raise RuntimeError("Driver/library version mismatch")

    def nvmlShutdown(self) -> None:
        self.shutdown_calls += 1

    def nvmlDeviceGetCount(self) -> int:
        return 2

    def nvmlDeviceGetHandleByIndex(self, index: int) -> int:
        return index

    def nvmlDeviceGetUUID(self, handle: int) -> bytes:
        return f"GPU-{handle}".encode()

    def nvmlDeviceGetName(self, handle: int) -> str:
        return "Tesla V100 SXM2 16GB"

    def nvmlDeviceGetPciInfo(self, handle: int) -> SimpleNamespace:
        return SimpleNamespace(busId=f"0000:0{handle}:00.0")

    def nvmlDeviceGetPowerUsage(self, handle: int) -> int:
        if handle == self.fail_gpu_index:
            raise RuntimeError("GPU lost")
        return 100_000 + handle * 1_000

    def nvmlDeviceGetPowerManagementLimit(self, handle: int) -> int:
        return 250_000

    def nvmlDeviceGetEnforcedPowerLimit(self, handle: int) -> int:
        return 250_000

    def nvmlDeviceGetTemperature(self, handle: int, sensor: int) -> int:
        return 55

    def nvmlDeviceGetUtilizationRates(self, handle: int) -> SimpleNamespace:
        return SimpleNamespace(gpu=65)

    def nvmlDeviceGetMemoryInfo(self, handle: int) -> SimpleNamespace:
        return SimpleNamespace(used=1024**3, total=16 * 1024**3)

    def nvmlDeviceGetFanSpeed(self, handle: int) -> int:
        raise RuntimeError("not supported")

    def nvmlDeviceGetClockInfo(self, handle: int, clock_type: int) -> int:
        return 1530


def test_two_gpus_are_keyed_by_uuid(fake_clock, now) -> None:
    adapter = NvidiaAdapter(GpuConfig(), module=FakeNvml(), clock=fake_clock, now=lambda: now)
    gpus = adapter.sample()
    assert [g.uuid for g in gpus] == ["GPU-0", "GPU-1"]
    assert gpus[0].power.value == pytest.approx(100.0)
    assert gpus[1].power.value == pytest.approx(101.0)
    assert gpus[0].power_limit.value == pytest.approx(250.0)
    assert gpus[0].fan_speed.quality is Quality.UNAVAILABLE


def test_one_gpu_failure_does_not_affect_the_other(fake_clock, now) -> None:
    adapter = NvidiaAdapter(
        GpuConfig(), module=FakeNvml(fail_gpu_index=1), clock=fake_clock, now=lambda: now
    )
    gpus = {g.uuid: g for g in adapter.sample()}
    assert gpus["GPU-0"].power.value == pytest.approx(100.0)
    assert gpus["GPU-1"].power.value is None
    assert "GPU lost" in (gpus["GPU-1"].power.error or "")


def test_init_failure_backs_off(fake_clock, now) -> None:
    module = FakeNvml(fail_init=True)
    cfg = GpuConfig(reinit_interval=10.0)
    adapter = NvidiaAdapter(cfg, module=module, clock=fake_clock, now=lambda: now)

    assert adapter.ensure_initialized() is False
    assert "mismatch" in (adapter.init_error or "")
    assert adapter.sample() == ()
    assert module.init_calls == 1

    fake_clock.advance(5.0)
    assert adapter.ensure_initialized() is False
    assert module.init_calls == 1

    fake_clock.advance(6.0)
    assert adapter.ensure_initialized() is False
    assert module.init_calls == 2


def test_nvidia_smi_csv_parser_handles_na() -> None:
    text = (
        "GPU-abc, Tesla V100, 120.5, 250.0, 55, 65, 1024, 16384, [N/A], 1530, 877, 00000000:02:00.0\n"
        "broken,row\n"
    )
    gpus = parse_nvidia_smi_csv(text, NOW)
    assert len(gpus) == 1
    gpu = gpus[0]
    assert gpu.uuid == "GPU-abc"
    assert gpu.power.value == pytest.approx(120.5)
    assert gpu.memory_used.value == 1024 * 1024 * 1024
    assert gpu.memory_total.value == 16 * 1024 * 1024 * 1024
    assert gpu.fan_speed.quality is Quality.UNAVAILABLE
    assert gpu.core_clock.value == pytest.approx(1530.0)
