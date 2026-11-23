from __future__ import annotations

from pathlib import Path

from app.collectors.gpu import GpuCollector
from app.config import AppConfig, GpuConfig
from app.domain import Quality
from app.power.adapters.amdgpu import AmdgpuAdapter
from tests.test_gpu_collector import FakeNvml, FakeSmi


def make_card(
    root: Path,
    index: int,
    *,
    name: str = "Radeon RX 7800 XT",
    vendor: str = "0x1002",
    vram_bytes: int | None = 16 * 1024**3,
    power_uw: int = 45_000_000,
    use_input: bool = False,
) -> Path:
    card = root / f"card{index}"
    device = card / "device"
    hwmon = device / "hwmon" / "hwmon0"
    hwmon.mkdir(parents=True, exist_ok=True)

    driver = "amdgpu" if vendor == "0x1002" else "nvidia"
    (device / "vendor").write_text(vendor)
    (device / "product_name").write_text(name)
    (device / "uevent").write_text(f"DRIVER={driver}\nPCI_SLOT_NAME=0000:03:00.0\n")
    if vram_bytes is not None:
        (device / "mem_info_vram_total").write_text(str(vram_bytes))
        (device / "mem_info_vram_used").write_text(str(vram_bytes // 4))
    (device / "gpu_busy_percent").write_text("37")
    (device / "pp_dpm_sclk").write_text("0: 500Mhz\n1: 2500Mhz *\n")
    (device / "pp_dpm_mclk").write_text("0: 200Mhz\n1: 1200Mhz *\n")

    (hwmon / "name").write_text("amdgpu" if vendor == "0x1002" else "nvidia")
    power_file = "power1_input" if use_input else "power1_average"
    (hwmon / power_file).write_text(str(power_uw))
    (hwmon / "power1_cap").write_text("300000000")
    (hwmon / "temp1_input").write_text("62000")
    (hwmon / "fan1_input").write_text("1200")
    (hwmon / "fan1_max").write_text("3000")
    return card


def test_amdgpu_metrics_are_converted(tmp_path: Path) -> None:
    make_card(tmp_path, 0)
    adapter = AmdgpuAdapter(GpuConfig(), root=tmp_path)

    gpus = adapter.sample()

    assert len(gpus) == 1
    gpu = gpus[0]
    assert gpu.uuid == "amdgpu:0000:03:00.0"
    assert gpu.pci_bus_id == "0000:03:00.0"
    assert gpu.power.value == 45.0
    assert gpu.power.unit == "W"
    assert "power1_average" in gpu.power.source
    assert gpu.power_limit.value == 300.0
    assert gpu.temperature.value == 62.0
    assert gpu.temperature.unit == "°C"
    assert gpu.utilization.value == 37.0
    assert gpu.memory_total.value == 16 * 1024**3
    assert gpu.core_clock.value == 2500.0
    assert gpu.memory_clock.value == 1200.0
    assert gpu.fan_speed.value == 40.0
    assert gpu.fan_speed_rpm.value == 1200.0
    assert gpu.include_in_system_total is True


def test_power1_input_fallback(tmp_path: Path) -> None:
    make_card(tmp_path, 1, use_input=True)
    gpu = AmdgpuAdapter(GpuConfig(), root=tmp_path).sample()[0]
    assert gpu.power.value == 45.0
    assert "power1_input" in gpu.power.source


def test_apu_is_excluded_but_still_reported(tmp_path: Path) -> None:
    make_card(tmp_path, 0, name="AMD Radeon Graphics", vram_bytes=None)
    gpu = AmdgpuAdapter(GpuConfig(), root=tmp_path).sample()[0]
    assert gpu.include_in_system_total is False
    assert gpu.power.quality is Quality.MEASURED


def test_apu_override_can_include(tmp_path: Path) -> None:
    make_card(tmp_path, 0, name="AMD Radeon Graphics", vram_bytes=None)
    cfg = GpuConfig(amdgpu={"apu_policy": "include"})
    assert AmdgpuAdapter(cfg, root=tmp_path).sample()[0].include_in_system_total is True


def test_low_vram_apu_without_graphics_name_is_still_excluded(tmp_path: Path) -> None:
    make_card(tmp_path, 0, name="AMD Radeon 780M", vram_bytes=2 * 1024**3)
    assert AmdgpuAdapter(GpuConfig(), root=tmp_path).sample()[0].include_in_system_total is False


def test_low_vram_discrete_card_is_not_excluded(tmp_path: Path) -> None:
    make_card(tmp_path, 0, name="Radeon RX 640", vram_bytes=2 * 1024**3)
    assert AmdgpuAdapter(GpuConfig(), root=tmp_path).sample()[0].include_in_system_total is True


def test_non_amd_card_is_skipped(tmp_path: Path) -> None:
    make_card(tmp_path, 0, vendor="0x10de", name="GeForce RTX")
    assert AmdgpuAdapter(GpuConfig(), root=tmp_path).sample() == ()


def test_missing_hwmon_metrics_are_unavailable(tmp_path: Path) -> None:
    card = make_card(tmp_path, 0)
    for path in card.rglob("*"):
        if path.is_file() and path.name in {"power1_average", "temp1_input"}:
            path.unlink()
    gpu = AmdgpuAdapter(GpuConfig(), root=tmp_path).sample()[0]
    assert gpu.power.quality is Quality.UNAVAILABLE
    assert gpu.temperature.quality is Quality.UNAVAILABLE


def test_mixed_vendor_collector_merges_backends(tmp_path: Path) -> None:
    make_card(tmp_path, 0, name="Radeon RX 7800 XT")
    cfg = AppConfig()

    class NvidiaWithOneGpu(FakeNvml):
        def sample(self):
            from tests.fakes import gpu_snapshot

            return (gpu_snapshot(uuid="GPU-NVIDIA", power_w=100.0),)

    collector = GpuCollector(
        cfg,
        nvml=NvidiaWithOneGpu(initialized=True),
        smi=FakeSmi(),
        amd=AmdgpuAdapter(cfg.gpu, root=tmp_path),
    )
    gpus = collector.collect()

    assert collector.available is True
    assert [gpu.index for gpu in gpus] == [0, 1]
    assert {gpu.uuid for gpu in gpus} == {"GPU-NVIDIA", "amdgpu:0000:03:00.0"}
