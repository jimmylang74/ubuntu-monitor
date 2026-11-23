"""Daily/weekly/monthly energy totals from persisted samples."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any, Callable

from app.config import AppConfig
from app.database.database import AppDatabase
from app.timeutil import local_day_bounds, local_month_bounds, resolve_timezone


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

    @staticmethod
    def _empty_totals() -> dict[str, Any]:
        return {
            "energy_wh": None,
            "energy_kwh": None,
            "quality": "unavailable",
            "samples": 0,
            "missing_samples": 0,
            "cost": None,
        }

    def _available_years(self, now_local: datetime, selected_year: int) -> list[int]:
        """Years that may still have hourly energy rows under the retention policy."""
        earliest = now_local - timedelta(days=self.cfg.history.hourly_retention_days)
        years = set(range(earliest.year, now_local.year + 1))
        years.add(selected_year)
        return sorted(years)

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
            "electricity_enabled": self.cfg.electricity.enabled,
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
            "electricity_enabled": self.cfg.electricity.enabled,
            "timezone": str(tz),
        }

    def yearly(self, year: int | None = None) -> dict[str, Any]:
        """Return one entry per calendar month with energy and cost totals."""
        tz = resolve_timezone(self.cfg.server.timezone)
        now_local = self._now().astimezone(tz)
        selected_year = now_local.year if year is None else year
        if not 1970 <= selected_year <= 2100:
            raise ValueError("year must be between 1970 and 2100")

        now_ms = int(self._now().timestamp() * 1000)
        months: list[dict[str, Any]] = []
        for month in range(1, 13):
            start_ms, end_ms = local_month_bounds(selected_year, month, tz)
            if start_ms >= now_ms:
                totals = self._empty_totals()
            else:
                totals = self._totals(start_ms, min(end_ms, now_ms))
            months.append({"month": month, "label": f"{selected_year}-{month:02d}", **totals})

        return {
            "year": selected_year,
            "months": months,
            "years": self._available_years(now_local, selected_year),
            "currency": self.cfg.electricity.currency,
            "price_per_kwh": self.cfg.electricity.price_per_kwh,
            "electricity_enabled": self.cfg.electricity.enabled,
            "timezone": str(tz),
        }
