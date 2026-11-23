"""YAML configuration with strict validation."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG_PATH = PROJECT_ROOT / "config" / "config.yaml"


class _Base(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ServerConfig(_Base):
    host: str = "0.0.0.0"
    port: int = Field(default=8010, ge=1, le=65535)
    log_level: str = "info"
    # Empty means stderr (systemd journal).  Set a path for rotating file logs.
    log_file: str = ""
    log_max_bytes: int = Field(default=10 * 1024 * 1024, ge=4096)
    log_backup_count: int = Field(default=5, ge=1)
    timezone: str = "local"

    @property
    def access_log(self) -> bool:
        return self.log_level.lower() == "debug"


class CollectorConfig(_Base):
    cpu_interval: float = Field(default=1.0, gt=0)
    gpu_interval: float = Field(default=1.0, gt=0)
    memory_interval: float = Field(default=2.0, gt=0)
    disk_interval: float = Field(default=5.0, gt=0)
    network_interval: float = Field(default=1.0, gt=0)
    system_interval: float = Field(default=1.0, gt=0)
    timeout: float = Field(default=2.0, gt=0)


class AmdgpuConfig(_Base):
    # auto: exclude cards that look like APUs (their hwmon power already
    # includes the CPU package); include/exclude: force the decision.
    apu_policy: Literal["auto", "include", "exclude"] = "auto"
    # In auto mode, dedicated VRAM at or below this value suggests an APU.
    apu_vram_limit_gb: float = Field(default=2.0, ge=0)


class GpuConfig(_Base):
    fallback_nvidia_smi: bool = True
    reinit_interval: float = Field(default=10.0, gt=0)
    expose_fan: bool = True
    # Backends are tried independently so mixed NVIDIA + AMD hosts are supported.
    backends: list[Literal["nvidia", "amdgpu"]] = Field(
        default_factory=lambda: ["nvidia", "amdgpu"]
    )
    # False: at least one healthy backend is enough.  True: any backend failure
    # makes gpu_available=False (useful for alerting on a broken driver).
    require_all_backends: bool = False
    amdgpu: AmdgpuConfig = Field(default_factory=AmdgpuConfig)


class RaplConfig(_Base):
    helper: str = "/usr/local/lib/power-monitor/rapl-read"
    use_sudo: Literal["auto", "always", "never"] = "auto"
    # Mainline Linux registers AMD Zen RAPL under the "intel-rapl" powercap
    # control type too.  Add other powercap prefixes here if your kernel does.
    control_types: list[str] = Field(default_factory=lambda: ["intel-rapl"])
    max_plausible_w: float = Field(default=1500.0, gt=0)
    min_interval_s: float = Field(default=0.2, gt=0)


class DatabaseConfig(_Base):
    type: Literal["sqlite"] = "sqlite"
    path: str = "data/power_monitor.db"
    write_interval: float = Field(default=1.0, gt=0)
    busy_timeout_ms: int = Field(default=5000, ge=0)
    wal: bool = True
    # Bounded write queue: when full, the oldest pending sample is dropped so
    # collection and WebSocket delivery can never be blocked by disk I/O.
    queue_size: int = Field(default=300, ge=10)


class ShellyConfig(_Base):
    url: str = ""
    generation: Literal["auto", "gen1", "gen2"] = "auto"


class ModbusConfig(_Base):
    host: str = ""
    port: int = Field(default=502, ge=1, le=65535)
    unit_id: int = 1
    function_code: int = 3
    register_address: int = Field(default=0, alias="register")
    register_type: Literal["uint32", "float32"] = "float32"
    word_order: Literal["big", "little"] = "big"
    scale: float = 1.0
    min_interval_s: float = Field(default=1.0, gt=0)


class ExternalMeterConfig(_Base):
    enabled: bool = False
    type: Literal["shelly", "modbus", "http_pdu"] = "shelly"
    timeout: float = Field(default=2.0, gt=0)
    interval: float = Field(default=1.0, gt=0)
    failover_after: int = Field(default=3, ge=1)
    shelly: ShellyConfig = Field(default_factory=ShellyConfig)
    modbus: ModbusConfig | None = None
    http_pdu: dict[str, Any] = Field(default_factory=dict)


class PowerConfig(_Base):
    mode: Literal["auto", "estimated", "external"] = "auto"
    # None means "derive a suggestion from detected hardware at runtime".
    # An explicit number always wins and is reported as source="config".
    estimated_other_power_w: float | None = Field(default=None, ge=0)
    strict_estimate: bool = True
    external_meter: ExternalMeterConfig = Field(default_factory=ExternalMeterConfig)


class NetworkConfig(_Base):
    exclude_interfaces: list[str] = Field(
        default_factory=lambda: [
            "lo",
            "docker*",
            "br-*",
            "veth*",
            "virbr*",
            "tun*",
            "tap*",
        ]
    )
    include_interfaces: list[str] = Field(default_factory=list)


class ElectricityConfig(_Base):
    enabled: bool = False
    price_per_kwh: float = Field(default=0.8, ge=0)
    currency: str = "CNY"


class HistoryConfig(_Base):
    raw_retention_days: int = Field(default=7, ge=1)
    minute_retention_days: int = Field(default=90, ge=1)
    hourly_retention_days: int = Field(default=365, ge=1)
    cleanup_hour: int = Field(default=3, ge=0, le=23)
    max_points: int = Field(default=2000, ge=100)


class AppConfig(_Base):
    server: ServerConfig = Field(default_factory=ServerConfig)
    collector: CollectorConfig = Field(default_factory=CollectorConfig)
    gpu: GpuConfig = Field(default_factory=GpuConfig)
    rapl: RaplConfig = Field(default_factory=RaplConfig)
    database: DatabaseConfig = Field(default_factory=DatabaseConfig)
    power: PowerConfig = Field(default_factory=PowerConfig)
    network: NetworkConfig = Field(default_factory=NetworkConfig)
    electricity: ElectricityConfig = Field(default_factory=ElectricityConfig)
    history: HistoryConfig = Field(default_factory=HistoryConfig)

    source_path: Path | None = Field(default=None, exclude=True)

    @property
    def database_path(self) -> Path:
        path = Path(self.database.path)
        return path if path.is_absolute() else PROJECT_ROOT / path


def load_config(path: Path | str | None = None) -> AppConfig:
    """Load YAML config. Missing file means all defaults."""
    config_path = Path(path) if path is not None else DEFAULT_CONFIG_PATH
    if not config_path.exists():
        return AppConfig(source_path=None)

    try:
        raw = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise ValueError(f"invalid YAML in {config_path}: {exc}") from exc

    if not isinstance(raw, dict):
        raise ValueError(f"{config_path} must contain a YAML mapping at the top level")

    try:
        cfg = AppConfig.model_validate(raw)
    except Exception as exc:  # pydantic error already descriptive
        raise ValueError(f"invalid configuration in {config_path}:\n{exc}") from exc
    return cfg.model_copy(update={"source_path": config_path})
