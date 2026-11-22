"""Wires configuration, storage, services and API together for one process."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from app.config import AppConfig
from app.database.database import AppDatabase, DatabaseUnavailable
from app.database.writer import DatabaseWriter
from app.services.collector_service import CollectorService
from app.services.energy_service import EnergyService
from app.services.history_service import HistoryService
from app.services.meter_service import MeterService
from app.services.retention_service import RetentionService
from app.services.statistics_service import StatisticsService
from app.snapshot import Snapshot
from app.state import EventBus, StateStore

logger = logging.getLogger(__name__)


@dataclass
class Runtime:
    config: AppConfig
    database: AppDatabase
    writer: DatabaseWriter
    store: StateStore
    bus: EventBus
    service: CollectorService
    meter: MeterService
    statistics: StatisticsService
    retention: RetentionService
    history: HistoryService
    energy: EnergyService
    started_at: datetime

    # ------------------------------------------------------------------ lifecycle

    async def start(self) -> None:
        try:
            self.database.ensure_open()
        except DatabaseUnavailable as exc:
            logger.warning("database unavailable at startup; serving degraded: %s", exc)
        await self.writer.start()
        await self.service.start()
        await self.meter.start()
        await self.statistics.start()
        await self.retention.start()

    async def stop(self) -> None:
        await self.retention.stop()
        await self.statistics.stop()
        await self.meter.stop()
        await self.service.stop()
        await self.writer.stop()
        self.database.close()

    # ------------------------------------------------------------------ views

    def current_snapshot(self) -> Snapshot:
        snapshot = self.store.get("snapshot")
        if isinstance(snapshot, Snapshot):
            return snapshot
        return self.service.empty_snapshot()

    def health(self) -> dict[str, Any]:
        return {
            "collectors": self.service.health_dict(),
            "database": self.writer.status(),
        }


def build_runtime(cfg: AppConfig) -> Runtime:
    database = AppDatabase(cfg.database, cfg.database_path)
    writer = DatabaseWriter(database, cfg.database)
    store = StateStore()
    bus = EventBus()
    service = CollectorService(cfg, store, bus, writer, database)
    meter = MeterService(cfg, store, writer)
    return Runtime(
        config=cfg,
        database=database,
        writer=writer,
        store=store,
        bus=bus,
        service=service,
        meter=meter,
        statistics=StatisticsService(cfg, database),
        retention=RetentionService(cfg, database),
        history=HistoryService(cfg, database),
        energy=EnergyService(cfg, database),
        started_at=datetime.now(UTC),
    )
