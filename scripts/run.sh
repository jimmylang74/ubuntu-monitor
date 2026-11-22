#!/usr/bin/env bash
# Run the project inside its own virtualenv.
#
#   ./scripts/run.sh                      # web service on 0.0.0.0:8010
#   ./scripts/run.sh --config FILE        # web service with an explicit config
#   ./scripts/run.sh check                # validate config, print effective settings
#   ./scripts/run.sh doctor               # Phase 1 hardware diagnostics
#   ./scripts/run.sh sample --count 5     # Phase 1 CLI sampler
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
if [[ ! -x "$ROOT/.venv/bin/python" ]]; then
    echo "missing .venv; run scripts/setup.sh first" >&2
    exit 1
fi

export PM_CONFIG="${PM_CONFIG:-$ROOT/config/config.yaml}"

# shellcheck disable=SC1091
source "$ROOT/.venv/bin/activate"

case "${1:-}" in
    check|doctor|sample)
        exec python -m app.cli "$@"
        ;;
    --config)
        if [[ -z "${2:-}" ]]; then
            echo "--config requires a path" >&2
            exit 2
        fi
        PM_CONFIG="$2"
        exec python -m app.main
        ;;
    web)
        shift
        exec python -m app.main "$@"
        ;;
    "")
        exec python -m app.main
        ;;
    *)
        echo "usage: $0 [web|check|doctor|sample|--config FILE]" >&2
        exit 2
        ;;
esac
