"""The only module allowed to contain SQL.

The writer uses one short transaction per snapshot; reads use their own
connections (SQLite WAL allows concurrent readers while the writer commits).
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import Engine, create_engine, event, select, text
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import NullPool

from app.database.models import (
    SCHEMA_VERSION,
    Aggregate1h,
    Aggregate1m,
    Base,
    EventLog,
    GpuSample,
    MeterSample,
    Sample,
    SchemaMeta,
)

RAW_COLUMNS = ("ts_ms", "system_w", "cpu_w", "gpu_w")

_DELETE_SQL = {
    "sample": (
        "DELETE FROM sample WHERE ts_ms IN "
        "(SELECT ts_ms FROM sample WHERE ts_ms < :cutoff LIMIT :batch)"
    ),
    "gpu_sample": (
        "DELETE FROM gpu_sample WHERE rowid IN "
        "(SELECT rowid FROM gpu_sample WHERE ts_ms < :cutoff LIMIT :batch)"
    ),
    "aggregate_1m": (
        "DELETE FROM aggregate_1m WHERE bucket_ms IN "
        "(SELECT bucket_ms FROM aggregate_1m WHERE bucket_ms < :cutoff LIMIT :batch)"
    ),
    "aggregate_1h": (
        "DELETE FROM aggregate_1h WHERE bucket_ms IN "
        "(SELECT bucket_ms FROM aggregate_1h WHERE bucket_ms < :cutoff LIMIT :batch)"
    ),
    "event_log": (
        "DELETE FROM event_log WHERE id IN "
        "(SELECT id FROM event_log WHERE ts_ms < :cutoff LIMIT :batch)"
    ),
    "meter_sample": (
        "DELETE FROM meter_sample WHERE ts_ms IN "
        "(SELECT ts_ms FROM meter_sample WHERE ts_ms < :cutoff LIMIT :batch)"
    ),
}


def create_engine_for(path: Path, busy_timeout_ms: int, wal: bool) -> Engine:
    path.parent.mkdir(parents=True, exist_ok=True)
    engine = create_engine(
        f"sqlite+pysqlite:///{path}",
        connect_args={
            "timeout": max(busy_timeout_ms, 1) / 1000.0,
            "check_same_thread": False,
        },
        poolclass=NullPool,
    )

    @event.listens_for(engine, "connect")
    def _set_pragmas(dbapi_connection: Any, _record: Any) -> None:
        cursor = dbapi_connection.cursor()
        cursor.execute(f"PRAGMA busy_timeout={int(busy_timeout_ms)}")
        cursor.execute("PRAGMA synchronous=NORMAL")
        cursor.execute("PRAGMA temp_store=MEMORY")
        cursor.close()

    if wal:
        with engine.connect() as connection:
            connection.exec_driver_sql("PRAGMA journal_mode=WAL")
    return engine


class Repository:
    def __init__(self, engine: Engine) -> None:
        self.engine = engine
        self._session = sessionmaker(engine, expire_on_commit=False)

    # ------------------------------------------------------------------ schema

    def ensure_schema(self) -> None:
        Base.metadata.create_all(self.engine)
        self._add_missing_columns()
        with self._session() as session, session.begin():
            meta = session.get(SchemaMeta, "schema_version")
            if meta is None:
                session.add(SchemaMeta(key="schema_version", value=SCHEMA_VERSION))
            elif meta.value != SCHEMA_VERSION:
                meta.value = SCHEMA_VERSION

    def _add_missing_columns(self) -> None:
        """Lightweight additive migration for existing databases.

        ``create_all`` never alters an existing table, so Phase 4's aggregate
        quality/scope columns are added explicitly.  No column is dropped and
        no data is rewritten.
        """
        additions = {
            "aggregate_1m": {
                "system_quality": "VARCHAR(16)",
                "system_scope": "VARCHAR(32)",
            },
            "aggregate_1h": {
                "system_quality": "VARCHAR(16)",
                "system_scope": "VARCHAR(32)",
            },
        }
        with self.engine.connect() as connection:
            for table, columns in additions.items():
                existing = {
                    row[1]
                    for row in connection.exec_driver_sql(
                        f"PRAGMA table_info({table})"
                    )
                }
                for name, ddl in columns.items():
                    if name not in existing:
                        connection.exec_driver_sql(
                            f"ALTER TABLE {table} ADD COLUMN {name} {ddl}"
                        )
            connection.commit()

    def ping(self) -> None:
        with self.engine.connect() as connection:
            connection.execute(text("SELECT 1"))

    # ------------------------------------------------------------------ writes

    def upsert_sample(
        self, record: dict[str, Any], gpu_rows: Sequence[dict[str, Any]]
    ) -> None:
        with self._session() as session, session.begin():
            statement = sqlite_insert(Sample).values(**record)
            statement = statement.on_conflict_do_update(
                index_elements=["ts_ms"],
                set_={key: getattr(statement.excluded, key) for key in record if key != "ts_ms"},
            )
            session.execute(statement)

            if gpu_rows:
                gpu_statement = sqlite_insert(GpuSample).values(list(gpu_rows))
                gpu_statement = gpu_statement.on_conflict_do_update(
                    index_elements=["ts_ms", "uuid"],
                    set_={
                        key: getattr(gpu_statement.excluded, key)
                        for key in gpu_rows[0]
                        if key not in {"ts_ms", "uuid"}
                    },
                )
                session.execute(gpu_statement)

    def write_event(self, level: str, component: str, message: str, at: datetime) -> None:
        with self._session() as session, session.begin():
            session.add(
                EventLog(
                    ts_ms=int(at.timestamp() * 1000),
                    level=level,
                    component=component,
                    message=message[:2000],
                )
            )

    def upsert_meter_sample(self, row: dict[str, Any]) -> None:
        with self._session() as session, session.begin():
            statement = sqlite_insert(MeterSample).values(**row)
            statement = statement.on_conflict_do_update(
                index_elements=["ts_ms"],
                set_={
                    key: getattr(statement.excluded, key)
                    for key in row
                    if key != "ts_ms"
                },
            )
            session.execute(statement)

    def upsert_month_rows(self, rows: Iterable[dict[str, Any]]) -> None:
        rows = list(rows)
        if not rows:
            return
        with self._session() as session, session.begin():
            statement = sqlite_insert(Aggregate1m).values(rows)
            statement = statement.on_conflict_do_update(
                index_elements=["bucket_ms"],
                set_={
                    key: getattr(statement.excluded, key)
                    for key in rows[0]
                    if key != "bucket_ms"
                },
            )
            session.execute(statement)

    def upsert_hour_rows(self, rows: Iterable[dict[str, Any]]) -> None:
        rows = list(rows)
        if not rows:
            return
        with self._session() as session, session.begin():
            statement = sqlite_insert(Aggregate1h).values(rows)
            statement = statement.on_conflict_do_update(
                index_elements=["bucket_ms"],
                set_={
                    key: getattr(statement.excluded, key)
                    for key in rows[0]
                    if key != "bucket_ms"
                },
            )
            session.execute(statement)

    # ------------------------------------------------------------------ reads

    def fetch_raw_samples(self, start_ms: int, end_ms: int) -> list[dict[str, Any]]:
        with self._session() as session:
            rows = session.execute(
                select(
                    Sample.ts_ms,
                    Sample.system_w,
                    Sample.system_quality,
                    Sample.system_scope,
                    Sample.cpu_w,
                    Sample.gpu_w,
                )
                .where(Sample.ts_ms >= start_ms, Sample.ts_ms < end_ms)
                .order_by(Sample.ts_ms)
            )
            return [dict(row._mapping) for row in rows]

    def fetch_gpu_samples(self, uuid: str, start_ms: int, end_ms: int) -> list[dict[str, Any]]:
        with self._session() as session:
            rows = session.execute(
                select(GpuSample.ts_ms, GpuSample.power_w)
                .where(
                    GpuSample.uuid == uuid,
                    GpuSample.ts_ms >= start_ms,
                    GpuSample.ts_ms < end_ms,
                )
                .order_by(GpuSample.ts_ms)
            )
            return [dict(row._mapping) for row in rows]

    def fetch_minute_buckets(self, start_ms: int, end_ms: int) -> list[dict[str, Any]]:
        with self._session() as session:
            rows = session.execute(
                select(
                    Aggregate1m.bucket_ms,
                    Aggregate1m.system_w_avg,
                    Aggregate1m.system_quality,
                    Aggregate1m.system_scope,
                    Aggregate1m.cpu_w_avg,
                    Aggregate1m.gpu_w_avg,
                )
                .where(
                    Aggregate1m.bucket_ms >= start_ms,
                    Aggregate1m.bucket_ms < end_ms,
                )
                .order_by(Aggregate1m.bucket_ms)
            )
            return [dict(row._mapping) for row in rows]

    def fetch_hour_buckets(self, start_ms: int, end_ms: int) -> list[dict[str, Any]]:
        with self._session() as session:
            rows = session.execute(
                select(
                    Aggregate1h.bucket_ms,
                    Aggregate1h.system_w_avg,
                    Aggregate1h.system_quality,
                    Aggregate1h.system_scope,
                    Aggregate1h.cpu_w_avg,
                    Aggregate1h.gpu_w_avg,
                )
                .where(
                    Aggregate1h.bucket_ms >= start_ms,
                    Aggregate1h.bucket_ms < end_ms,
                )
                .order_by(Aggregate1h.bucket_ms)
            )
            return [dict(row._mapping) for row in rows]

    # ------------------------------------------------------------------ aggregation

    def minute_buckets_from_raw(self, start_ms: int, end_ms: int) -> list[dict[str, Any]]:
        sql = text(
            """
            SELECT CAST(ts_ms / 60000 AS INTEGER) * 60000 AS bucket_ms,
                   COUNT(*) AS sample_count,
                   AVG(system_w) AS system_w_avg,
                   MAX(system_w) AS system_w_max,
                   CASE
                     WHEN SUM(CASE WHEN system_w IS NOT NULL THEN 1 ELSE 0 END) = 0
                       THEN 'unavailable'
                     WHEN SUM(CASE WHEN system_w IS NOT NULL THEN 1 ELSE 0 END) =
                          SUM(CASE WHEN system_w IS NOT NULL AND system_quality = 'measured'
                                   THEN 1 ELSE 0 END)
                       THEN 'measured'
                     ELSE 'estimated'
                   END AS system_quality,
                   CASE
                     WHEN SUM(CASE WHEN system_w IS NOT NULL THEN 1 ELSE 0 END) = 0
                       THEN NULL
                     WHEN SUM(CASE WHEN system_w IS NOT NULL THEN 1 ELSE 0 END) =
                          SUM(CASE WHEN system_w IS NOT NULL AND system_quality = 'measured'
                                   THEN 1 ELSE 0 END)
                       THEN 'wall'
                     ELSE 'dc_components'
                   END AS system_scope,
                   AVG(cpu_w) AS cpu_w_avg,
                   AVG(gpu_w) AS gpu_w_avg,
                   SUM(energy_wh) AS energy_wh,
                   SUM(CASE WHEN energy_wh IS NULL THEN 1 ELSE 0 END) AS missing_energy,
                   SUM(CASE WHEN energy_quality = 'estimated' THEN 1 ELSE 0 END) AS estimated_energy
            FROM sample
            WHERE ts_ms >= :start_ms AND ts_ms < :end_ms
            GROUP BY bucket_ms
            ORDER BY bucket_ms
            """
        )
        with self.engine.connect() as connection:
            rows = connection.execute(sql, {"start_ms": start_ms, "end_ms": end_ms})
            return [dict(row._mapping) for row in rows]

    def hour_buckets_from_minutes(self, start_ms: int, end_ms: int) -> list[dict[str, Any]]:
        sql = text(
            """
            SELECT CAST(bucket_ms / 3600000 AS INTEGER) * 3600000 AS bucket_ms,
                   SUM(sample_count) AS sample_count,
                   SUM(expected_count) AS expected_count,
                   SUM(CASE WHEN system_w_avg IS NOT NULL THEN system_w_avg * sample_count ELSE 0 END)
                       / NULLIF(SUM(CASE WHEN system_w_avg IS NOT NULL THEN sample_count ELSE 0 END), 0)
                       AS system_w_avg,
                   MAX(system_w_max) AS system_w_max,
                   CASE
                     WHEN SUM(CASE WHEN system_w_avg IS NOT NULL THEN 1 ELSE 0 END) = 0
                       THEN 'unavailable'
                     WHEN SUM(CASE WHEN system_w_avg IS NOT NULL THEN 1 ELSE 0 END) =
                          SUM(CASE WHEN system_w_avg IS NOT NULL AND system_quality = 'measured'
                                   THEN 1 ELSE 0 END)
                       THEN 'measured'
                     ELSE 'estimated'
                   END AS system_quality,
                   CASE
                     WHEN SUM(CASE WHEN system_w_avg IS NOT NULL THEN 1 ELSE 0 END) = 0
                       THEN NULL
                     WHEN SUM(CASE WHEN system_w_avg IS NOT NULL THEN 1 ELSE 0 END) =
                          SUM(CASE WHEN system_w_avg IS NOT NULL AND system_quality = 'measured'
                                   THEN 1 ELSE 0 END)
                       THEN 'wall'
                     ELSE 'dc_components'
                   END AS system_scope,
                   SUM(CASE WHEN cpu_w_avg IS NOT NULL THEN cpu_w_avg * sample_count ELSE 0 END)
                       / NULLIF(SUM(CASE WHEN cpu_w_avg IS NOT NULL THEN sample_count ELSE 0 END), 0)
                       AS cpu_w_avg,
                   SUM(CASE WHEN gpu_w_avg IS NOT NULL THEN gpu_w_avg * sample_count ELSE 0 END)
                       / NULLIF(SUM(CASE WHEN gpu_w_avg IS NOT NULL THEN sample_count ELSE 0 END), 0)
                       AS gpu_w_avg,
                   SUM(energy_wh) AS energy_wh,
                   SUM(CASE WHEN energy_wh IS NULL THEN 1 ELSE 0 END) AS missing_energy,
                   SUM(CASE WHEN energy_quality = 'estimated' THEN 1 ELSE 0 END) AS estimated_energy
            FROM aggregate_1m
            WHERE bucket_ms >= :start_ms AND bucket_ms < :end_ms
            GROUP BY CAST(bucket_ms / 3600000 AS INTEGER)
            ORDER BY bucket_ms
            """
        )
        with self.engine.connect() as connection:
            rows = connection.execute(sql, {"start_ms": start_ms, "end_ms": end_ms})
            return [dict(row._mapping) for row in rows]

    # ------------------------------------------------------------------ energy

    def energy_between(self, start_ms: int, end_ms: int, raw_cutoff_ms: int) -> dict[str, Any]:
        """Sum stored energy for [start, end).

        Data newer than ``raw_cutoff_ms`` comes from raw samples; older data
        comes from hourly aggregates.  The two ranges never overlap, so energy
        is not double counted.
        """
        raw_start = max(start_ms, raw_cutoff_ms)
        totals = {"wh": 0.0, "samples": 0, "missing": 0, "estimated": 0, "energy_rows": 0}
        any_data = False

        if raw_start < end_ms:
            with self.engine.connect() as connection:
                row = connection.execute(
                    text(
                        """
                        SELECT SUM(energy_wh) AS wh,
                               COUNT(*) AS samples,
                               SUM(CASE WHEN energy_wh IS NULL THEN 1 ELSE 0 END) AS missing,
                               SUM(CASE WHEN energy_quality = 'estimated' THEN 1 ELSE 0 END) AS estimated,
                               COUNT(energy_wh) AS energy_rows
                        FROM sample WHERE ts_ms >= :start_ms AND ts_ms < :end_ms
                        """
                    ),
                    {"start_ms": raw_start, "end_ms": end_ms},
                ).one()
            if row.samples:
                any_data = True
                totals["wh"] += float(row.wh or 0.0)
                totals["samples"] += int(row.samples)
                totals["missing"] += int(row.missing or 0)
                totals["estimated"] += int(row.estimated or 0)
                totals["energy_rows"] += int(row.energy_rows or 0)

        if start_ms < min(end_ms, raw_cutoff_ms):
            with self.engine.connect() as connection:
                row = connection.execute(
                    text(
                        """
                        SELECT SUM(energy_wh) AS wh,
                               SUM(sample_count) AS samples,
                               SUM(CASE WHEN energy_wh IS NULL THEN 1 ELSE 0 END) AS missing,
                               SUM(CASE WHEN energy_quality = 'estimated' THEN 1 ELSE 0 END) AS estimated,
                               COUNT(energy_wh) AS energy_rows
                        FROM aggregate_1h WHERE bucket_ms >= :start_ms AND bucket_ms < :end_ms
                        """
                    ),
                    {"start_ms": start_ms, "end_ms": min(end_ms, raw_cutoff_ms)},
                ).one()
            if row.samples:
                any_data = True
                totals["wh"] += float(row.wh or 0.0)
                totals["samples"] += int(row.samples)
                totals["missing"] += int(row.missing or 0)
                totals["estimated"] += int(row.estimated or 0)
                totals["energy_rows"] += int(row.energy_rows or 0)

        if not any_data or not totals["energy_rows"]:
            # Rows exist but every energy value is NULL: reporting 0 Wh would
            # fabricate a measurement.  Unknown energy stays unavailable.
            return {
                "energy_wh": None,
                "quality": "unavailable",
                "samples": totals["samples"],
                "missing": totals["missing"],
            }
        quality = "estimated" if (totals["missing"] or totals["estimated"]) else "measured"
        return {
            "energy_wh": totals["wh"],
            "quality": quality,
            "samples": totals["samples"],
            "missing": totals["missing"],
        }

    # ------------------------------------------------------------------ retention

    def delete_before(self, table: str, cutoff_ms: int, batch: int = 10_000) -> int:
        statement = _DELETE_SQL.get(table)
        if statement is None:
            raise ValueError(f"unsupported retention table: {table}")
        with self.engine.connect() as connection:
            result = connection.execute(
                text(statement), {"cutoff": cutoff_ms, "batch": batch}
            )
            connection.commit()
            return int(result.rowcount or 0)

    def table_counts(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        with self.engine.connect() as connection:
            for table in (
                "sample",
                "gpu_sample",
                "aggregate_1m",
                "aggregate_1h",
                "meter_sample",
                "event_log",
            ):
                counts[table] = int(
                    connection.execute(text(f"SELECT COUNT(*) FROM {table}")).scalar_one()
                )
        return counts


def utc_ms(at: datetime) -> int:
    return int(at.astimezone(UTC).timestamp() * 1000)
