"""FastAPI application assembly and the ``power-monitor-web`` entry point."""

from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager
from logging.handlers import RotatingFileHandler
from typing import AsyncIterator

from fastapi import FastAPI

from app import __version__
from app.api.routes import root_router, router
from app.api.websocket import router as websocket_router
from app.config import AppConfig, ServerConfig, load_config, resolve_data_path
from app.database.database import DatabaseUnavailable
from app.runtime import build_runtime
from app.web import mount_static, router as web_router

logger = logging.getLogger(__name__)

CONFIG_ENV = "PM_CONFIG"


def create_app(config: AppConfig | None = None, *, start_background: bool = True) -> FastAPI:
    """Build the ASGI app.

    ``start_background=False`` is used by tests: the runtime is created and the
    database opened, but no collector/aggregation tasks are scheduled.
    """
    cfg = config or load_config(os.environ.get(CONFIG_ENV))

    @asynccontextmanager
    async def lifespan(application: FastAPI) -> AsyncIterator[None]:
        runtime = build_runtime(cfg)
        application.state.runtime = runtime
        if start_background:
            await runtime.start()
        else:
            try:
                runtime.database.ensure_open()
            except DatabaseUnavailable as exc:
                logger.warning("database unavailable: %s", exc)
        try:
            yield
        finally:
            if start_background:
                await runtime.stop()
            else:
                runtime.database.close()

    application = FastAPI(
        title="Ubuntu Power Monitor",
        version=__version__,
        description="Real-time CPU/GPU/system power monitoring with explicit data provenance.",
        lifespan=lifespan,
    )
    application.state.config = cfg
    application.state.runtime = None
    mount_static(application)
    application.include_router(web_router)
    application.include_router(root_router)
    application.include_router(router)
    application.include_router(websocket_router)

    @application.get("/api", include_in_schema=False)
    def api_index() -> dict[str, object]:
        return {
            "name": "ubuntu-power-monitor",
            "version": __version__,
            "status": "ok",
            "base": "/api/v1",
            "websocket": "/ws/realtime",
            "docs": "/docs",
            "ui": "/",
        }

    return application


def configure_logging(server: ServerConfig) -> None:
    """Configure process-wide logging.

    Without ``server.log_file`` logs go to stderr, which systemd forwards to
    the journal.  With a file configured a size-bounded rotating handler is
    used so a long-running service cannot fill the disk.
    """
    level = getattr(logging, server.log_level.upper(), logging.INFO)
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s")
    handlers: list[logging.Handler]
    if server.log_file:
        path = resolve_data_path(server.log_file)
        path.parent.mkdir(parents=True, exist_ok=True)
        handler = RotatingFileHandler(
            path,
            maxBytes=server.log_max_bytes,
            backupCount=server.log_backup_count,
            encoding="utf-8",
        )
        handlers = [handler]
    else:
        handlers = [logging.StreamHandler()]
    for handler in handlers:
        handler.setFormatter(fmt)
    logging.basicConfig(level=level, handlers=handlers, force=True)


def run() -> None:
    import uvicorn

    cfg = load_config(os.environ.get(CONFIG_ENV))
    configure_logging(cfg.server)
    application = create_app(cfg)
    logger.info(
        "starting power monitor on %s:%s (config=%s, db=%s)",
        cfg.server.host,
        cfg.server.port,
        cfg.source_path or "defaults",
        cfg.database_path,
    )
    uvicorn.run(
        application,
        host=cfg.server.host,
        port=cfg.server.port,
        log_level=cfg.server.log_level,
        log_config=None,
        access_log=cfg.server.access_log,
    )


if __name__ == "__main__":
    run()
