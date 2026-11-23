#!/usr/bin/env bash
# Install the read-only RAPL helper and a scoped NOPASSWD sudoers rule.
# Run with sudo:  sudo ./scripts/install-rapl-helper.sh
# No password is stored in the project or in the config file.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TARGET_USER="${SUDO_USER:-${USER}}"

if [[ "$(id -u)" -ne 0 ]]; then
    echo "run this script with sudo" >&2
    exit 1
fi

install -d -o root -g root -m 0755 /usr/local/lib/power-monitor
install -o root -g root -m 0755 "$ROOT/deploy/rapl-read" /usr/local/lib/power-monitor/rapl-read

cat > /etc/sudoers.d/power-monitor-rapl <<EOF
$TARGET_USER ALL=(root) NOPASSWD: /usr/local/lib/power-monitor/rapl-read
EOF
chown root:root /etc/sudoers.d/power-monitor-rapl
chmod 0440 /etc/sudoers.d/power-monitor-rapl
visudo -cf /etc/sudoers.d/power-monitor-rapl

echo "installed for user: $TARGET_USER"

# Verify through the same path the service uses: the target user runs the
# helper via `sudo -n`.  Running the helper directly as that user would return
# empty energy values because energy_uj is root-only.  The helper scans every
# powercap `*:*` domain, so AMD Zen RAPL is covered as well.
if ! output=$(sudo -u "$TARGET_USER" -n sudo -n /usr/local/lib/power-monitor/rapl-read); then
    echo "warning: $TARGET_USER cannot execute the RAPL helper via sudo -n" >&2
    exit 1
fi
if printf '%s\n' "$output" | awk -F '\t' 'NF >= 3 && $3 ~ /^[0-9]+$/ {found=1} END {exit !found}'; then
    echo "RAPL helper verified for user: $TARGET_USER"
else
    # A VM, ARM host or kernel without powercap simply exposes no counters.
    # Installation itself succeeded; CPU power will be reported unavailable.
    echo "warning: helper installed, but this host exposes no powercap energy counters" >&2
    echo "         CPU power will be unavailable; GPU and other collectors still work" >&2
fi
