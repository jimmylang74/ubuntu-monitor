#!/usr/bin/env bash
# Install and start the systemd unit for the current checkout.
#
#   sudo ./scripts/install-service.sh
#   sudo PM_USER=other ./scripts/install-service.sh
#
# The unit runs as the invoking user (or PM_USER).  It relies on the scoped
# NOPASSWD RAPL helper installed by scripts/install-rapl-helper.sh; no password
# is written to the unit, the config or the repository.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TARGET_USER="${PM_USER:-${SUDO_USER:-${USER}}}"
TARGET_GROUP="$(id -gn "$TARGET_USER")"
SERVICE_NAME="power-monitor.service"

if [[ "$(id -u)" -ne 0 ]]; then
    echo "run this script with sudo" >&2
    exit 1
fi

if [[ ! -f "$ROOT/config/config.yaml" ]]; then
    echo "missing $ROOT/config/config.yaml" >&2
    echo "copy config/config.example.yaml to config/config.yaml and edit it first" >&2
    exit 1
fi

if [[ ! -x "$ROOT/.venv/bin/python" ]]; then
    echo "missing $ROOT/.venv; run scripts/setup.sh first" >&2
    exit 1
fi

install -d -o "$TARGET_USER" -g "$TARGET_GROUP" -m 0755 "$ROOT/data" "$ROOT/logs"

sed \
    -e "s|@ROOT@|$ROOT|g" \
    -e "s|@USER@|$TARGET_USER|g" \
    -e "s|@GROUP@|$TARGET_GROUP|g" \
    "$ROOT/deploy/power-monitor.service" > "/etc/systemd/system/$SERVICE_NAME"
chmod 0644 "/etc/systemd/system/$SERVICE_NAME"

systemctl daemon-reload
systemctl enable "$SERVICE_NAME" >/dev/null
systemctl restart "$SERVICE_NAME"

echo "installed $SERVICE_NAME for user $TARGET_USER"
echo "logs: journalctl -u $SERVICE_NAME -f"
systemctl --no-pager --lines=10 status "$SERVICE_NAME" || true
