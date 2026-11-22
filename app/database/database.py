"""SQLite lifecycle: open lazily, never crash the service when the disk fails."""

from __future__ import annotations

import threading
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from app.config import DatabaseConfig
from app.database.repository import Repository, create_engine_for


class DatabaseUnavailable(RuntimeError):
    pass


class AppDatabase:
    RETRY_INTERVAL_S = 5.0

    def __init__(self, cfg: DatabaseConfig, path: Path) -> None:
        self.cfg = cfg
        self.path = path
        self.repo: Repository | None = None
        self.error: str | None = None
        self._lock = threading.Lock()
        self._last_attempt = float("-inf")
        self._opened_at: datetime | None = None

    # ------------------------------------------------------------------ open

    def ensure_open(self) -> Repository:
        with self._lock:
            if self.repo is not None:
                return self.repo
            now = time.monotonic()
            if self.error is not None and now - self._last_attempt < self.RETRY_INTERVAL_S:
                raise DatabaseUnavailable(self.error)
            self._last_attempt = now
            try:
                engine = create_engine_for(self.path, self.cfg.busy_timeout_ms, self.cfg.wal)
                repository = Repository(engine)
                repository.ensure_schema()
                repository.ping()
            except Exception as exc:  # noqa: BLE001 - disk errors vary widely
                self.error = f"{exc.__class__.__name__}: {exc}"
                self.repo = None
                raise DatabaseUnavailable(self.error) from exc
            self.repo = repository
            self.error = None
            self._opened_at = datetime.now(UTC)
            return repository

    def close(self) -> None:
        with self._lock:
            if self.repo is not None:
                self.repo.engine.dispose()
            self.repo = None

    # ------------------------------------------------------------------ write

    def write_snapshot(
        self, record: dict[str, Any], gpu_rows: list[dict[str, Any]]
    ) -> None:
        self.ensure_open().upsert_sample(record, gpu_rows)

    def write_meter_sample(self, row: dict[str, Any]) -> None:
        self.ensure_open().upsert_meter_sample(row)

    def write_event(self, level: str, component: str, message: str) -> None:
        try:
            self.ensure_open().write_event(level, component, message, datetime.now(UTC))
        except DatabaseUnavailable:
            pass

    # ------------------------------------------------------------------ status

    def status(self) -> dict[str, Any]:
        return {
            "state": "ok" if self.repo is not None else ("degraded" if self.error else "starting"),
            "path": str(self.path),
            "wal": self.cfg.wal,
            "error": self.error,
            "opened_at": self._opened_at.isoformat() if self._opened_at else None,
        }

    def table_counts(self) -> dict[str, int]:
        return self.ensure_open().table_counts()
