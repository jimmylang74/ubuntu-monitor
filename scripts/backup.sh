#!/usr/bin/env bash
# Back up the configured SQLite database without stopping the service.
#   ./scripts/backup.sh
#   ./scripts/backup.sh --dest /mnt/backups/power.db --keep 30
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
exec "$ROOT/.venv/bin/python" "$ROOT/scripts/backup.py" "$@"
