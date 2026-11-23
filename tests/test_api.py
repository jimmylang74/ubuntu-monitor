from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.config import (
    AppConfig,
    DatabaseConfig,
    ExternalMeterConfig,
    PowerConfig,
    ShellyConfig,
)
from app.domain import measured, unavailable
from app.main import create_app
from app.power.adapters.base import MeterCapabilities, MeterReading
from app.power.calculator import MeterState
from app.runtime import Runtime
from tests.fakes import cpu_snapshot, gpu_snapshot, memory_snapshot

NOW_MS = int(datetime.now(UTC).timestamp() * 1000)


def seed(
    runtime: Runtime,
    *,
    gpu_available: bool = True,
    gpus=None,
    gpu_error: str | None = None,
) -> None:
    if gpus is None:
        gpus = (gpu_snapshot(power_w=50.0),)
    runtime.store.update("cpu", cpu_snapshot(100.0))
    runtime.store.update("gpu", tuple(gpus))
    runtime.store.update("gpu_available", gpu_available)
    runtime.store.update("gpu_error", gpu_error)
    runtime.store.update("memory", memory_snapshot())
    runtime.store.update("disk", ())
    runtime.store.update("network", ())
    runtime.store.update("host", {"hostname": "test-host", "kernel": "test"})
    snapshot = runtime.service.build_snapshot()
    runtime.store.update("snapshot", snapshot)


@pytest.fixture
def client(tmp_path: Path):
    cfg = AppConfig(
        database=DatabaseConfig(path=str(tmp_path / "api.db")),
        power=PowerConfig(estimated_other_power_w=150.0),
    )
    app = create_app(cfg, start_background=False)
    with TestClient(app) as test_client:
        seed(app.state.runtime)
        yield test_client, app.state.runtime


def test_root_and_health(client) -> None:
    http, _ = client

    root = http.get("/")
    assert root.status_code == 200
    assert root.headers["content-type"].startswith("text/html")
    assert 'href="/"' in root.text
    assert "Power Monitor" in root.text

    api = http.get("/api")
    assert api.status_code == 200
    assert api.json()["name"] == "ubuntu-power-monitor"
    assert api.json()["websocket"] == "/ws/realtime"
    assert api.json()["ui"] == "/"

    health = http.get("/health")
    assert health.status_code == 200
    payload = health.json()
    assert payload["status"] == "ok"
    assert payload["database"]["database"]["state"] == "ok"
    assert payload["uptime_s"] >= 0


def test_system_cpu_gpu_endpoints(client) -> None:
    http, runtime = client

    system = http.get("/api/v1/system")
    assert system.status_code == 200
    assert system.json()["host"]["hostname"] == "test-host"
    assert system.json()["gpu"]["available"] is True

    cpu = http.get("/api/v1/cpu")
    assert cpu.status_code == 200
    assert cpu.json()["cpu"]["total_power"]["value"] == pytest.approx(100.0)
    assert cpu.json()["cpu"]["packages"][0]["package_id"] == 0

    gpu = http.get("/api/v1/gpu")
    assert gpu.status_code == 200
    assert gpu.json()["gpus"][0]["uuid"] == "GPU-1"
    assert gpu.json()["gpus"][0]["fan_speed"]["quality"] == "unavailable"


def test_power_current_reports_provenance(client) -> None:
    http, _ = client
    payload = http.get("/api/v1/power/current").json()

    system = payload["system"]
    assert system["value"] == pytest.approx(300.0)
    assert system["unit"] == "W"
    assert system["quality"] == "estimated"
    assert system["scope"] == "dc_components"
    assert system["mode"] == "estimated"
    assert system["source"] == "calculator:components"
    assert system["other_components_w"] == pytest.approx(150.0)
    assert system["other_power_source"] == "config"
    assert payload["cpu"]["value"] == pytest.approx(100.0)
    assert payload["gpu"][0]["power"]["value"] == pytest.approx(50.0)
    # One sample is not enough to integrate energy.
    assert payload["energy"]["value"] is None
    assert payload["energy"]["quality"] == "unavailable"


