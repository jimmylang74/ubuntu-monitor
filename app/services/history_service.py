"""History queries, server-side downsampling and window selection."""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Callable

from app.config import AppConfig
from app.database.database import AppDatabase
from app.database.repository import Repository


@dataclass(frozen=True, slots=True)
class Window:
    seconds: int
    source: str  # raw | aggregate_1m | aggregate_1h


WINDOWS: dict[str, Window] = {
    "5m": Window(300, "raw"),
    "15m": Window(900, "raw"),
    "1h": Window(3600, "raw"),
    "6h": Window(21_600, "aggregate_1m"),
    "24h": Window(86_400, "aggregate_1m"),
    "7d": Window(604_800, "aggregate_1h"),
    "30d": Window(2_592_000, "aggregate_1h"),
    "365d": Window(31_536_000, "aggregate_1h"),
}

_RESOLUTION_LABEL = {1000: "1s", 2000: "2s", 5000: "5s", 10_000: "10s", 60_000: "1m", 3_600_000: "1h"}


def _label(ms: int) -> str:
    if ms in _RESOLUTION_LABEL:
        return _RESOLUTION_LABEL[ms]
    if ms < 60_000:
        return f"{ms // 1000}s"
    if ms < 3_600_000:
        return f"{ms // 60_000}m"
    return f"{ms // 3_600_000}h"


def _bucket_ms(duration_ms: int, max_points: int) -> int:
    if max_points <= 0:
        return 1000
    raw = duration_ms / max_points
    return max(1000, int(math.ceil(raw / 1000.0)) * 1000)


def _downsample(points: list[tuple[int, float | None]], resolution_ms: int) -> list[list[Any]]:
    buckets: dict[int, list[float]] = {}
    for ts_ms, value in points:
        bucket = ts_ms // resolution_ms * resolution_ms
        values = buckets.setdefault(bucket, [])
        if value is not None:
            values.append(float(value))
    return [
        [bucket, (sum(values) / len(values)) if values else None]
        for bucket, values in sorted(buckets.items())
    ]


def parse_series(raw: str | None) -> list[str]:
    if not raw:
        return ["system", "cpu", "gpu_total"]
    identifiers: list[str] = []
    for item in raw.split(","):
        identifier = item.strip()
        if not identifier:
            continue
        if identifier in {"system", "cpu", "gpu_total"} or identifier.startswith("gpu:"):
            identifiers.append(identifier)
        else:
            raise ValueError(
                f"unknown series '{identifier}' (expected system, cpu, gpu_total or gpu:<uuid>)"
            )
    return identifiers or ["system", "cpu", "gpu_total"]


class HistoryService:
    def __init__(
        self,
        cfg: AppConfig,
        database: AppDatabase,
        *,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self.cfg = cfg
        self.database = database
        self._now = now or (lambda: datetime.now(UTC))

    @property
    def repository(self) -> Repository:
        return self.database.ensure_open()

    def build(self, window: str, series_ids: list[str]) -> dict[str, Any]:
        spec = WINDOWS.get(window)
        if spec is None:
            raise ValueError(f"unknown window '{window}' (expected {', '.join(WINDOWS)}))")

        end_ms = int(self._now().timestamp() * 1000)
        start_ms = end_ms - spec.seconds * 1000
        raw_cutoff_ms = end_ms - self.cfg.history.raw_retention_days * 86_400_000

        rows, source = self._rows(spec, start_ms, end_ms, raw_cutoff_ms)
        resolution_ms = _bucket_ms(spec.seconds * 1000, self.cfg.history.max_points)
        if source in {"aggregate_1m", "aggregate_1h"}:
            native = 60_000 if source == "aggregate_1m" else 3_600_000
            resolution_ms = max(native, resolution_ms)

        series: list[dict[str, Any]] = []
        for identifier in series_ids:
            if identifier.startswith("gpu:"):
                uuid = identifier.split(":", 1)[1]
                series.append(self._gpu_series(uuid, start_ms, end_ms, resolution_ms, spec.seconds))
                continue

            timestamp_key = "bucket_ms" if source != "raw" else "ts_ms"
            value_key = {
                "system": "system_w_avg" if source != "raw" else "system_w",
                "cpu": "cpu_w_avg" if source != "raw" else "cpu_w",
                "gpu_total": "gpu_w_avg" if source != "raw" else "gpu_w",
            }[identifier]
            points = [
                (int(row[timestamp_key]), row.get(value_key))
                for row in rows
            ]
            quality = "measured" if identifier != "system" else "estimated"
            scope = "component" if identifier != "system" else "dc_components"
            if identifier == "system":
                values = [row for row in rows if row.get(value_key) is not None]
                if values and all(
                    row.get("system_quality") == "measured" for row in values
                ):
                    quality = "measured"
                    scope = "wall"
            series.append(
                {
                    "id": identifier,
                    "unit": "W",
                    "quality": quality,
                    "scope": scope,
                    "points": _downsample(points, resolution_ms),
                }
            )

        return {
            "window": window,
            "source": source,
            "resolution": _label(resolution_ms),
            "from_ms": start_ms,
            "to_ms": end_ms,
            "series": series,
        }

    def _rows(
        self, spec: Window, start_ms: int, end_ms: int, raw_cutoff_ms: int
    ) -> tuple[list[dict[str, Any]], str]:
        if spec.source == "raw":
            return self.repository.fetch_raw_samples(start_ms, end_ms), "raw"

        if spec.source == "aggregate_1m":
            rows = self.repository.fetch_minute_buckets(start_ms, end_ms)
            if rows:
                return rows, "aggregate_1m"
            if start_ms >= raw_cutoff_ms:
                return self.repository.fetch_raw_samples(start_ms, end_ms), "raw"
            return [], "aggregate_1m"

        rows = self.repository.fetch_hour_buckets(start_ms, end_ms)
        if rows:
            return rows, "aggregate_1h"
        # Aggregation may not have caught up yet (fresh install or restart).
        if start_ms >= raw_cutoff_ms:
            return self.repository.fetch_raw_samples(start_ms, end_ms), "raw"
        minute_rows = self.repository.fetch_minute_buckets(start_ms, end_ms)
        if minute_rows:
            return minute_rows, "aggregate_1m"
        return [], "aggregate_1h"

    def _gpu_series(
        self,
        uuid: str,
        start_ms: int,
        end_ms: int,
        resolution_ms: int,
        window_seconds: int,
    ) -> dict[str, Any]:
        series: dict[str, Any] = {
            "id": f"gpu:{uuid}",
            "unit": "W",
            "quality": "measured",
            "scope": "component",
            "points": [],
        }
        if window_seconds > 86_400:
            series["note"] = "per-GPU history is only available for windows up to 24h"
            return series
        rows = self.repository.fetch_gpu_samples(uuid, start_ms, end_ms)
        points = [(int(row["ts_ms"]), row.get("power_w")) for row in rows]
        series["points"] = _downsample(points, resolution_ms)
        return series
