from __future__ import annotations

from pathlib import Path

import pytest

from app.config import PROJECT_ROOT, AppConfig, load_config


def test_defaults_use_port_8010() -> None:
    cfg = AppConfig()
    assert cfg.server.port == 8010
    assert cfg.server.host == "0.0.0.0"
    # None enables hardware-derived suggestions; an explicit config value wins.
    assert cfg.power.estimated_other_power_w is None
    assert cfg.gpu.backends == ["nvidia", "amdgpu"]
    assert cfg.rapl.control_types == ["intel-rapl"]


def test_missing_file_returns_defaults(tmp_path: Path) -> None:
    cfg = load_config(tmp_path / "absent.yaml")
    assert cfg.server.port == 8010
    assert cfg.source_path is None


def test_example_config_parses() -> None:
    cfg = load_config(Path("config/config.example.yaml"))
    assert cfg.server.port == 8010
    assert cfg.power.estimated_other_power_w is None
    assert cfg.rapl.max_plausible_w == 1500.0
    assert cfg.gpu.amdgpu.apu_policy == "auto"


def test_unknown_field_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "bad.yaml"
    path.write_text("server:\n  port: 8010\n  typo_field: true\n")
    with pytest.raises(ValueError, match="typo_field"):
        load_config(path)


def test_database_path_defaults_to_project_root(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("PM_DATA_DIR", raising=False)
    assert AppConfig().database_path == PROJECT_ROOT / "data" / "power_monitor.db"


def test_database_path_honors_pm_data_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PM_DATA_DIR", str(tmp_path))
    assert AppConfig().database_path == tmp_path / "data" / "power_monitor.db"


def test_package_config_parses(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("PM_DATA_DIR", raising=False)
    cfg = load_config(Path("debian/config.yaml"))
    assert cfg.rapl.helper == "/usr/lib/power-monitor/rapl-read"
    assert cfg.database_path == PROJECT_ROOT / "data" / "power_monitor.db"


def test_cli_honors_pm_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from app.cli import main

    path = tmp_path / "custom.yaml"
    path.write_text("server:\n  port: 8123\n", encoding="utf-8")
    monkeypatch.setenv("PM_CONFIG", str(path))
    assert main(["check"]) == 0
    output = capsys.readouterr().out
    assert str(path) in output
    assert "8123" in output
