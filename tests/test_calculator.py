from __future__ import annotations

from datetime import UTC, datetime

import pytest

from app.config import PowerConfig
from app.domain import (
    CpuSnapshot,
    GpuSnapshot,
    Quality,
    measured,
    unavailable,
)
from app.power.calculator import MeterState, compute_system_power

NOW = datetime(2026, 1, 1, tzinfo=UTC)


def cpu(power_w: float | None) -> CpuSnapshot:
    power = (
        unavailable("W", "rapl", NOW, error="no RAPL")
        if power_w is None
        else measured(power_w, "W", "rapl:packages", NOW)
    )
    return CpuSnapshot(
        timestamp=NOW,
        model="test",
        socket_count=1,
        packages=(),
        total_power=power,
        usage_percent=measured(10.0, "%", "psutil", NOW),
        max_temperature=measured(40.0, "°C", "hwmon", NOW),
    )


def gpu(uuid: str, power_w: float | None) -> GpuSnapshot:
    empty = unavailable("W", f"nvml:{uuid}", NOW, error="unavailable")
    power = empty if power_w is None else measured(power_w, "W", f"nvml:{uuid}", NOW)
    other = unavailable("", "nvml", NOW)
    return GpuSnapshot(
        uuid=uuid,
        index=0,
        name="Tesla V100",
        pci_bus_id=None,
        power=power,
        power_limit=other,
        temperature=other,
        utilization=other,
        memory_used=other,
        memory_total=other,
        fan_speed=other,
        core_clock=other,
        memory_clock=other,
    )


def test_healthy_meter_is_measured_wall_power() -> None:
    cfg = PowerConfig()
    result = compute_system_power(
        cpu(100.0),
        (gpu("GPU-1", 200.0),),
        measured(512.5, "W", "shelly:http://meter", NOW),
        MeterState(enabled=True, healthy=True, last_success=NOW),
        cfg,
        NOW,
    )
    assert result.mode == "measured"
    assert result.power.quality is Quality.MEASURED
    assert result.power.value == pytest.approx(512.5)
    assert result.power.source == "shelly:http://meter"
    assert result.scope == "wall"
    assert result.meter_status == "ok"


def test_offline_meter_falls_back_to_estimate() -> None:
    cfg = PowerConfig(estimated_other_power_w=150.0)
    result = compute_system_power(
        cpu(100.0),
        (gpu("GPU-1", 200.0),),
        None,
        MeterState(enabled=True, healthy=False, last_success=NOW),
        cfg,
        NOW,
    )
    assert result.mode == "estimated"
    assert result.power.quality is Quality.ESTIMATED
    assert result.power.value == pytest.approx(450.0)
    assert result.scope == "dc_components"
    assert result.meter_status == "offline"


def test_missing_cpu_makes_estimate_unavailable_by_default() -> None:
    cfg = PowerConfig(strict_estimate=True)
    result = compute_system_power(cpu(None), (), None, MeterState(), cfg, NOW)
    assert result.mode == "unavailable"
    assert result.power.value is None
    assert result.missing_components == ("cpu",)


def test_single_gpu_failure_marks_total_unavailable() -> None:
    cfg = PowerConfig()
    result = compute_system_power(
        cpu(100.0),
        (gpu("GPU-1", 200.0), gpu("GPU-2", None)),
        None,
        MeterState(),
        cfg,
        NOW,
    )
    assert result.mode == "unavailable"
    assert "gpu:GPU-2" in result.missing_components


def test_no_gpu_is_zero_not_missing() -> None:
    cfg = PowerConfig(estimated_other_power_w=150.0)
    result = compute_system_power(cpu(100.0), (), None, MeterState(), cfg, NOW)
    assert result.mode == "estimated"
    assert result.power.value == pytest.approx(250.0)
    assert result.missing_components == ()


def test_gpu_enumeration_failure_is_missing_not_zero() -> None:
    cfg = PowerConfig()
    result = compute_system_power(cpu(100.0), (), None, MeterState(), cfg, NOW, gpu_available=False)
    assert result.mode == "unavailable"
    assert "gpu" in result.missing_components


def test_non_strict_estimate_sums_available_components() -> None:
    cfg = PowerConfig(strict_estimate=False, estimated_other_power_w=100.0)
    result = compute_system_power(cpu(None), (gpu("GPU-1", 50.0),), None, MeterState(), cfg, NOW)
    assert result.mode == "estimated"
    assert result.power.value == pytest.approx(150.0)
    assert result.missing_components == ("cpu",)


def test_estimated_mode_ignores_meter() -> None:
    cfg = PowerConfig(mode="estimated")
    result = compute_system_power(
        cpu(100.0),
        (),
        measured(900.0, "W", "meter", NOW),
        MeterState(enabled=True, healthy=True),
        cfg,
        NOW,
    )
    assert result.mode == "estimated"
    assert result.power.value == pytest.approx(250.0)


def test_estimate_is_never_marked_measured() -> None:
    result = compute_system_power(cpu(100.0), (), None, MeterState(), PowerConfig(), NOW)
    assert result.power.quality is Quality.ESTIMATED
