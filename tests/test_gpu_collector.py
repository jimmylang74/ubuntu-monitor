from __future__ import annotations

from app.collectors.gpu import GpuCollector
from app.config import AppConfig, GpuConfig


class FakeNvml:
    def __init__(self, *, initialized: bool, init_error: str | None = None, gpus=()) -> None:
        self.initialized = initialized
        self.init_error = init_error
        self._gpus = gpus
        self.sample_calls = 0

    def sample(self):
        self.sample_calls += 1
        return self._gpus if self.initialized else ()

    def close(self) -> None:
        pass


class FakeSmi:
    def __init__(self, *, gpus=(), error: str | None = None) -> None:
        self.error = error
        self._gpus = gpus
        self.sample_calls = 0

    def sample(self):
        self.sample_calls += 1
        return self._gpus


def collector(nvml, smi, cfg: AppConfig | None = None) -> GpuCollector:
    cfg = cfg or AppConfig(gpu=GpuConfig(backends=["nvidia"]))
    return GpuCollector(cfg, nvml=nvml, smi=smi)


def test_zero_gpus_from_nvml_is_authoritative() -> None:
    nvml = FakeNvml(initialized=True)
    smi = FakeSmi()
    gpu = collector(nvml, smi)
    assert gpu.collect() == ()
    assert gpu.available is True
    assert gpu.error is None
    assert smi.sample_calls == 0


def test_failed_nvml_and_failed_smi_is_unavailable() -> None:
    nvml = FakeNvml(initialized=False, init_error="driver/library mismatch")
    smi = FakeSmi(error="nvidia-smi: command failed")
    gpu = collector(nvml, smi)
    assert gpu.collect() == ()
    assert gpu.available is False
    assert "mismatch" in (gpu.error or "")


def test_successful_smi_with_zero_rows_means_zero_gpus() -> None:
    nvml = FakeNvml(initialized=False, init_error="driver/library mismatch")
    smi = FakeSmi()
    gpu = collector(nvml, smi)
    assert gpu.collect() == ()
    assert gpu.available is True
    assert gpu.error is None


def test_fallback_can_be_disabled() -> None:
    cfg = AppConfig(gpu=GpuConfig(fallback_nvidia_smi=False, backends=["nvidia"]))
    nvml = FakeNvml(initialized=False, init_error="boom")
    smi = FakeSmi()
    gpu = collector(nvml, smi, cfg)
    assert gpu.collect() == ()
    assert gpu.available is False
    assert gpu.error == "nvidia: boom"
    assert smi.sample_calls == 0
