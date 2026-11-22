"""Daily/weekly/monthly energy totals from persisted samples."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any, Callable

from app.config import AppConfig
from app.database.database import AppDatabase
from app.timeutil import local_day_bounds, resolve_timezone


class EnergyService:
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

    def _totals(self, start_ms: int, end_ms: int) -> dict[str, Any]:
        repository = self.database.ensure_open()
        raw_cutoff_ms = (
            int(self._now().timestamp() * 1000) - self.cfg.history.raw_retention_days * 86_400_000
        )
        result = repository.energy_between(start_ms, end_ms, raw_cutoff_ms)
        energy_wh = result["energy_wh"]
        energy_kwh = None if energy_wh is None else energy_wh / 1000.0
        cost = None
        if energy_kwh is not None and self.cfg.electricity.enabled:
            cost = round(energy_kwh * self.cfg.electricity.price_per_kwh, 4)
        return {
            "energy_wh": energy_wh,
            "energy_kwh": energy_kwh,
            "quality": result["quality"],
            "samples": result["samples"],
            "missing_samples": result["missing"],
            "cost": cost,
        }

    def daily(self, days: int = 7) -> dict[str, Any]:
        if days < 1 or days > 366:
            raise ValueError("days must be between 1 and 366")
        tz = resolve_timezone(self.cfg.server.timezone)
        now_local = self._now().astimezone(tz)
        entries: list[dict[str, Any]] = []
        for offset in range(days - 1, -1, -1):
            day = (now_local - timedelta(days=offset)).date()
            start_ms, end_ms = local_day_bounds(day, tz)
            now_ms = int(self._now().timestamp() * 1000)
            totals = self._totals(start_ms, min(end_ms, now_ms))
            entries.append({"date": day.isoformat(), **totals})
        return {
            "days": entries,
            "currency": self.cfg.electricity.currency,
            "price_per_kwh": self.cfg.electricity.price_per_kwh,
            "timezone": str(tz),
        }

    def summary(self) -> dict[str, Any]:
        tz = resolve_timezone(self.cfg.server.timezone)
        now_local = self._now().astimezone(tz)
        today = now_local.date()
        now_ms = int(self._now().timestamp() * 1000)

        def period(first_day_offset: int, last_day_offset: int) -> dict[str, Any]:
            start_ms, _ = local_day_bounds(today - timedelta(days=first_day_offset), tz)
            _, end_ms = local_day_bounds(today - timedelta(days=last_day_offset), tz)
            return self._totals(start_ms, min(end_ms, now_ms))

        return {
            "today": period(0, 0),
            "yesterday": period(1, 1),
            "last_7_days": period(6, 0),
            "last_30_days": period(29, 0),
            "currency": self.cfg.electricity.currency,
            "price_per_kwh": self.cfg.electricity.price_per_kwh,
            "timezone": str(tz),
        }
