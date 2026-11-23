from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from app.config import (
    AppConfig,
    DatabaseConfig,
    ElectricityConfig,
    HistoryConfig,
    ServerConfig,
)
from app.database.database import AppDatabase
from app.database.repository import utc_ms
from app.services.energy_service import EnergyService

NOW = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)
NOW_MS = utc_ms(NOW)


@pytest.fixture
def database(tmp_path: Path) -> AppDatabase:
    app_db = AppDatabase(DatabaseConfig(path=str(tmp_path / "energy.db")), tmp_path / "energy.db")
    app_db.ensure_open()
    yield app_db
    app_db.close()


@pytest.fixture
def cfg() -> AppConfig:
    return AppConfig(
        server=ServerConfig(timezone="UTC"),
        electricity=ElectricityConfig(enabled=True, price_per_kwh=0.5, currency="CNY"),
        history=HistoryConfig(raw_retention_days=7),
    )


def sample(ts_ms: int, energy_wh: float | None, quality: str = "measured") -> dict:
    return {
        "ts_ms": ts_ms,
        "system_w": 200.0 if energy_wh is not None else None,
        "system_quality": "measured" if energy_wh is not None else "unavailable",
        "energy_wh": energy_wh,
        "energy_quality": quality,
    }


def test_daily_energy_sums_raw_samples(database: AppDatabase, cfg: AppConfig) -> None:
    repository = database.ensure_open()
    repository.upsert_sample(sample(NOW_MS - 2 * 3_600_000, 2.0), [])
    repository.upsert_sample(sample(NOW_MS - 3_600_000, 3.0), [])

    result = EnergyService(cfg, database, now=lambda: NOW).daily(days=1)

    assert result["currency"] == "CNY"
    assert result["timezone"] == "UTC"
    day = result["days"][0]
    assert day["date"] == "2026-01-01"
    assert day["energy_wh"] == pytest.approx(5.0)
    assert day["energy_kwh"] == pytest.approx(0.005)
    assert day["quality"] == "measured"
    assert day["cost"] == pytest.approx(0.0025)
    assert day["samples"] == 2


def test_daily_energy_marks_missing_samples_estimated(database: AppDatabase, cfg: AppConfig) -> None:
    repository = database.ensure_open()
    repository.upsert_sample(sample(NOW_MS - 1000, 2.0), [])
    repository.upsert_sample(sample(NOW_MS - 2000, None), [])

    day = EnergyService(cfg, database, now=lambda: NOW).daily(days=1)["days"][0]

    assert day["energy_wh"] == pytest.approx(2.0)
    assert day["quality"] == "estimated"
    assert day["missing_samples"] == 1


def test_daily_energy_is_unavailable_without_data(database: AppDatabase, cfg: AppConfig) -> None:
    day = EnergyService(cfg, database, now=lambda: NOW).daily(days=1)["days"][0]
    assert day["energy_wh"] is None
    assert day["energy_kwh"] is None
    assert day["quality"] == "unavailable"
    assert day["cost"] is None


def test_daily_returns_requested_number_of_days(database: AppDatabase, cfg: AppConfig) -> None:
    result = EnergyService(cfg, database, now=lambda: NOW).daily(days=3)
    assert [day["date"] for day in result["days"]] == [
        "2025-12-30",
        "2025-12-31",
        "2026-01-01",
    ]
    with pytest.raises(ValueError):
        EnergyService(cfg, database, now=lambda: NOW).daily(days=0)
    with pytest.raises(ValueError):
        EnergyService(cfg, database, now=lambda: NOW).daily(days=367)


def test_summary_partitions_today_yesterday_and_rolling_windows(
    database: AppDatabase, cfg: AppConfig
) -> None:
    repository = database.ensure_open()
    repository.upsert_sample(sample(NOW_MS - 3_600_000, 1.0), [])
    yesterday = NOW_MS - 24 * 3_600_000
    repository.upsert_sample(sample(yesterday, 2.0), [])

    summary = EnergyService(cfg, database, now=lambda: NOW).summary()

    assert summary["today"]["energy_wh"] == pytest.approx(1.0)
    assert summary["yesterday"]["energy_wh"] == pytest.approx(2.0)
    assert summary["last_7_days"]["energy_wh"] == pytest.approx(3.0)
    assert summary["last_30_days"]["energy_wh"] == pytest.approx(3.0)
    assert summary["price_per_kwh"] == pytest.approx(0.5)


