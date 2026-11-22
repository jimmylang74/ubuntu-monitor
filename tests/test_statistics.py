from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from app.config import AppConfig, DatabaseConfig, HistoryConfig
from app.database.database import AppDatabase
from app.database.repository import utc_ms
from app.services.retention_service import RetentionService
from app.services.statistics_service import StatisticsService

NOW = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)
NOW_MS = utc_ms(NOW)


@pytest.fixture
def database(tmp_path: Path) -> AppDatabase:
    app_db = AppDatabase(DatabaseConfig(path=str(tmp_path / "stats.db")), tmp_path / "stats.db")
    app_db.ensure_open()
    yield app_db
    app_db.close()


def sample(ts_ms: int, system_w: float | None, energy_wh: float | None) -> dict:
    return {
        "ts_ms": ts_ms,
        "system_w": system_w,
        "system_quality": "estimated" if system_w is not None else "unavailable",
        "cpu_w": None if system_w is None else system_w / 2,
        "cpu_quality": "measured" if system_w is not None else "unavailable",
        "gpu_w": 40.0 if system_w is not None else None,
        "gpu_quality": "measured" if system_w is not None else "unavailable",
        "energy_wh": energy_wh,
        "energy_quality": "measured" if energy_wh is not None else "unavailable",
    }


def minute_row(bucket_ms: int, avg: float, samples: int = 60) -> dict:
    return {
        "bucket_ms": bucket_ms,
        "system_w_avg": avg,
        "system_w_max": avg + 10,
        "cpu_w_avg": avg / 2,
        "gpu_w_avg": 40.0,
        "energy_wh": 1.0,
        "energy_quality": "measured",
        "sample_count": samples,
        "expected_count": samples,
    }


def test_statistics_rolls_raw_into_minutes_and_hours(database: AppDatabase) -> None:
    repository = database.ensure_open()
    previous_minute = NOW_MS - 60_000
    repository.upsert_sample(sample(previous_minute + 0, 100.0, 1.0), [])
    repository.upsert_sample(sample(previous_minute + 20_000, 300.0, 2.0), [])

    service = StatisticsService(
        AppConfig(database=DatabaseConfig(write_interval=1.0)),
        database,
        now=lambda: NOW,
    )
    counts = service.run_once(at=NOW)

    assert counts["minutes"] == 1
    minute = repository.fetch_minute_buckets(previous_minute, previous_minute + 60_000)
    assert len(minute) == 1
    assert minute[0]["bucket_ms"] == previous_minute
    assert minute[0]["system_w_avg"] == pytest.approx(200.0)
    assert minute[0]["cpu_w_avg"] == pytest.approx(100.0)
    assert minute[0]["gpu_w_avg"] == pytest.approx(40.0)

    assert counts["hours"] == 1
    hours = repository.fetch_hour_buckets(NOW_MS - 3_600_000, NOW_MS)
    assert len(hours) == 1
    assert hours[0]["bucket_ms"] == NOW_MS - 3_600_000
    assert hours[0]["system_w_avg"] == pytest.approx(200.0)


def test_statistics_propagates_measured_wall_quality(database: AppDatabase) -> None:
    repository = database.ensure_open()
    previous_minute = NOW_MS - 60_000
    measured = {"system_quality": "measured", "system_scope": "wall"}
    repository.upsert_sample(
        sample(previous_minute, 400.0, 1.0) | measured, []
    )
    repository.upsert_sample(
        sample(previous_minute + 20_000, 420.0, 2.0) | measured, []
    )

    service = StatisticsService(AppConfig(), database, now=lambda: NOW)
    service.run_once(at=NOW)

    minute = repository.fetch_minute_buckets(previous_minute, previous_minute + 60_000)[0]
    assert minute["system_quality"] == "measured"
    assert minute["system_scope"] == "wall"
    hour = repository.fetch_hour_buckets(NOW_MS - 3_600_000, NOW_MS)[0]
    assert hour["system_quality"] == "measured"
    assert hour["system_scope"] == "wall"


