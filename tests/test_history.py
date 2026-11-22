from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from app.config import AppConfig, DatabaseConfig, HistoryConfig
from app.database.database import AppDatabase
from app.database.repository import utc_ms
from app.services.history_service import HistoryService, parse_series

NOW = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)
NOW_MS = utc_ms(NOW)


@pytest.fixture
def database(tmp_path: Path) -> AppDatabase:
    app_db = AppDatabase(DatabaseConfig(path=str(tmp_path / "history.db")), tmp_path / "history.db")
    app_db.ensure_open()
    yield app_db
    app_db.close()


def cfg(max_points: int = 2000) -> AppConfig:
    return AppConfig(history=HistoryConfig(max_points=max_points))


def sample_row(ts_ms: int, system_w: float | None, cpu_w: float | None = None) -> dict:
    return {
        "ts_ms": ts_ms,
        "system_w": system_w,
        "cpu_w": cpu_w,
        "gpu_w": 40.0,
    }


def minute_row(bucket_ms: int, system_w: float, cpu_w: float = 80.0) -> dict:
    return {
        "bucket_ms": bucket_ms,
        "system_w_avg": system_w,
        "system_w_max": system_w + 10,
        "cpu_w_avg": cpu_w,
        "gpu_w_avg": 40.0,
        "energy_wh": 1.0,
        "energy_quality": "estimated",
        "sample_count": 60,
        "expected_count": 60,
    }


def hour_row(bucket_ms: int, system_w: float, cpu_w: float = 80.0) -> dict:
    return {
        "bucket_ms": bucket_ms,
        "system_w_avg": system_w,
        "system_w_max": system_w + 10,
        "cpu_w_avg": cpu_w,
        "gpu_w_avg": 40.0,
        "energy_wh": 60.0,
        "energy_quality": "estimated",
        "sample_count": 3600,
        "expected_count": 3600,
    }


def test_parse_series_defaults_and_validation() -> None:
    assert parse_series(None) == ["system", "cpu", "gpu_total"]
    assert parse_series("") == ["system", "cpu", "gpu_total"]
    assert parse_series(" gpu:GPU-1 , cpu ") == ["gpu:GPU-1", "cpu"]
    with pytest.raises(ValueError):
        parse_series("system,banana")


def test_five_minute_window_reads_raw_and_downsamples(database: AppDatabase) -> None:
    repository = database.ensure_open()
    for offset, value in ((0, 100.0), (500, 200.0), (1500, 300.0)):
        repository.upsert_sample(sample_row(NOW_MS - 10_000 + offset, value), [])

    result = HistoryService(cfg(), database, now=lambda: NOW).build("5m", ["system"])

    assert result["source"] == "raw"
    assert result["resolution"] == "1s"
    assert result["window"] == "5m"
    series = result["series"][0]
    assert series["id"] == "system"
    assert series["quality"] == "estimated"
    assert series["scope"] == "dc_components"
    points = series["points"]
    assert len(points) == 2
    assert points[0][1] == pytest.approx(150.0)  # 100 and 200 in the same second
    assert points[1][1] == pytest.approx(300.0)


def test_raw_window_keeps_none_values(database: AppDatabase) -> None:
    repository = database.ensure_open()
    repository.upsert_sample(sample_row(NOW_MS - 2000, None), [])
    repository.upsert_sample(sample_row(NOW_MS - 500, 120.0), [])

    result = HistoryService(cfg(), database, now=lambda: NOW).build("5m", ["system"])
    assert [value for _, value in result["series"][0]["points"]] == [None, 120.0]


def test_six_hour_window_uses_minute_aggregates(database: AppDatabase) -> None:
    repository = database.ensure_open()
    repository.upsert_month_rows(
        [minute_row(NOW_MS - 60_000, 210.0), minute_row(NOW_MS - 120_000, 190.0)]
    )

    result = HistoryService(cfg(), database, now=lambda: NOW).build("6h", ["system", "cpu"])

    assert result["source"] == "aggregate_1m"
    assert result["resolution"] == "1m"
    system, cpu = result["series"]
    assert [value for _, value in system["points"]] == pytest.approx([190.0, 210.0])
    assert [value for _, value in cpu["points"]] == pytest.approx([80.0, 80.0])


def test_six_hour_window_falls_back_to_raw_when_no_aggregates(database: AppDatabase) -> None:
    repository = database.ensure_open()
    repository.upsert_sample(sample_row(NOW_MS - 1000, 120.0), [])

    result = HistoryService(cfg(), database, now=lambda: NOW).build("6h", ["system"])

    assert result["source"] == "raw"
    assert [value for _, value in result["series"][0]["points"]] == [120.0]


def test_week_window_uses_hourly_aggregates(database: AppDatabase) -> None:
    repository = database.ensure_open()
    repository.upsert_hour_rows(
        [hour_row(NOW_MS - 3_600_000, 220.0), hour_row(NOW_MS - 7_200_000, 180.0)]
    )

    result = HistoryService(cfg(), database, now=lambda: NOW).build("7d", ["system"])

    assert result["source"] == "aggregate_1h"
    assert result["resolution"] == "1h"
    assert [value for _, value in result["series"][0]["points"]] == pytest.approx([180.0, 220.0])


