from __future__ import annotations

import asyncio
import time

import pytest

from app.config import DatabaseConfig
from app.database.writer import DatabaseWriter
from tests.fakes import FakeDatabase


async def _wait_until(predicate, timeout: float = 2.0) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not predicate():
        if loop.time() > deadline:
            raise AssertionError("condition not met before timeout")
        await asyncio.sleep(0.01)


def record(seq: int) -> dict:
    return {"ts_ms": seq, "system_w": 100.0 + seq}


async def test_writer_persists_submitted_records() -> None:
    database = FakeDatabase()
    writer = DatabaseWriter(database, DatabaseConfig(queue_size=10))
    await writer.start()
    try:
        writer.submit(record(1), [])
        writer.submit(record(2), [{"ts_ms": 2, "uuid": "GPU-1"}])
        await _wait_until(lambda: writer.written == 2)

        status = writer.status()
        assert status["state"] == "ok"
        assert status["written"] == 2
        assert status["failed"] == 0
        assert status["queued"] == 0
        assert database.calls == 2
    finally:
        await writer.stop()


async def test_writer_marks_degraded_and_recovers_without_stopping() -> None:
    database = FakeDatabase()
    database.fail = True
    writer = DatabaseWriter(database, DatabaseConfig(queue_size=10))
    await writer.start()
    try:
        writer.submit(record(1), [])
        await _wait_until(lambda: writer.failed == 1)
        assert writer.status()["state"] == "degraded"
        assert "disk on fire" in (writer.last_error or "")

        database.fail = False
        writer.submit(record(2), [])
        await _wait_until(lambda: writer.written == 1)
        status = writer.status()
        assert status["state"] == "ok"
        assert status["last_error"] is None
    finally:
        await writer.stop()


async def test_writer_drops_oldest_sample_when_queue_is_full() -> None:
    database = FakeDatabase()
    writer = DatabaseWriter(database, DatabaseConfig(queue_size=10))

    for seq in range(25):
        writer.submit(record(seq), [])

    assert writer.dropped == 15
    assert writer.status()["queued"] == 10
    # Records 0..14 were dropped, so the oldest queued record is 15.
    await asyncio.sleep(0)
    queued = [writer._queue.get_nowait()[0]["ts_ms"] for _ in range(10)]
    assert queued == list(range(15, 25))


async def test_writer_stop_flushes_pending_items() -> None:
    database = FakeDatabase()
    writer = DatabaseWriter(database, DatabaseConfig(queue_size=50))
    await writer.start()
    for seq in range(20):
        writer.submit(record(seq), [])
    await writer.stop()

    assert database.calls == 20
    assert writer.written == 20
    assert writer.status()["state"] == "stopped"


async def test_writer_submit_after_stop_is_ignored() -> None:
    database = FakeDatabase()
    writer = DatabaseWriter(database, DatabaseConfig(queue_size=10))
    await writer.start()
    await writer.stop()
    writer.submit(record(1), [])
    assert database.calls == 0


async def test_writer_does_not_block_when_database_is_slow() -> None:
    class SlowDatabase(FakeDatabase):
        def write_snapshot(self, record, gpu_rows) -> None:  # type: ignore[override]
            time.sleep(0.05)
            super().write_snapshot(record, gpu_rows)

    database = SlowDatabase()
    writer = DatabaseWriter(database, DatabaseConfig(queue_size=10))
    await writer.start()
    try:
        started = time.monotonic()
        for seq in range(50):
            writer.submit(record(seq), [])
        elapsed = time.monotonic() - started
        assert elapsed < 0.25
        assert writer.dropped > 0
    finally:
        await writer.stop()


@pytest.mark.parametrize("queue_size", [10, 100])
async def test_writer_status_reports_capacity(queue_size: int) -> None:
    writer = DatabaseWriter(FakeDatabase(), DatabaseConfig(queue_size=queue_size))
    assert writer.status()["capacity"] == queue_size
    assert writer.status()["state"] == "starting"