def test_cost_is_null_when_electricity_pricing_is_disabled(
    database: AppDatabase,
) -> None:
    repository = database.ensure_open()
    repository.upsert_sample(sample(NOW_MS - 1000, 250.0), [])
    cfg = AppConfig(server=ServerConfig(timezone="UTC"), electricity=ElectricityConfig(enabled=False))

    day = EnergyService(cfg, database, now=lambda: NOW).daily(days=1)["days"][0]
    assert day["energy_wh"] == pytest.approx(250.0)
    assert day["cost"] is None


def test_yearly_cost_returns_twelve_months(database: AppDatabase, cfg: AppConfig) -> None:
    repository = database.ensure_open()
    now = datetime(2026, 6, 15, 12, 0, 0, tzinfo=UTC)

    def hour_row(bucket_ms: int, energy_wh: float) -> dict:
        return {
            "bucket_ms": bucket_ms,
            "system_w_avg": 180.0,
            "system_w_max": 190.0,
            "cpu_w_avg": 80.0,
            "gpu_w_avg": 40.0,
            "energy_wh": energy_wh,
            "energy_quality": "measured",
            "sample_count": 3600,
            "expected_count": 3600,
        }

    repository.upsert_hour_rows(
        [
            hour_row(utc_ms(datetime(2026, 1, 2, tzinfo=UTC)), 1500.0),
            hour_row(utc_ms(datetime(2026, 3, 2, tzinfo=UTC)), 2500.0),
        ]
    )
    repository.upsert_sample(sample(utc_ms(datetime(2026, 6, 14, 12, tzinfo=UTC)), 3000.0), [])

    result = EnergyService(cfg, database, now=lambda: now).yearly(2026)

    assert result["year"] == 2026
    assert 2026 in result["years"]
    assert [entry["month"] for entry in result["months"]] == list(range(1, 13))
    january, march, june, december = (
        result["months"][0],
        result["months"][2],
        result["months"][5],
        result["months"][11],
    )
    assert january["label"] == "2026-01"
    assert january["energy_kwh"] == pytest.approx(1.5)
    assert january["cost"] == pytest.approx(0.75)
    assert march["cost"] == pytest.approx(1.25)
    assert june["cost"] == pytest.approx(1.5)
    assert december["quality"] == "unavailable"
    assert december["cost"] is None


def test_yearly_defaults_to_current_year_and_keeps_cost_null_when_disabled(
    database: AppDatabase,
) -> None:
    disabled_cfg = AppConfig(
        server=ServerConfig(timezone="UTC"),
        electricity=ElectricityConfig(enabled=False),
    )

    result = EnergyService(disabled_cfg, database, now=lambda: NOW).yearly()

    assert result["year"] == 2026
    assert result["electricity_enabled"] is False
    assert all(entry["cost"] is None for entry in result["months"])
    with pytest.raises(ValueError):
        EnergyService(disabled_cfg, database, now=lambda: NOW).yearly(1960)


def test_energy_uses_hourly_aggregates_before_raw_cutoff(
    database: AppDatabase,
) -> None:
    repository = database.ensure_open()
    cfg = AppConfig(
        server=ServerConfig(timezone="UTC"),
        history=HistoryConfig(raw_retention_days=7),
    )
    old = NOW_MS - 10 * 86_400_000
    repository.upsert_hour_rows(
        [
            {
                "bucket_ms": old,
                "system_w_avg": 180.0,
                "system_w_max": 190.0,
                "cpu_w_avg": 80.0,
                "gpu_w_avg": 40.0,
                "energy_wh": 180.0,
                "energy_quality": "measured",
                "sample_count": 3600,
                "expected_count": 3600,
            }
        ]
    )
    repository.upsert_sample(sample(NOW_MS - 1000, 1.0), [])

    summary = EnergyService(cfg, database, now=lambda: NOW).summary()

    assert summary["last_30_days"]["energy_wh"] == pytest.approx(181.0)
    assert summary["last_30_days"]["quality"] == "measured"