def test_statistics_mixed_quality_is_estimated_components(database: AppDatabase) -> None:
    repository = database.ensure_open()
    previous_minute = NOW_MS - 60_000
    repository.upsert_sample(
        sample(previous_minute, 400.0, 1.0)
        | {"system_quality": "measured", "system_scope": "wall"},
        [],
    )
    repository.upsert_sample(
        sample(previous_minute + 20_000, 300.0, 2.0)
        | {"system_quality": "estimated", "system_scope": "dc_components"},
        [],
    )

    StatisticsService(AppConfig(), database, now=lambda: NOW).run_once(at=NOW)

    minute = repository.fetch_minute_buckets(previous_minute, previous_minute + 60_000)[0]
    assert minute["system_quality"] == "estimated"
    assert minute["system_scope"] == "dc_components"


def test_statistics_marks_partial_minute_energy_estimated(database: AppDatabase) -> None:
    repository = database.ensure_open()
    previous_minute = NOW_MS - 60_000
    repository.upsert_sample(sample(previous_minute, 100.0, 1.0), [])
    repository.upsert_sample(sample(previous_minute + 20_000, None, None), [])

    service = StatisticsService(AppConfig(), database, now=lambda: NOW)
    service.run_once(at=NOW)

    rows = repository.fetch_minute_buckets(previous_minute, previous_minute + 60_000)
    assert len(rows) == 1
    # Derived quality is not exposed by fetch_minute_buckets, so inspect the table.
    with repository.engine.connect() as connection:
        from sqlalchemy import text

        quality = connection.execute(
            text("SELECT energy_quality FROM aggregate_1m WHERE bucket_ms = :bucket"),
            {"bucket": previous_minute},
        ).scalar_one()
    assert quality == "estimated"


def test_statistics_is_idempotent(database: AppDatabase) -> None:
    repository = database.ensure_open()
    previous_minute = NOW_MS - 60_000
    repository.upsert_sample(sample(previous_minute, 100.0, 1.0), [])

    service = StatisticsService(AppConfig(), database, now=lambda: NOW)
    service.run_once(at=NOW)
    service.run_once(at=NOW)

    with repository.engine.connect() as connection:
        from sqlalchemy import text

        count = connection.execute(text("SELECT COUNT(*) FROM aggregate_1m")).scalar_one()
    assert count == 1


def test_statistics_handles_empty_raw_data(database: AppDatabase) -> None:
    counts = StatisticsService(AppConfig(), database, now=lambda: NOW).run_once(at=NOW)
    assert counts == {"minutes": 0, "hours": 0}


def test_statistics_backfills_after_long_downtime(database: AppDatabase) -> None:
    repository = database.ensure_open()
    two_days_ago = NOW_MS - 2 * 86_400_000
    repository.upsert_sample(sample(two_days_ago, 100.0, 1.0), [])

    service = StatisticsService(AppConfig(), database, now=lambda: NOW)
    counts = service.run_once(at=NOW)

    assert counts["minutes"] == 1
    minutes = repository.fetch_minute_buckets(two_days_ago, two_days_ago + 60_000)
    assert len(minutes) == 1

    assert counts["hours"] == 1
    hours = repository.fetch_hour_buckets(two_days_ago, two_days_ago + 3_600_000)
    assert len(hours) == 1


async def test_retention_removes_only_expired_rows(database: AppDatabase) -> None:
    repository = database.ensure_open()
    cfg = AppConfig(
        history=HistoryConfig(
            raw_retention_days=2,
            minute_retention_days=3,
            hourly_retention_days=4,
        )
    )
    old = NOW_MS - 5 * 86_400_000
    recent = NOW_MS - 1000
    repository.upsert_sample(sample(old, 100.0, 1.0), [{"ts_ms": old, "uuid": "GPU-1", "power_w": 1.0}])
    repository.upsert_sample(sample(recent, 100.0, 1.0), [])
    repository.upsert_month_rows([minute_row(old, 100.0)])
    repository.upsert_hour_rows(
        [minute_row(old, 100.0) | {"bucket_ms": old}]
    )

    service = RetentionService(cfg, database, now=lambda: NOW)
    counts = await service.run_once(at=NOW)

    assert counts == {
        "sample": 1,
        "gpu_sample": 1,
        "meter_sample": 0,
        "aggregate_1m": 1,
        "aggregate_1h": 1,
    }
    assert repository.table_counts()["sample"] == 1
    assert repository.table_counts()["gpu_sample"] == 0


async def test_retention_start_and_stop_are_idempotent(database: AppDatabase) -> None:
    service = RetentionService(AppConfig(), database, now=lambda: NOW)
    await service.start()
    await service.start()
    await service.stop()
    await service.stop()
    assert service._task is None
