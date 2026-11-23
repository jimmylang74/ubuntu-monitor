from __future__ import annotations

from pathlib import Path

import pytest

from app.config import AppConfig, load_config


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
