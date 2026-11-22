#!/usr/bin/env python3
"""Soak/stability monitor for a running power-monitor service.

Checks that the process stays alive, HTTP stays healthy, RSS growth stays
bounded, and the database does not grow without limit.  Intended for the
Phase 5 "1 hour accelerated" run, but accepts any duration.

    .venv/bin/python scripts/stability_check.py --duration 3600
    .venv/bin/python scripts/stability_check.py --duration 60 --interval 2 --pid 1234

Exit code 0 means PASS, 1 means a threshold or availability check failed.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.config import load_config  # noqa: E402


def http_json(url: str, timeout: float = 5.0) -> dict:
    with urllib.request.urlopen(url, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def find_pid(base_url: str) -> int:
    import psutil

    port = urlparse(base_url).port or 80
    try:
        for conn in psutil.net_connections(kind="inet"):
            if conn.status == psutil.CONN_LISTEN and conn.laddr and conn.laddr.port == port:
                if conn.pid:
                    return int(conn.pid)
    except (psutil.Error, OSError):
        pass
    for proc in psutil.process_iter(["pid", "cmdline"]):
        cmdline = " ".join(proc.info.get("cmdline") or [])
        if "app.main" in cmdline:
            return int(proc.info["pid"])
    raise RuntimeError(f"cannot find the process listening on {base_url}")


def rss_mb(pid: int) -> float:
    import psutil

    return psutil.Process(pid).memory_info().rss / 1024 / 1024


def db_size_mb(path: Path) -> float:
    total = 0
    for suffix in ("", "-wal", "-shm"):
        candidate = Path(str(path) + suffix)
        if candidate.is_file():
            total += candidate.stat().st_size
    return total / 1024 / 1024


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Monitor a running power-monitor service")
    parser.add_argument("--base-url", default="http://127.0.0.1:8010")
    parser.add_argument("--duration", type=float, default=3600.0, help="seconds (default 1 hour)")
    parser.add_argument("--interval", type=float, default=10.0, help="sample interval in seconds")
    parser.add_argument("--pid", type=int, default=None)
    parser.add_argument("--db", type=Path, default=None)
    parser.add_argument("--max-rss-growth-mb", type=float, default=64.0)
    parser.add_argument("--max-db-growth-mb", type=float, default=512.0)
    args = parser.parse_args(argv)

    try:
        cfg = load_config()
        pid = args.pid or find_pid(args.base_url)
        db_path = args.db or cfg.database_path
    except (RuntimeError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    base = args.base_url.rstrip("/")
    print(
        f"monitor {base} pid={pid} db={db_path} duration={args.duration:.0f}s "
        f"interval={args.interval:.0f}s"
    )
    print(
        f"thresholds: rss_growth<={args.max_rss_growth_mb:.0f} MiB "
        f"db_growth<={args.max_db_growth_mb:.0f} MiB"
    )
    print(f"{'elapsed':>8} {'rss_mib':>10} {'db_mib':>10} {'health':>10} {'db':>8} {'meter':>8}")

    started = time.monotonic()
    rss_start = rss_now = rss_mb(pid)
    db_start = db_now = db_size_mb(db_path)
    http_failures = 0
    samples = 0
    failure_reason: str | None = None
    next_sample = started

    while True:
        elapsed = time.monotonic() - started
        if elapsed >= args.duration:
            break
        wait = next_sample - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        elapsed = time.monotonic() - started
        next_sample = min(started + args.duration, next_sample + args.interval)
        samples += 1
        try:
            health = http_json(f"{base}/health")
            current = http_json(f"{base}/api/v1/power/current")
            rss_now = rss_mb(pid)
            db_now = db_size_mb(db_path)
            print(
                f"{elapsed:8.0f} {rss_now:10.1f} {db_now:10.1f} "
                f"{health.get('status', '?'):>10} "
                f"{str(health.get('database', {}).get('state', '?')):>8} "
                f"{str(health.get('meter', {}).get('state', '?')):>8}"
            )
            if not isinstance(current.get("system"), dict):
                failure_reason = "power/current payload missing system block"
        except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
            http_failures += 1
            print(f"{elapsed:8.0f}  request failed: {exc}")
            if failure_reason is None:
                failure_reason = f"HTTP failure: {exc}"

    rss_growth = rss_now - rss_start
    db_growth = db_now - db_start
    try:
        alive = rss_mb(pid) >= 0
    except Exception:  # noqa: BLE001 - process disappeared
        alive = False

    result = "PASS"
    if not alive:
        result = "FAIL"
        failure_reason = failure_reason or "process is no longer running"
    if http_failures:
        result = "FAIL"
    if rss_growth > args.max_rss_growth_mb:
        result = "FAIL"
        failure_reason = failure_reason or "RSS growth exceeded threshold"
    if db_growth > args.max_db_growth_mb:
        result = "FAIL"
        failure_reason = failure_reason or "database growth exceeded threshold"

    print()
    print(
        f"samples={samples} elapsed={time.monotonic() - started:.0f}s "
        f"rss {rss_start:.1f}->{rss_now:.1f} MiB (growth {rss_growth:+.1f}) "
        f"db {db_start:.1f}->{db_now:.1f} MiB (growth {db_growth:+.1f}) "
        f"http_failures={http_failures}"
    )
    print(f"RESULT: {result}" + (f" ({failure_reason})" if failure_reason else ""))
    return 0 if result == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
