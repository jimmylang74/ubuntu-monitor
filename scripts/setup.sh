#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON="${PYTHON:-/usr/bin/python3.12}"

if [[ ! -x "$ROOT/.venv/bin/python" ]]; then
    echo "creating $ROOT/.venv"
    "$PYTHON" -m venv "$ROOT/.venv"
fi

"$ROOT/.venv/bin/pip" install --upgrade pip >/dev/null
"$ROOT/.venv/bin/pip" install -r "$ROOT/requirements.txt" -r "$ROOT/requirements-dev.txt"

echo "done. activate with: source .venv/bin/activate"
