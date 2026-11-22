"""Timezone helpers for local-day energy boundaries and scheduled jobs."""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta, tzinfo
from zoneinfo import ZoneInfo


def resolve_timezone(name: str | None) -> tzinfo:
    if not name or name.lower() in {"local", "system"}:
        return datetime.now().astimezone().tzinfo or UTC
    try:
        return ZoneInfo(name)
    except Exception as exc:  # noqa: BLE001 - surface a clear config error
        raise ValueError(f"unknown timezone '{name}'") from exc


def local_day_bounds(day: date, tz: tzinfo) -> tuple[int, int]:
    start = datetime.combine(day, time.min, tzinfo=tz)
    end = datetime.combine(day + timedelta(days=1), time.min, tzinfo=tz)
    return int(start.timestamp() * 1000), int(end.timestamp() * 1000)


def next_local_time(now: datetime, hour: int, minute: int, tz: tzinfo) -> datetime:
    local = now.astimezone(tz)
    candidate = local.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if candidate <= local:
        candidate += timedelta(days=1)
    return candidate
