"""REST endpoints.  This layer only reads StateStore / repository / service."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request

from app import __version__
from app.database.database import DatabaseUnavailable
from app.runtime import Runtime
from app.services.history_service import parse_series

router = APIRouter(prefix="/api/v1")
root_router = APIRouter()


def _runtime(request: Request) -> Runtime:
    return request.app.state.runtime


def _db_error(exc: Exception) -> HTTPException:
    return HTTPException(status_code=503, detail=f"database unavailable: {exc}")


@root_router.get("/health")
def health(request: Request) -> dict[str, Any]:
    runtime = _runtime(request)
    database = runtime.writer.status()
    collectors = runtime.service.health_dict()
    meter = runtime.meter.status()
    failing = [
        name for name, state in collectors.items() if state.get("state") in {"error", "stale"}
    ]
    if meter.get("enabled") and meter.get("state") in {"offline", "error"}:
        failing.append("meter")
    degraded = database.get("state") == "degraded" or bool(failing)
    return {
        "status": "degraded" if degraded else "ok",
        "time": datetime.now(UTC).isoformat(),
        "uptime_s": max(0.0, (datetime.now(UTC) - runtime.started_at).total_seconds()),
        "database": database,
        "meter": meter,
        "failing_collectors": failing,
    }


@router.get("/system")
def system(request: Request) -> dict[str, Any]:
    runtime = _runtime(request)
    snapshot = runtime.current_snapshot()
    return {
        "timestamp": snapshot.timestamp.isoformat(),
        "started_at": runtime.started_at.isoformat(),
        "version": __version__,
        "host": snapshot.host,
        "gpu": {"available": snapshot.gpu_available, "error": snapshot.gpu_error},
        "health": runtime.health(),
    }


@router.get("/cpu")
def cpu(request: Request) -> dict[str, Any]:
    snapshot = _runtime(request).current_snapshot()
    payload = snapshot.to_dict()
    return {"timestamp": payload["timestamp"], "cpu": payload["cpu"]}


@router.get("/gpu")
def gpu(request: Request) -> dict[str, Any]:
    snapshot = _runtime(request).current_snapshot()
    payload = snapshot.to_dict()
    return {
        "timestamp": payload["timestamp"],
        "available": snapshot.gpu_available,
        "error": snapshot.gpu_error,
        "gpus": payload["gpu"],
    }


@router.get("/power/current")
def power_current(request: Request) -> dict[str, Any]:
    snapshot = _runtime(request).current_snapshot()
    system_power = snapshot.system_power
    system = system_power.power.to_dict()
    system.update(
        {
            "scope": system_power.scope,
            "mode": system_power.mode,
            "other_components_w": system_power.other_components_w,
            "missing_components": list(system_power.missing_components),
            "meter_status": system_power.meter_status,
            "last_meter_at": (
                system_power.last_meter_at.isoformat() if system_power.last_meter_at else None
            ),
        }
    )
    return {
        "timestamp": snapshot.timestamp.isoformat(),
        "system": system,
        "meter": snapshot.meter,
        "cpu": snapshot.cpu.total_power.to_dict() if snapshot.cpu else None,
        "gpu": [
            {"uuid": item.uuid, "name": item.name, "power": item.power.to_dict()}
            for item in snapshot.gpus
        ],
        "energy": (
            snapshot.energy_wh.to_dict()
            if snapshot.energy_wh is not None
            else {
                "value": None,
                "unit": "Wh",
                "quality": "unavailable",
                "source": "integrated",
                "timestamp": snapshot.timestamp.isoformat(),
                "error": "need two samples to integrate energy",
                "missing": [],
            }
        ),
        "gpu_available": snapshot.gpu_available,
    }


@router.get("/meter")
def meter(request: Request) -> dict[str, Any]:
    runtime = _runtime(request)
    snapshot = runtime.current_snapshot()
    return {
        "timestamp": snapshot.timestamp.isoformat(),
        "meter": snapshot.meter or runtime.meter.status(),
        "system_meter_status": snapshot.system_power.meter_status,
        "last_meter_at": (
            snapshot.system_power.last_meter_at.isoformat()
            if snapshot.system_power.last_meter_at
            else None
        ),
    }


@router.get("/power/history")
def power_history(
    request: Request,
    window: str = Query("5m"),
    series: str | None = Query(None),
) -> dict[str, Any]:
    runtime = _runtime(request)
    try:
        identifiers = parse_series(series)
        return runtime.history.build(window, identifiers)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except DatabaseUnavailable as exc:
        raise _db_error(exc) from exc


@router.get("/energy/daily")
def energy_daily(request: Request, days: int = Query(7, ge=1, le=366)) -> dict[str, Any]:
    try:
        return _runtime(request).energy.daily(days)
    except DatabaseUnavailable as exc:
        raise _db_error(exc) from exc


@router.get("/energy/weekly")
def energy_weekly(request: Request) -> dict[str, Any]:
    try:
        summary = _runtime(request).energy.summary()
    except DatabaseUnavailable as exc:
        raise _db_error(exc) from exc
    return {
        "window": "7d",
        "period": summary["last_7_days"],
        "currency": summary["currency"],
        "price_per_kwh": summary["price_per_kwh"],
        "timezone": summary["timezone"],
    }


@router.get("/energy/monthly")
def energy_monthly(request: Request) -> dict[str, Any]:
    try:
        summary = _runtime(request).energy.summary()
    except DatabaseUnavailable as exc:
        raise _db_error(exc) from exc
    return {
        "window": "30d",
        "period": summary["last_30_days"],
        "currency": summary["currency"],
        "price_per_kwh": summary["price_per_kwh"],
        "timezone": summary["timezone"],
    }


@router.get("/energy/summary")
def energy_summary(request: Request) -> dict[str, Any]:
    try:
        return _runtime(request).energy.summary()
    except DatabaseUnavailable as exc:
        raise _db_error(exc) from exc


@router.get("/config")
def config(request: Request) -> dict[str, Any]:
    cfg = _runtime(request).config
    return {
        "version": __version__,
        "server": {
            "host": cfg.server.host,
            "port": cfg.server.port,
            "timezone": cfg.server.timezone,
        },
        "collector": cfg.collector.model_dump(),
        "gpu": {
            "fallback_nvidia_smi": cfg.gpu.fallback_nvidia_smi,
            "reinit_interval": cfg.gpu.reinit_interval,
        },
        "rapl": {
            "helper": cfg.rapl.helper,
            "use_sudo": cfg.rapl.use_sudo,
            "max_plausible_w": cfg.rapl.max_plausible_w,
        },
        "power": {
            "mode": cfg.power.mode,
            "estimated_other_power_w": cfg.power.estimated_other_power_w,
            "strict_estimate": cfg.power.strict_estimate,
            "external_meter": {
                "enabled": cfg.power.external_meter.enabled,
                "type": cfg.power.external_meter.type,
                "interval": cfg.power.external_meter.interval,
                "failover_after": cfg.power.external_meter.failover_after,
            },
        },
        "electricity": cfg.electricity.model_dump(),
        "history": cfg.history.model_dump(),
    }


@router.get("/diagnostics")
def diagnostics(request: Request) -> dict[str, Any]:
    runtime = _runtime(request)
    payload = runtime.service.diagnostics()
    try:
        payload["database"]["tables"] = runtime.database.table_counts()
    except DatabaseUnavailable as exc:
        payload["database"]["tables"] = None
        payload["database"]["error"] = str(exc)
    return payload
