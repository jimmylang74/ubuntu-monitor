"""Daily retention: delete old raw samples and aggregates in small batches."""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime, timedelta
from typing import Callable

from app.config import AppConfig
from app.database.database import AppDatabase, DatabaseUnavailable
from app.timeutil import resolve_timezone

logger = logging.getLogger(__name__)

BATCH_SIZE = 10_000


class RetentionService:
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
        self._task: asyncio.Task[None] | None = None

    # ------------------------------------------------------------------ lifecycle

    async def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._run(), name="retention")

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
            self._task = None

    async def _run(self) -> None:
        tz = resolve_timezone(self.cfg.server.timezone)
        while True:
            now = self._now()
            local = now.astimezone(tz)
            next_run = local.replace(
                hour=self.cfg.history.cleanup_hour, minute=0, second=0, microsecond=0
            )
            if next_run <= local:
                next_run += timedelta(days=1)
            await asyncio.sleep(max(0.0, (next_run - local).total_seconds()))
            try:
                counts = await self.run_once()
                if any(counts.values()):
                    logger.info("retention removed rows: %s", counts)
            except DatabaseUnavailable as exc:
                logger.warning("retention skipped, database unavailable: %s", exc)
            except Exception:  # noqa: BLE001
                logger.exception("retention failed")
            await asyncio.sleep(60)

    # ------------------------------------------------------------------ work

    async def run_once(self, at: datetime | None = None) -> dict[str, int]:
        repository = self.database.ensure_open()
        now = at or self._now()
        now_ms = int(now.timestamp() * 1000)
        cutoffs = {
            "sample": now_ms - self.cfg.history.raw_retention_days * 86_400_000,
            "gpu_sample": now_ms - self.cfg.history.raw_retention_days * 86_400_000,
            "meter_sample": now_ms - self.cfg.history.raw_retention_days * 86_400_000,
            "aggregate_1m": now_ms - self.cfg.history.minute_retention_days * 86_400_000,
            "aggregate_1h": now_ms - self.cfg.history.hourly_retention_days * 86_400_000,
        }

        counts: dict[str, int] = {}
        for table, cutoff in cutoffs.items():
            removed = 0
            while True:
                batch = await asyncio.to_thread(
                    repository.delete_before, table, cutoff, BATCH_SIZE
                )
                removed += batch
                if batch < BATCH_SIZE:
                    break
                await asyncio.sleep(0)
            counts[table] = removed
        return counts