def test_meter_endpoint_and_wall_power(tmp_path: Path) -> None:
    cfg = AppConfig(
        database=DatabaseConfig(path=str(tmp_path / "meter.db")),
        power=PowerConfig(
            external_meter=ExternalMeterConfig(
                enabled=True,
                type="shelly",
                failover_after=3,
                shelly=ShellyConfig(url="http://meter"),
            )
        ),
    )
    app = create_app(cfg, start_background=False)
    with TestClient(app) as http:
        runtime = app.state.runtime
        seed(runtime)
        timestamp = datetime.now(UTC)
        reading = MeterReading(
            timestamp=timestamp,
            power=measured(450.0, "W", "shelly:http://meter", timestamp),
            energy_total=measured(12.5, "kWh", "shelly:http://meter", timestamp),
            voltage=unavailable("V", "shelly:http://meter", timestamp),
            current=unavailable("A", "shelly:http://meter", timestamp),
            power_factor=unavailable("", "shelly:http://meter", timestamp),
            source="shelly:http://meter",
        )
        runtime.meter.last_reading = reading
        runtime.meter.last_success = timestamp
        runtime.meter.capabilities = MeterCapabilities(adapter_id="shelly:gen2", supports_energy=True)
        runtime.meter.state = "ok"
        runtime.meter.failures = 0
        runtime.store.update("meter", reading)
        runtime.store.update(
            "meter_state",
            MeterState(enabled=True, healthy=True, last_success=timestamp, status="ok"),
        )
        runtime.store.update("meter_status", runtime.meter.status())
        runtime.store.update("snapshot", runtime.service.build_snapshot())

        current = http.get("/api/v1/power/current").json()
        assert current["system"]["value"] == pytest.approx(450.0)
        assert current["system"]["quality"] == "measured"
        assert current["system"]["scope"] == "wall"
        assert current["system"]["source"] == "shelly:http://meter"
        assert current["meter"]["state"] == "ok"

        meter = http.get("/api/v1/meter").json()
        assert meter["meter"]["enabled"] is True
        assert meter["meter"]["scope"] == "wall"
        assert meter["meter"]["energy_source"] == "meter"
        assert meter["meter"]["reading"]["power"]["value"] == pytest.approx(450.0)
        assert meter["system_meter_status"] == "ok"
        assert meter["last_meter_at"] is not None

        health = http.get("/health").json()
        assert health["meter"]["enabled"] is True
        assert health["meter"]["state"] == "ok"


def test_power_history_endpoint_and_validation(client) -> None:
    http, runtime = client
    repository = runtime.database.ensure_open()
    repository.upsert_sample(
        {
            "ts_ms": NOW_MS - 1000,
            "system_w": 300.0,
            "system_quality": "estimated",
            "system_source": "calculator:components",
            "system_scope": "dc_components",
            "cpu_w": 100.0,
            "cpu_quality": "measured",
            "gpu_w": 50.0,
            "gpu_quality": "measured",
        },
        [{"ts_ms": NOW_MS - 1000, "uuid": "GPU-1", "power_w": 50.0}],
    )

    history = http.get("/api/v1/power/history", params={"window": "5m"})
    assert history.status_code == 200
    payload = history.json()
    assert payload["window"] == "5m"
    assert payload["source"] == "raw"
    assert {series["id"] for series in payload["series"]} == {"system", "cpu", "gpu_total"}
    assert all(series["points"] for series in payload["series"])

    invalid_window = http.get("/api/v1/power/history", params={"window": "2h"})
    assert invalid_window.status_code == 400
    assert "window" in invalid_window.json()["detail"]

    invalid_series = http.get("/api/v1/power/history", params={"series": "system,banana"})
    assert invalid_series.status_code == 400

    per_gpu = http.get(
        "/api/v1/power/history", params={"window": "1h", "series": "gpu:GPU-1"}
    )
    assert per_gpu.status_code == 200
    assert per_gpu.json()["series"][0]["id"] == "gpu:GPU-1"


