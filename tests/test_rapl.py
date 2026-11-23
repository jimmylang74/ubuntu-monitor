from __future__ import annotations

import os
from datetime import datetime
from pathlib import Path

import pytest

from app.config import RaplConfig
from app.power.adapters.rapl import RaplAdapter
from tests.conftest import FakeClock

MAX_UJ = 262_143_328_850


def make_domain(
    root: Path,
    dirname: str,
    name: str,
    energy: int,
    *,
    max_uj: int = MAX_UJ,
    enabled: int = 1,
) -> Path:
    domain = root / dirname
    domain.mkdir(parents=True, exist_ok=True)
    (domain / "name").write_text(name)
    (domain / "energy_uj").write_text(str(energy))
    (domain / "max_energy_range_uj").write_text(str(max_uj))
    (domain / "enabled").write_text(str(enabled))
    return domain


def set_energy(domain: Path, value: int) -> None:
    (domain / "energy_uj").write_text(str(value))


def make_adapter(
    root: Path,
    clock: FakeClock,
    now: datetime,
    *,
    use_sudo: str = "never",
    max_plausible_w: float = 1500.0,
    **kwargs,
) -> RaplAdapter:
    cfg = RaplConfig(use_sudo=use_sudo, min_interval_s=0.2, max_plausible_w=max_plausible_w)
    return RaplAdapter(cfg, root=root, clock=clock, now=lambda: now, **kwargs)


def test_only_configured_control_types_are_discovered(
    tmp_path: Path, fake_clock: FakeClock, now: datetime
) -> None:
    make_domain(tmp_path, "intel-rapl:0", "package-0", 1_000_000)
    make_domain(tmp_path, "amd-rapl:0", "package-0", 2_000_000)
    cfg = RaplConfig(control_types=["amd-rapl"], use_sudo="never")
    adapter = RaplAdapter(cfg, root=tmp_path, clock=fake_clock, now=lambda: now)

    adapter.refresh()

    assert [domain.domain_id for domain in adapter.all_domains] == ["amd-rapl:0"]


def test_first_sample_is_warmup_then_power_is_derived(tmp_path: Path, fake_clock: FakeClock, now: datetime) -> None:
    domain = make_domain(tmp_path, "intel-rapl:0", "package-0", 1_000_000)
    adapter = make_adapter(tmp_path, fake_clock, now)

    first = adapter.sample()
    assert first[0].power_w is None
    assert "warm-up" in (first[0].error or "")

    set_energy(domain, 2_000_000)
    fake_clock.advance(1.0)
    second = adapter.sample()
    assert second[0].power_w == pytest.approx(1.0)
    assert second[0].error is None
    assert second[0].energy_j == pytest.approx(1.0)


def test_wrap_around_uses_max_energy_range(tmp_path: Path, fake_clock: FakeClock, now: datetime) -> None:
    domain = make_domain(tmp_path, "intel-rapl:0", "package-0", MAX_UJ - 250_000)
    adapter = make_adapter(tmp_path, fake_clock, now)
    adapter.sample()

    set_energy(domain, 250_000)
    fake_clock.advance(1.0)
    sample = adapter.sample()[0]
    assert sample.power_w == pytest.approx(0.5)


def test_multiple_packages_are_independent(tmp_path: Path, fake_clock: FakeClock, now: datetime) -> None:
    first = make_domain(tmp_path, "intel-rapl:0", "package-0", 1_000_000)
    second = make_domain(tmp_path, "intel-rapl:1", "package-1", 2_000_000)
    adapter = make_adapter(tmp_path, fake_clock, now)
    adapter.sample()

    set_energy(first, 3_000_000)
    set_energy(second, 6_000_000)
    fake_clock.advance(1.0)
    samples = {s.name: s for s in adapter.sample()}
    assert samples["package-0"].power_w == pytest.approx(2.0)
    assert samples["package-1"].power_w == pytest.approx(4.0)
    assert len(adapter.package_domains) == 2


def test_child_domain_is_not_a_package(tmp_path: Path, fake_clock: FakeClock, now: datetime) -> None:
    make_domain(tmp_path, "intel-rapl:0", "package-0", 1_000_000)
    make_domain(tmp_path, "intel-rapl:0:0", "dram", 500_000)
    adapter = make_adapter(tmp_path, fake_clock, now)
    adapter.refresh()

    assert [d.name for d in adapter.package_domains] == ["package-0"]
    assert {d.name for d in adapter.all_domains} == {"package-0", "dram"}


def test_implausible_delta_resets_baseline(tmp_path: Path, fake_clock: FakeClock, now: datetime) -> None:
    domain = make_domain(tmp_path, "intel-rapl:0", "package-0", 0)
    adapter = make_adapter(tmp_path, fake_clock, now, max_plausible_w=2.0)
    adapter.sample()

    set_energy(domain, 5_000_000)
    fake_clock.advance(1.0)
    reset = adapter.sample()[0]
    assert reset.power_w is None
    assert "implausible" in (reset.error or "")

    set_energy(domain, 6_000_000)
    fake_clock.advance(1.0)
    recovered = adapter.sample()[0]
    assert recovered.power_w == pytest.approx(1.0)


@pytest.mark.skipif(os.geteuid() == 0, reason="root can read mode-000 sysfs fixtures")
def test_permission_denied_reports_unavailable(tmp_path: Path, fake_clock: FakeClock, now: datetime) -> None:
    domain = make_domain(tmp_path, "intel-rapl:0", "package-0", 1_000_000)
    (domain / "energy_uj").chmod(0o000)
    adapter = make_adapter(tmp_path, fake_clock, now)

    sample = adapter.sample()[0]
    assert sample.power_w is None
    assert "permission" in (sample.error or "").lower()


def test_helper_output_parsing(tmp_path: Path, fake_clock: FakeClock, now: datetime) -> None:
    make_domain(tmp_path, "intel-rapl:0", "package-0", 1)
    energy = 1_000_000

    def runner() -> str:
        return f"intel-rapl:0\tpackage-0\t{energy}\t{MAX_UJ}\t1\n"

    adapter = make_adapter(tmp_path, fake_clock, now, use_sudo="always", runner=runner)
    first = adapter.sample()[0]
    assert first.power_w is None
    assert first.source.startswith("rapl-sudo:")

    energy = 3_000_000
    fake_clock.advance(1.0)
    second = adapter.sample()[0]
    assert second.power_w == pytest.approx(2.0)


def test_no_rapl_domains_returns_empty(tmp_path: Path, fake_clock: FakeClock, now: datetime) -> None:
    adapter = make_adapter(tmp_path, fake_clock, now)
    assert adapter.sample() == ()
