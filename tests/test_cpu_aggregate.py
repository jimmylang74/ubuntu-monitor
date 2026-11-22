from __future__ import annotations

from datetime import UTC, datetime

import pytest

from app.collectors.cpu import aggregate_package_power
from app.domain import CpuPackage, Quality, measured, unavailable

NOW = datetime(2026, 1, 1, tzinfo=UTC)


def package(name: str, power_w: float | None) -> CpuPackage:
    power = (
        unavailable("W", "rapl", NOW, error="missing") if power_w is None else measured(power_w, "W", "rapl", NOW)
    )
    spare = unavailable("", "none", NOW)
    return CpuPackage(
        package_id=0,
        name=name,
        power=power,
        energy_j=spare,
        temperature=spare,
        frequency_mhz=spare,
        usage_percent=spare,
        core_count=1,
        thread_count=2,
    )


def test_sum_of_all_packages_is_measured() -> None:
    total = aggregate_package_power((package("package-0", 100.0), package("package-1", 50.0)), NOW)
    assert total.quality is Quality.MEASURED
    assert total.value == pytest.approx(150.0)


def test_missing_package_makes_total_unavailable() -> None:
    total = aggregate_package_power((package("package-0", 100.0), package("package-1", None)), NOW)
    assert total.quality is Quality.UNAVAILABLE
    assert total.value is None
    assert total.missing == ("package-1",)


def test_no_packages_is_unavailable() -> None:
    total = aggregate_package_power((), NOW)
    assert total.quality is Quality.UNAVAILABLE
