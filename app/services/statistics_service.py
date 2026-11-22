"""Roll raw samples up into one-minute and one-hour buckets.

Aggregation is idempotent (``INSERT ... ON CONFLICT DO UPDATE``) and always
reprocesses a couple of recent buckets, so a restart or a late sample cannot
lose or double count data.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime, timedelta
from typing import Callable

from app.config import AppConfig
from app.database.database import AppDatabase, DatabaseUnavailable

logger = logging.getLogger(__name__)

MINUTE_MS = 60_000
HOUR_MS = 3_600_000


def _energy_quality(energy_wh: float | None, missing: int, estimated: int) -> str:
    if energy_wh is None:
        return "unavailable"
    return "estimated" if (missing or estimated) else "measured"


class StatisticsService:
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
        self.expected_per_minute = max(
            1, int(round(60.0 / max(cfg.database.write_interval, 0.1)))
        )
        self._last_minute_bucket: int | None = None
        self._last_hour_bucket: int | None = None
        self._task: asyncio.Task[None] | None = None

    # ------------------------------------------------------------------ lifecycle

    async def start(self) -> None:
        try:
            await asyncio.to_thread(self.run_once)
        except DatabaseUnavailable as exc:
            logger.warning("initial aggregation skipped: %s", exc)
        if self._task is None:
            self._task = asyncio.create_task(self._run(), name="statistics")

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
            self._task = None

    async def _run(self) -> None:
        while True:
            now = self._now()
            next_run = (now + timedelta(minutes=1)).replace(second=5, microsecond=0)
            await asyncio.sleep(max(0.0, (next_run - now).total_seconds()))
            try:
                await asyncio.to_thread(self.run_once)
            except DatabaseUnavailable as exc:
                logger.warning("aggregation skipped, database unavailable: %s", exc)
            except Exception:  # noqa: BLE001
                logger.exception("aggregation failed")

    # ------------------------------------------------------------------ work

    def run_once(self, at: datetime | None = None) -> dict[str, int]:
        repository = self.database.ensure_open()
        now = at or self._now()
        now_ms = int(now.timestamp() * 1000)
        current_minute = now_ms // MINUTE_MS * MINUTE_MS
        current_hour = now_ms // HOUR_MS * HOUR_MS

        minute_since = (
            self._last_minute_bucket
            if self._last_minute_bucket is not None
            else current_minute - self.cfg.history.raw_retention_days * 86_400_000
        )
        if self._last_minute_bucket is not None:
            minute_since = min(minute_since, current_minute - 2 * MINUTE_MS)
        minute_since = max(minute_since, 0)

        raw_rows = repository.minute_buckets_from_raw(minute_since, current_minute)
        minute_records = [
            {
                "bucket_ms": int(row["bucket_ms"]),
                "system_w_avg": row["system_w_avg"],
                "system_w_max": row["system_w_max"],
                "system_quality": row["system_quality"],
                "system_scope": row["system_scope"],
                "cpu_w_avg": row["cpu_w_avg"],
                "gpu_w_avg": row["gpu_w_avg"],
                "energy_wh": row["energy_wh"],
                "energy_quality": _energy_quality(
                    row["energy_wh"], int(row["missing_energy"] or 0), int(row["estimated_energy"] or 0)
                ),
                "sample_count": int(row["sample_count"] or 0),
                "expected_count": self.expected_per_minute,
            }
            for row in raw_rows
        ]
        repository.upsert_month_rows(minute_records)
        self._last_minute_bucket = current_minute

        hour_since = (
            self._last_hour_bucket
            if self._last_hour_bucket is not None
            else current_hour - self.cfg.history.minute_retention_days * 86_400_000
        )
        if self._last_hour_bucket is not None:
            hour_since = min(hour_since, current_hour - 2 * HOUR_MS)
        hour_since = max(hour_since, 0)

        minute_buckets = repository.hour_buckets_from_minutes(hour_since, current_hour)
        hour_records = [
            {
                "bucket_ms": int(row["bucket_ms"]),
                "system_w_avg": row["system_w_avg"],
                "system_w_max": row["system_w_max"],
                "system_quality": row["system_quality"],
                "system_scope": row["system_scope"],
                "cpu_w_avg": row["cpu_w_avg"],
                "gpu_w_avg": row["gpu_w_avg"],
                "energy_wh": row["energy_wh"],
                "energy_quality": _energy_quality(
                    row["energy_wh"], int(row["missing_energy"] or 0), int(row["estimated_energy"] or 0)
                ),
                "sample_count": int(row["sample_count"] or 0),
                "expected_count": int(row["expected_count"] or 0),
            }
            for row in minute_buckets
        ]
        repository.upsert_hour_rows(hour_records)
        self._last_hour_bucket = current_hour

        return {"minutes": len(minute_records), "hours": len(hour_records)}