def test_energy_endpoints(client) -> None:
    http, _ = client

    daily = http.get("/api/v1/energy/daily", params={"days": 2})
    assert daily.status_code == 200
    assert len(daily.json()["days"]) == 2
    assert daily.json()["timezone"]

    summary = http.get("/api/v1/energy/summary")
    assert summary.status_code == 200
    assert set(summary.json()) >= {
        "today",
        "yesterday",
        "last_7_days",
        "last_30_days",
        "currency",
        "timezone",
    }

    weekly = http.get("/api/v1/energy/weekly")
    assert weekly.status_code == 200
    assert weekly.json()["window"] == "7d"

    monthly = http.get("/api/v1/energy/monthly")
    assert monthly.status_code == 200
    assert monthly.json()["window"] == "30d"


def test_config_and_diagnostics(client) -> None:
    http, _ = client

    config = http.get("/api/v1/config")
    assert config.status_code == 200
    assert config.json()["server"]["port"] == 8010
    assert config.json()["power"]["estimated_other_power_w"] == pytest.approx(150.0)
    assert config.json()["power"]["last_suggestion"] is None
    assert config.json()["gpu"]["backends"] == ["nvidia", "amdgpu"]
    assert config.json()["gpu"]["amdgpu"]["apu_policy"] == "auto"
    assert config.json()["rapl"]["control_types"] == ["intel-rapl"]

    diagnostics = http.get("/api/v1/diagnostics")
    assert diagnostics.status_code == 200
    payload = diagnostics.json()
    assert "rapl" in payload
    assert "gpu" in payload
    assert payload["gpu"]["amdgpu_initialized"] is True
    assert payload["other_power"]["configured_w"] == pytest.approx(150.0)
    assert "collectors" in payload
    assert payload["database"]["tables"]["sample"] >= 0


def test_websocket_sends_hello_and_snapshot(client) -> None:
    http, _ = client
    with http.websocket_connect("/ws/realtime") as websocket:
        hello = websocket.receive_json()
        snapshot = websocket.receive_json()

    assert hello == {"type": "hello", "protocol": 1, "interval_ms": 1000}
    assert snapshot["type"] == "snapshot"
    assert snapshot["protocol"] == 1
    assert snapshot["system"]["power"]["value"] == pytest.approx(300.0)
    assert snapshot["gpu"][0]["uuid"] == "GPU-1"
    assert snapshot["health"]["gpu"]["state"] in {"starting", "ok", "error"}


def test_gpu_failure_is_reported_as_unavailable(tmp_path: Path) -> None:
    cfg = AppConfig(database=DatabaseConfig(path=str(tmp_path / "gpu-failure.db")))
    app = create_app(cfg, start_background=False)
    with TestClient(app) as http:
        runtime = app.state.runtime
        seed(
            runtime,
            gpu_available=False,
            gpus=(),
            gpu_error="driver/library version mismatch",
        )

        payload = http.get("/api/v1/power/current").json()
        assert payload["system"]["value"] is None
        assert payload["system"]["quality"] == "unavailable"
        assert payload["system"]["missing_components"] == ["gpu"]
        assert payload["system"]["scope"] is None
        assert payload["gpu"] == []

        gpu = http.get("/api/v1/gpu").json()
        assert gpu["available"] is False
        assert gpu["error"] == "driver/library version mismatch"

        ws_snapshot = None
        with http.websocket_connect("/ws/realtime") as websocket:
            websocket.receive_json()
            ws_snapshot = websocket.receive_json()
        assert ws_snapshot["gpu_available"] is False
        assert ws_snapshot["system"]["power"]["value"] is None


def test_database_failure_returns_503(tmp_path: Path) -> None:
    broken = tmp_path / "broken-db"
    broken.mkdir()
    cfg = AppConfig(database=DatabaseConfig(path=str(broken)))
    app = create_app(cfg, start_background=False)
    with TestClient(app) as http:
        seed(app.state.runtime)
        response = http.get("/api/v1/power/history", params={"window": "5m"})
        assert response.status_code == 503
        assert "database unavailable" in response.json()["detail"]
