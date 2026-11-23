from __future__ import annotations

import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.config import AppConfig, DatabaseConfig
from app.main import create_app

PAGES = {
    "/": "/static/assets/pages/dashboard.js",
    "/cpu": "/static/assets/pages/cpu.js",
    "/gpu": "/static/assets/pages/gpu.js",
    "/energy": "/static/assets/pages/energy.js",
}

STATIC_ASSETS = [
    "/static/assets/app.css",
    "/static/assets/core.js",
    "/static/assets/pages/dashboard.js",
    "/static/assets/pages/cpu.js",
    "/static/assets/pages/gpu.js",
    "/static/assets/pages/energy.js",
    "/static/vendor/echarts.min.js",
    "/static/favicon.svg",
]

SCRIPT_SRC = re.compile(r'(?:src|href)="(/(?:static|api)[^"]*)"')


@pytest.fixture
def http(tmp_path: Path):
    cfg = AppConfig(database=DatabaseConfig(path=str(tmp_path / "web.db")))
    app = create_app(cfg, start_background=False)
    with TestClient(app) as client:
        yield client


def test_pages_render_shared_shell(http: TestClient) -> None:
    for path, script in PAGES.items():
        response = http.get(path)
        assert response.status_code == 200, path
        assert response.headers["content-type"].startswith("text/html"), path
        body = response.text
        assert "<title>" in body
        assert 'class="nav"' in body
        assert "Power Monitor" in body
        assert f'src="{script}?v=' in body
        assert 'src="/static/vendor/echarts.min.js?v=' in body


def test_pages_do_not_reference_cdn(http: TestClient) -> None:
    for path in PAGES:
        body = http.get(path).text
        lowered = body.lower()
        assert "cdn." not in lowered, path
        assert "unpkg" not in lowered, path
        assert "jsdelivr" not in lowered, path
        assert not re.search(r'(?:src|href)="https?://', body), path


def test_every_page_asset_resolves(http: TestClient) -> None:
    seen: set[str] = set()
    for path in PAGES:
        body = http.get(path).text
        for asset in SCRIPT_SRC.findall(body):
            if asset.startswith("/api"):
                continue
            seen.add(asset)
            response = http.get(asset)
            assert response.status_code == 200, asset
    assert any(asset.startswith("/static/assets/app.css?v=") for asset in seen)
    assert any(asset.startswith("/static/vendor/echarts.min.js?v=") for asset in seen)


def test_static_asset_urls_are_versioned(http: TestClient) -> None:
    for path in PAGES:
        body = http.get(path).text
        assets = [asset for asset in SCRIPT_SRC.findall(body) if asset.startswith("/static")]
        assert assets, path
        assert all("?v=" in asset for asset in assets), path


@pytest.mark.parametrize("asset", STATIC_ASSETS)
def test_static_assets_are_served(http: TestClient, asset: str) -> None:
    response = http.get(asset)
    assert response.status_code == 200, asset
    assert response.content
    assert response.headers.get("cache-control") == "no-cache"


def test_echarts_is_vendored_with_license() -> None:
    static = Path(__file__).resolve().parents[1] / "app" / "static"
    bundle = static / "vendor" / "echarts.min.js"
    license_file = static / "vendor" / "ECHARTS-LICENSE.txt"
    assert bundle.is_file() and bundle.stat().st_size > 500_000
    assert license_file.is_file()
    assert "Apache" in license_file.read_text(encoding="utf-8")


def test_favicon_route_serves_svg(http: TestClient) -> None:
    response = http.get("/favicon.ico")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("image/svg+xml")
    assert b"<svg" in response.content


def test_api_metadata_describes_ui(http: TestClient) -> None:
    payload = http.get("/api").json()
    assert payload["ui"] == "/"
    assert payload["base"] == "/api/v1"
    assert payload["websocket"] == "/ws/realtime"
    assert payload["docs"] == "/docs"
