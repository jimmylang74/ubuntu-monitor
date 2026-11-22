from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from app.config import DatabaseConfig
from app.database.database import AppDatabase, DatabaseUnavailable
from app.database.repository import utc_ms

NOW = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)
NOW_MS = utc_ms(NOW)


@pytest.fixture
def database(tmp_path: Path) -> AppDatabase:
    app_db = AppDatabase(DatabaseConfig(path=str(tmp_path / "power.db")), tmp_path / "power.db")
    app_db.ensure_open()
    yield app_db
    app_db.close()


def sample(ts_ms: int, system_w: float | None, energy_wh: float | None = None) -> dict:
    return {
        "ts_ms": ts_ms,
        "system_w": system_w,
        "system_quality": "estimated" if system_w is not None else "unavailable",
        "system_source": "calculator:rapl+nvml",
        "system_scope": "dc_components" if system_w is not None else None,
        "cpu_w": None if system_w is None else system_w / 2.0,
        "cpu_quality": "measured" if system_w is not None else "unavailable",
        "gpu_w": None if system_w is None else system_w / 4.0,
        "gpu_quality": "measured" if system_w is not None else "unavailable",
        "energy_wh": energy_wh,
        "energy_quality": "estimated" if energy_wh is not None else "unavailable",
    }


def hour_row(bucket_ms: int, energy_wh: float, samples: int = 5) -> dict:
    return {
        "bucket_ms": bucket_ms,
        "system_w_avg": 200.0,
        "system_w_max": 240.0,
        "cpu_w_avg": 80.0,
        "gpu_w_avg": 40.0,
        "energy_wh": energy_wh,
        "energy_quality": "measured",
        "sample_count": samples,
        "expected_count": samples,
    }


def test_schema_is_created_once_with_version(database: AppDatabase) -> None:
    repository = database.ensure_open()
    repository.ensure_schema()
    assert repository.ping() is None
    counts = repository.table_counts()
    assert set(counts) == {
        "sample",
        "gpu_sample",
        "aggregate_1m",
        "aggregate_1h",
        "meter_sample",
        "event_log",
    }
    assert all(value == 0 for value in counts.values())
    assert database.status()["state"] == "ok"


def test_missing_values_stay_null_not_zero(database: AppDatabase) -> None:
    repository = database.ensure_open()
    repository.upsert_sample(sample(NOW_MS, None), [])

    rows = repository.fetch_raw_samples(NOW_MS, NOW_MS + 1000)
    assert len(rows) == 1
    assert rows[0]["system_w"] is None
    assert rows[0]["cpu_w"] is None
    assert rows[0]["gpu_w"] is None


def test_upsert_is_idempotent_and_updates_values(database: AppDatabase) -> None:
    repository = database.ensure_open()
    repository.upsert_sample(sample(NOW_MS, 100.0), [])
    repository.upsert_sample(sample(NOW_MS, 250.0), [])

    rows = repository.fetch_raw_samples(NOW_MS, NOW_MS + 1000)
    assert len(rows) == 1
    assert rows[0]["system_w"] == 250.0


def test_gpu_rows_are_upserted_per_uuid(database: AppDatabase) -> None:
    repository = database.ensure_open()
    record = sample(NOW_MS, 300.0)
    gpu_rows = [
        {"ts_ms": NOW_MS, "uuid": "GPU-1", "power_w": 50.0, "power_quality": "measured"},
        {"ts_ms": NOW_MS, "uuid": "GPU-2", "power_w": 60.0, "power_quality": "measured"},
    ]
    repository.upsert_sample(record, gpu_rows)
    repository.upsert_sample(
        sample(NOW_MS, 300.0),
        [{"ts_ms": NOW_MS, "uuid": "GPU-1", "power_w": 55.0, "power_quality": "measured"}],
    )

    first = repository.fetch_gpu_samples("GPU-1", NOW_MS, NOW_MS + 1000)
    second = repository.fetch_gpu_samples("GPU-2", NOW_MS, NOW_MS + 1000)
    assert [row["power_w"] for row in first] == [55.0]
    assert [row["power_w"] for row in second] == [60.0]


def test_minute_aggregation_from_raw_marks_missing_energy(database: AppDatabase) -> None:
    repository = database.ensure_open()
    for offset, system_w, energy in ((0, 100.0, 1.0), (20, 200.0, 2.0), (40, None, None)):
        repository.upsert_sample(sample(NOW_MS + offset, system_w, energy), [])

    buckets = repository.minute_buckets_from_raw(NOW_MS, NOW_MS + 60_000)
    assert len(buckets) == 1
    bucket = buckets[0]
    assert bucket["bucket_ms"] == NOW_MS // 60_000 * 60_000
    assert bucket["sample_count"] == 3
    assert bucket["system_w_avg"] == pytest.approx(150.0)
    assert bucket["system_w_max"] == pytest.approx(200.0)
    assert bucket["energy_wh"] == pytest.approx(3.0)
    assert bucket["missing_energy"] == 1