def test_downsampling_respects_max_points(database: AppDatabase) -> None:
    repository = database.ensure_open()
    rows = [sample_row(NOW_MS - 300_000 + index * 1000, float(index)) for index in range(250)]
    for row in rows:
        repository.upsert_sample(row, [])

    result = HistoryService(cfg(max_points=100), database, now=lambda: NOW).build("5m", ["system"])

    assert len(result["series"][0]["points"]) <= 100
    assert result["resolution"] == "3s"


def test_per_gpu_series_is_limited_to_24_hours(database: AppDatabase) -> None:
    repository = database.ensure_open()
    repository.upsert_sample(
        sample_row(NOW_MS - 1000, 200.0),
        [{"ts_ms": NOW_MS - 1000, "uuid": "GPU-1", "power_w": 55.0}],
    )

    recent = HistoryService(cfg(), database, now=lambda: NOW).build(
        "1h", ["gpu:GPU-1"]
    )["series"][0]
    assert recent["id"] == "gpu:GPU-1"
    assert [value for _, value in recent["points"]] == [55.0]

    distant = HistoryService(cfg(), database, now=lambda: NOW).build(
        "30d", ["gpu:GPU-1"]
    )["series"][0]
    assert distant["points"] == []
    assert "24h" in distant["note"]


def test_unknown_window_raises(database: AppDatabase) -> None:
    service = HistoryService(cfg(), database, now=lambda: NOW)
    with pytest.raises(ValueError):
        service.build("2h", ["system"])


def test_empty_window_returns_series_with_no_points(database: AppDatabase) -> None:
    result = HistoryService(cfg(), database, now=lambda: NOW).build("1h", ["system", "cpu"])
    assert result["source"] == "raw"
    assert all(series["points"] == [] for series in result["series"])
    assert result["to_ms"] - result["from_ms"] == 3600 * 1000
    assert result["from_ms"] == NOW_MS - 3600 * 1000


def test_series_metadata_is_explicit(database: AppDatabase) -> None:
    result = HistoryService(cfg(), database, now=lambda: NOW).build("15m", ["system", "cpu", "gpu_total"])
    by_id = {series["id"]: series for series in result["series"]}
    assert by_id["system"]["scope"] == "dc_components"
    assert by_id["cpu"]["scope"] == "component"
    assert by_id["gpu_total"]["unit"] == "W"
    assert by_id["gpu_total"]["quality"] == "measured"


def test_aggregate_history_reports_wall_scope_when_all_rows_measured(
    database: AppDatabase,
) -> None:
    repository = database.ensure_open()
    repository.upsert_month_rows(
        [
            minute_row(NOW_MS - 60_000, 410.0) | {"system_quality": "measured", "system_scope": "wall"},
            minute_row(NOW_MS - 120_000, 400.0) | {"system_quality": "measured", "system_scope": "wall"},
        ]
    )

    result = HistoryService(cfg(), database, now=lambda: NOW).build("6h", ["system"])
    series = result["series"][0]

    assert series["quality"] == "measured"
    assert series["scope"] == "wall"


def test_aggregate_history_mixed_rows_report_estimated_components(
    database: AppDatabase,
) -> None:
    repository = database.ensure_open()
    repository.upsert_month_rows(
        [
            minute_row(NOW_MS - 60_000, 410.0) | {"system_quality": "measured", "system_scope": "wall"},
            minute_row(NOW_MS - 120_000, 200.0) | {
                "system_quality": "estimated",
                "system_scope": "dc_components",
            },
        ]
    )

    result = HistoryService(cfg(), database, now=lambda: NOW).build("6h", ["system"])
    series = result["series"][0]

    assert series["quality"] == "estimated"
    assert series["scope"] == "dc_components"


def test_raw_history_reports_measured_wall_samples(database: AppDatabase) -> None:
    repository = database.ensure_open()
    repository.upsert_sample(
        sample_row(NOW_MS - 1000, 420.0) | {"system_quality": "measured", "system_scope": "wall"},
        [],
    )

    series = HistoryService(cfg(), database, now=lambda: NOW).build("5m", ["system"])["series"][0]

    assert series["quality"] == "measured"
    assert series["scope"] == "wall"


def test_window_boundaries_are_utc_milliseconds(database: AppDatabase) -> None:
    result = HistoryService(cfg(), database, now=lambda: NOW).build("24h", ["system"])
    assert result["to_ms"] == NOW_MS
    assert result["from_ms"] == NOW_MS - 86_400_000


def test_hourly_fallback_uses_minutes_when_hours_missing(database: AppDatabase) -> None:
    repository = database.ensure_open()
    repository.upsert_month_rows(
        [minute_row(NOW_MS - 60_000, 200.0)]
    )
    # Window starts before the raw retention cutoff, so hourly should be tried.
    service = HistoryService(
        AppConfig(history=HistoryConfig(raw_retention_days=1)),
        database,
        now=lambda: NOW,
    )
    result = service.build("7d", ["system"])
    assert result["source"] in {"aggregate_1m", "aggregate_1h"}
