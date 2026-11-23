from __future__ import annotations

from pathlib import Path

from app.collectors.cpu import read_package_temperature_sources, read_package_temperatures


def make_hwmon(root: Path, index: int, name: str, labels: dict[str, int]) -> Path:
    hwmon = root / f"hwmon{index}"
    hwmon.mkdir(parents=True, exist_ok=True)
    (hwmon / "name").write_text(name)
    for sensor, (label, millidegrees) in enumerate(labels.items(), start=1):
        (hwmon / f"temp{sensor}_label").write_text(label)
        (hwmon / f"temp{sensor}_input").write_text(str(millidegrees))
    return hwmon


def test_coretemp_uses_explicit_package_id(tmp_path: Path) -> None:
    make_hwmon(tmp_path, 0, "coretemp", {"Package id 0": 51000})
    result = read_package_temperature_sources(tmp_path)
    assert result[0].value == 51.0
    assert result[0].source == "hwmon:coretemp"


def test_k10temp_maps_hwmon_instances_to_packages(tmp_path: Path) -> None:
    make_hwmon(tmp_path, 0, "k10temp", {"Tctl": 48000, "Tccd1": 45000})
    make_hwmon(tmp_path, 1, "k10temp", {"Tdie": 52000})
    result = read_package_temperature_sources(tmp_path, (2, 3))
    assert set(result) == {2, 3}
    assert result[2].value == 48.0
    assert result[2].source == "hwmon:k10temp"
    assert result[3].value == 52.0


def test_value_only_helper_still_works(tmp_path: Path) -> None:
    make_hwmon(tmp_path, 0, "k10temp", {"Tctl": 47000})
    assert read_package_temperatures(tmp_path) == {0: 47.0}
