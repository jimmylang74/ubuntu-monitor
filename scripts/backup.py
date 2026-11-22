#!/usr/bin/env python3
"""Consistent online SQLite backup using the sqlite3 backup API.

Usage:
    ./scripts/backup.sh                       # backup next to the project
    ./scripts/backup.sh --dest /mnt/backups/power.db
    ./scripts/backup.sh --keep 30
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.config import load_config  # noqa: E402

TABLES = ("sample", "gpu_sample", "meter_sample", "aggregate_1m", "aggregate_1h", "event_log")


def backup(source: Path, dest: Path) -> dict[str, int]:
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists():
        dest.unlink()
    src = sqlite3.connect(f"file:{source}?mode=ro", uri=True)
    dst = sqlite3.connect(dest)
    try:
        src.backup(dst)
        result = dst.execute("PRAGMA integrity_check").fetchone()
        if not result or result[0] != "ok":
            raise RuntimeError(f"integrity check failed: {result!r}")
        counts: dict[str, int] = {}
        for table in TABLES:
            row = dst.execute(f"SELECT COUNT(*) FROM {table}").fetchone()
            counts[table] = int(row[0]) if row else 0
        return counts
    finally:
        src.close()
        dst.close()


def prune(directory: Path, keep: int, pattern: str) -> list[Path]:
    if keep <= 0:
        return []
    files = sorted(directory.glob(pattern), key=lambda item: item.stat().st_mtime, reverse=True)
    removed = files[keep:]
    for item in removed:
        item.unlink()
    return removed


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Back up the power-monitor SQLite database")
    parser.add_argument("--config", type=Path, default=None, help="path to config.yaml")
    parser.add_argument("--dest", type=Path, default=None, help="destination .db file")
    parser.add_argument(
        "--keep", type=int, default=14, help="keep N newest backups in the destination dir (0=all)"
    )
    args = parser.parse_args(argv)

    try:
        cfg = load_config(args.config)
    except ValueError as exc:
        print(f"configuration error: {exc}", file=sys.stderr)
        return 2

    source = cfg.database_path
    if not source.is_file():
        print(f"database not found: {source}", file=sys.stderr)
        return 1

    stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    dest = args.dest or ROOT / "backups" / f"power-monitor-{stamp}.db"
    if dest.is_dir():
        dest = dest / f"power-monitor-{stamp}.db"

    try:
        counts = backup(source, dest)
    except (OSError, sqlite3.Error, RuntimeError) as exc:
        print(f"backup failed: {exc}", file=sys.stderr)
        return 1

    removed = prune(dest.parent, args.keep, "power-monitor-*.db")
    print(f"backup written: {dest} ({dest.stat().st_size / 1024 / 1024:.1f} MiB)")
    print("rows: " + " ".join(f"{name}={count}" for name, count in counts.items()))
    if removed:
        print(f"pruned {len(removed)} old backup(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
