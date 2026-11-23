"""Web UI routes and static asset serving.

The UI is plain HTML/CSS/ES modules with no build step.  ECharts is vendored
under ``app/static/vendor`` so the dashboard also works on an isolated LAN.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

from fastapi import APIRouter, FastAPI, Request
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from app import __version__

APP_DIR = Path(__file__).resolve().parent
STATIC_DIR = APP_DIR / "static"
TEMPLATE_DIR = APP_DIR / "templates"

router = APIRouter(include_in_schema=False)
templates = Jinja2Templates(directory=str(TEMPLATE_DIR))


class _NoCacheStaticFiles(StaticFiles):
    """Always revalidate assets so a restart picks up changed JS/CSS."""

    async def get_response(self, path, scope):
        response = await super().get_response(path, scope)
        response.headers["Cache-Control"] = "no-cache"
        return response


def _asset_version() -> str:
    """Fingerprint static files so their URLs change whenever they are edited."""
    digest = hashlib.sha256()
    for path in sorted(STATIC_DIR.rglob("*")):
        if not path.is_file():
            continue
        stat = path.stat()
        digest.update(str(path.relative_to(STATIC_DIR)).encode())
        digest.update(str(stat.st_mtime_ns).encode())
        digest.update(str(stat.st_size).encode())
    return digest.hexdigest()[:12]


def _context(request: Request, page: str, title: str) -> dict[str, object]:
    return {
        "request": request,
        "page": page,
        "title": title,
        "version": __version__,
        "asset_version": _asset_version(),
    }


@router.get("/", response_class=HTMLResponse)
def dashboard(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(request, "index.html", _context(request, "dashboard", "总览"))


@router.get("/cpu", response_class=HTMLResponse)
def cpu_page(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(request, "cpu.html", _context(request, "cpu", "CPU"))


@router.get("/gpu", response_class=HTMLResponse)
def gpu_page(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(request, "gpu.html", _context(request, "gpu", "GPU"))


@router.get("/energy", response_class=HTMLResponse)
def energy_page(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(request, "energy.html", _context(request, "energy", "能耗"))


@router.get("/favicon.ico", include_in_schema=False)
def favicon() -> FileResponse:
    return FileResponse(STATIC_DIR / "favicon.svg", media_type="image/svg+xml")


def mount_static(app: FastAPI) -> None:
    """Mount ``/static`` on the FastAPI application."""

    app.mount("/static", _NoCacheStaticFiles(directory=STATIC_DIR), name="static")
