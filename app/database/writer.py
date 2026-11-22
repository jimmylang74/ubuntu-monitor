"""Bounded, single-writer database task.

Collection and WebSocket delivery must never wait for the disk.  ``submit`` is
non-blocking: when the queue is full the oldest pending sample is dropped and
counted.  Write failures mark the writer degraded but never stop collection.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from typing import Any, Callable

from app.config import DatabaseConfig
from app.database.database import AppDatabase, DatabaseUnavailable

logger = logging.getLogger(__name__)


class DatabaseWriter:
    def __init__(
        self,
        database: AppDatabase,
        cfg: DatabaseConfig,
        *,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self.database = database
        self.cfg = cfg
        self._now = now or (lambda: datetime.now(UTC))
        self._queue: asyncio.Queue[
            tuple[
                dict[str, Any] | None,
                list[dict[str, Any]],
                dict[str, Any] | None,
                tuple[str, str, str] | None,
            ]
            | None
        ] = asyncio.Queue(maxsize=cfg.queue_size)
        self._task: asyncio.Task[None] | None = None
        self._stopping = False

        self.written = 0
        self.dropped = 0
        self.failed = 0
        self.last_error: str | None = None
        self.last_write_at: datetime | None = None
        self.state = "starting"

    # ------------------------------------------------------------------ lifecycle

    async def start(self) -> None:
        if self._task is None:
            self.state = "starting"
            self._task = asyncio.create_task(self._run(), name="database-writer")

    async def stop(self) -> None:
        self._stopping = True
        if self._task is None:
            return
        try:
            await asyncio.wait_for(self._queue.put(None), timeout=5.0)
        except (TimeoutError, asyncio.QueueFull):
            self._task.cancel()
        try:
            await asyncio.wait_for(self._task, timeout=10.0)
        except (TimeoutError, asyncio.CancelledError):
            self._task.cancel()
        self._task = None

    # ------------------------------------------------------------------ submit

    def submit(self, record: dict[str, Any], gpu_rows: list[dict[str, Any]]) -> None:
        self._enqueue((record, gpu_rows, None, None))

    def submit_meter(self, meter_row: dict[str, Any]) -> None:
        """Queue one external-meter sample without waiting for the disk."""
        self._enqueue((None, [], meter_row, None))

    def submit_event(self, level: str, component: str, message: str) -> None:
        """Queue a small event-log row (meter failover/recovery)."""
        self._enqueue((None, [], None, (level, component, message)))

    def _enqueue(
        self,
        item: tuple[
            dict[str, Any] | None,
            list[dict[str, Any]],
            dict[str, Any] | None,
            tuple[str, str, str] | None,
        ],
    ) -> None:
        if self._stopping:
            return
        while True:
            try:
                self._queue.put_nowait(item)
                return
            except asyncio.QueueFull:
                try:
                    self._queue.get_nowait()
                    self._queue.task_done()
                except asyncio.QueueEmpty:
                    pass
                self.dropped += 1
                logger.warning(
                    "database queue full; dropped oldest sample (total dropped=%d)",
                    self.dropped,
                )
                if self.dropped % 100 == 1:
                    self._report_drop()

    # ------------------------------------------------------------------ worker

    def _report_drop(self) -> None:
        """Log the drop to the event table without blocking the event loop."""
        message = f"write queue full, {self.dropped} samples dropped"
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        loop.create_task(
            asyncio.to_thread(self.database.write_event, "warning", "database", message),
            name="database-drop-event",
        )

    async def _run(self) -> None:
        while True:
            item = await self._queue.get()
            try:
                if item is None:
                    self.state = "stopped"
                    return
                record, gpu_rows, meter_row, event = item
                try:
                    if record is not None:
                        await asyncio.to_thread(
                            self.database.write_snapshot, record, gpu_rows
                        )
                    if meter_row is not None:
                        await asyncio.to_thread(
                            self.database.write_meter_sample, meter_row
                        )
                    if event is not None:
                        await asyncio.to_thread(
                            self.database.write_event, event[0], event[1], event[2]
                        )
                except DatabaseUnavailable as exc:
                    self.failed += 1
                    self.last_error = str(exc)
                    self.state = "degraded"
                    logger.warning("database write failed (collector keeps running): %s", exc)
                except Exception as exc:  # noqa: BLE001 - never kill the writer
                    self.failed += 1
                    self.last_error = f"{exc.__class__.__name__}: {exc}"
                    self.state = "degraded"
                    logger.exception("unexpected database write error")
                else:
                    self.written += 1
                    self.last_write_at = self._now()
                    self.last_error = None
                    self.state = "ok"
            finally:
                self._queue.task_done()

    # ------------------------------------------------------------------ status

    def status(self) -> dict[str, Any]:
        return {
            "state": self.state,
            "queued": self._queue.qsize(),
            "capacity": self.cfg.queue_size,
            "written": self.written,
            "dropped": self.dropped,
            "failed": self.failed,
            "last_error": self.last_error,
            "last_write_at": self.last_write_at.isoformat() if self.last_write_at else None,
            "database": self.database.status(),
        }
