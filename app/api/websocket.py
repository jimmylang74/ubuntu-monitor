"""Realtime WebSocket: one complete snapshot per collection tick."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from app.runtime import Runtime
from app.state import Subscription

logger = logging.getLogger(__name__)

router = APIRouter()

HEARTBEAT_S = 20.0


async def _send_json(websocket: WebSocket, payload: dict[str, Any]) -> bool:
    try:
        await websocket.send_json(payload)
        return True
    except (WebSocketDisconnect, RuntimeError, OSError):
        return False


@router.websocket("/ws/realtime")
async def realtime(websocket: WebSocket) -> None:
    await websocket.accept()
    runtime: Runtime = websocket.app.state.runtime
    subscription: Subscription = runtime.bus.subscribe()

    try:
        if not await _send_json(
            websocket,
            {
                "type": "hello",
                "protocol": 1,
                "interval_ms": int(runtime.config.collector.system_interval * 1000),
            },
        ):
            return

        snapshot = runtime.store.get("snapshot")
        if snapshot is not None and not await _send_json(websocket, snapshot.to_dict()):
            return

        while True:
            try:
                item = await asyncio.wait_for(subscription.queue.get(), timeout=HEARTBEAT_S)
            except TimeoutError:
                if not await _send_json(
                    websocket,
                    {"type": "ping", "timestamp": runtime.current_snapshot().timestamp.isoformat()},
                ):
                    return
                continue
            if not await _send_json(websocket, item):
                return
    except WebSocketDisconnect:
        pass
    except Exception:  # noqa: BLE001 - a broken client must not affect others
        logger.debug("websocket client error", exc_info=True)
    finally:
        runtime.bus.unsubscribe(subscription)
