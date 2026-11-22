"""SQLAlchemy 2.0 schema.

All timestamps are Unix milliseconds (UTC).  Missing values are SQL NULL with a
parallel ``*_quality`` column set to ``unavailable`` -- zero is never used as a
substitute for "unknown".
"""

from __future__ import annotations

from sqlalchemy import BigInteger, Float, Index, Integer, String, Text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

SCHEMA_VERSION = "3"


class Base(DeclarativeBase):
    pass


class Sample(Base):
    __tablename__ = "sample"

    ts_ms: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    system_w: Mapped[float | None] = mapped_column(Float)
    system_quality: Mapped[str | None] = mapped_column(String(16))
    system_source: Mapped[str | None] = mapped_column(Text)
    system_scope: Mapped[str | None] = mapped_column(String(32))
    cpu_w: Mapped[float | None] = mapped_column(Float)
    cpu_quality: Mapped[str | None] = mapped_column(String(16))
    gpu_w: Mapped[float | None] = mapped_column(Float)
    gpu_quality: Mapped[str | None] = mapped_column(String(16))
    cpu_usage_pct: Mapped[float | None] = mapped_column(Float)
    cpu_temp_c: Mapped[float | None] = mapped_column(Float)
    gpu_usage_pct: Mapped[float | None] = mapped_column(Float)
    gpu_temp_c: Mapped[float | None] = mapped_column(Float)
    mem_used_b: Mapped[int | None] = mapped_column(BigInteger)
    mem_total_b: Mapped[int | None] = mapped_column(BigInteger)
    mem_usage_pct: Mapped[float | None] = mapped_column(Float)
    swap_used_b: Mapped[int | None] = mapped_column(BigInteger)
    energy_wh: Mapped[float | None] = mapped_column(Float)
    energy_quality: Mapped[str | None] = mapped_column(String(16))
    meter_w: Mapped[float | None] = mapped_column(Float)


Index("idx_sample_ts", Sample.ts_ms)


class GpuSample(Base):
    __tablename__ = "gpu_sample"

    ts_ms: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    uuid: Mapped[str] = mapped_column(String(64), primary_key=True)
    power_w: Mapped[float | None] = mapped_column(Float)
    power_quality: Mapped[str | None] = mapped_column(String(16))
    temp_c: Mapped[float | None] = mapped_column(Float)
    util_pct: Mapped[float | None] = mapped_column(Float)
    mem_used_b: Mapped[int | None] = mapped_column(BigInteger)
    mem_total_b: Mapped[int | None] = mapped_column(BigInteger)
    power_limit_w: Mapped[float | None] = mapped_column(Float)
    fan_pct: Mapped[float | None] = mapped_column(Float)
    clock_mhz: Mapped[float | None] = mapped_column(Float)
    mem_clock_mhz: Mapped[float | None] = mapped_column(Float)


Index("idx_gpu_sample_uuid_ts", GpuSample.uuid, GpuSample.ts_ms)


class Aggregate1m(Base):
    __tablename__ = "aggregate_1m"

    bucket_ms: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    system_w_avg: Mapped[float | None] = mapped_column(Float)
    system_w_max: Mapped[float | None] = mapped_column(Float)
    system_quality: Mapped[str | None] = mapped_column(String(16))
    system_scope: Mapped[str | None] = mapped_column(String(32))
    cpu_w_avg: Mapped[float | None] = mapped_column(Float)
    gpu_w_avg: Mapped[float | None] = mapped_column(Float)
    energy_wh: Mapped[float | None] = mapped_column(Float)
    energy_quality: Mapped[str | None] = mapped_column(String(16))
    sample_count: Mapped[int] = mapped_column(Integer, default=0)
    expected_count: Mapped[int] = mapped_column(Integer, default=0)


class Aggregate1h(Base):
    __tablename__ = "aggregate_1h"

    bucket_ms: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    system_w_avg: Mapped[float | None] = mapped_column(Float)
    system_w_max: Mapped[float | None] = mapped_column(Float)
    system_quality: Mapped[str | None] = mapped_column(String(16))
    system_scope: Mapped[str | None] = mapped_column(String(32))
    cpu_w_avg: Mapped[float | None] = mapped_column(Float)
    gpu_w_avg: Mapped[float | None] = mapped_column(Float)
    energy_wh: Mapped[float | None] = mapped_column(Float)
    energy_quality: Mapped[str | None] = mapped_column(String(16))
    sample_count: Mapped[int] = mapped_column(Integer, default=0)
    expected_count: Mapped[int] = mapped_column(Integer, default=0)


class MeterSample(Base):
    __tablename__ = "meter_sample"

    ts_ms: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    power_w: Mapped[float | None] = mapped_column(Float)
    energy_kwh: Mapped[float | None] = mapped_column(Float)
    voltage_v: Mapped[float | None] = mapped_column(Float)
    current_a: Mapped[float | None] = mapped_column(Float)
    power_factor: Mapped[float | None] = mapped_column(Float)
    source: Mapped[str | None] = mapped_column(Text)


class EventLog(Base):
    __tablename__ = "event_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ts_ms: Mapped[int] = mapped_column(BigInteger, index=True)
    level: Mapped[str] = mapped_column(String(16))
    component: Mapped[str] = mapped_column(String(64))
    message: Mapped[str] = mapped_column(Text)


class SchemaMeta(Base):
    __tablename__ = "schema_meta"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str] = mapped_column(Text)


__all__ = [
    "Aggregate1h",
    "Aggregate1m",
    "Base",
    "EventLog",
    "GpuSample",
    "MeterSample",
    "SCHEMA_VERSION",
    "Sample",
    "SchemaMeta",
]