def test_hour_aggregation_uses_sample_weighted_averages(database: AppDatabase) -> None:
    repository = database.ensure_open()
    repository.upsert_month_rows(
        [
            {**hour_row(NOW_MS, 1.0, samples=10), "system_w_avg": 100.0},
            {**hour_row(NOW_MS + 60_000, 2.0, samples=30), "system_w_avg": 300.0},
        ]
    )

    hours = repository.hour_buckets_from_minutes(NOW_MS, NOW_MS + 3_600_000)
    assert len(hours) == 1
    hour = hours[0]
    assert hour["bucket_ms"] == NOW_MS // 3_600_000 * 3_600_000
    assert hour["sample_count"] == 40
    assert hour["system_w_avg"] == pytest.approx((100.0 * 10 + 300.0 * 30) / 40)
    assert hour["energy_wh"] == pytest.approx(3.0)


def test_energy_partitions_raw_and_hourly_at_cutoff(database: AppDatabase) -> None:
    repository = database.ensure_open()
    cutoff_ms = NOW_MS + 10_000
    repository.upsert_hour_rows([hour_row(NOW_MS, 10.0, samples=5)])
    repository.upsert_sample(sample(cutoff_ms + 1000, 200.0, 1.0), [])
    repository.upsert_sample(sample(cutoff_ms + 2000, 200.0, 2.0), [])

    total = repository.energy_between(NOW_MS, cutoff_ms + 5000, cutoff_ms)
    assert total["energy_wh"] == pytest.approx(13.0)
    assert total["samples"] == 7
    assert total["quality"] == "estimated"

    old = repository.energy_between(NOW_MS, cutoff_ms, cutoff_ms)
    assert old["energy_wh"] == pytest.approx(10.0)
    assert old["samples"] == 5
    assert old["quality"] == "measured"

    recent = repository.energy_between(cutoff_ms, cutoff_ms + 5000, cutoff_ms)
    assert recent["energy_wh"] == pytest.approx(3.0)
    assert recent["samples"] == 2


def test_energy_returns_unavailable_without_rows(database: AppDatabase) -> None:
    repository = database.ensure_open()
    result = repository.energy_between(NOW_MS, NOW_MS + 1000, NOW_MS)
    assert result == {"energy_wh": None, "quality": "unavailable", "samples": 0, "missing": 0}


def test_energy_never_reports_zero_for_unknown_values(database: AppDatabase) -> None:
    repository = database.ensure_open()
    repository.upsert_sample(sample(NOW_MS, 100.0, None), [])

    result = repository.energy_between(NOW_MS, NOW_MS + 1000, NOW_MS)

    assert result["energy_wh"] is None
    assert result["quality"] == "unavailable"
    assert result["samples"] == 1
    assert result["missing"] == 1


def test_retention_deletes_only_before_cutoff(database: AppDatabase) -> None:
    repository = database.ensure_open()
    old = NOW_MS - 10 * 86_400_000
    repository.upsert_sample(sample(old, 100.0), [{"ts_ms": old, "uuid": "GPU-1", "power_w": 10.0}])
    repository.upsert_sample(sample(NOW_MS, 100.0), [])
    repository.upsert_month_rows([hour_row(old, 1.0)])
    repository.upsert_hour_rows([hour_row(old, 1.0)])

    assert repository.delete_before("sample", NOW_MS) == 1
    assert repository.delete_before("gpu_sample", NOW_MS) == 1
    assert repository.delete_before("aggregate_1m", NOW_MS) == 1
    assert repository.delete_before("aggregate_1h", NOW_MS) == 1

    assert repository.table_counts() == {
        "sample": 1,
        "gpu_sample": 0,
        "aggregate_1m": 0,
        "aggregate_1h": 0,
        "meter_sample": 0,
        "event_log": 0,
    }
    with pytest.raises(ValueError):
        repository.delete_before("not_a_table", NOW_MS)


def test_events_are_stored(database: AppDatabase) -> None:
    database.write_event("warning", "database", "disk slow")
    assert database.ensure_open().table_counts()["event_log"] == 1


def test_unopenable_database_reports_degraded(tmp_path: Path) -> None:
    broken = tmp_path / "not-a-file"
    broken.mkdir()
    app_db = AppDatabase(DatabaseConfig(path=str(broken)), broken)

    with pytest.raises(DatabaseUnavailable):
        app_db.ensure_open()
    with pytest.raises(DatabaseUnavailable):
        app_db.ensure_open()
    assert app_db.status()["state"] == "degraded"
    assert app_db.status()["error"]

    # Event writes are best effort and never raise.
    app_db.write_event("error", "database", "cannot open")
    app_db.close()


def test_database_recovers_after_initial_failure(tmp_path: Path, monkeypatch) -> None:
    broken = tmp_path / "later-a-file"
    broken.mkdir()
    app_db = AppDatabase(DatabaseConfig(path=str(broken)), broken)
    monkeypatch.setattr(AppDatabase, "RETRY_INTERVAL_S", 0.0)

    with pytest.raises(DatabaseUnavailable):
        app_db.ensure_open()

    broken.rmdir()
    repository = app_db.ensure_open()

    assert repository.ping() is None
    assert app_db.status()["state"] == "ok"
    assert app_db.status()["error"] is None
    app_db.close()


def test_time_helpers_use_unix_milliseconds() -> None:
    assert utc_ms(NOW) == int(NOW.timestamp() * 1000)
    assert utc_ms(NOW + timedelta(seconds=1)) == utc_ms(NOW) + 1000
