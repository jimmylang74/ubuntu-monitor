#!/usr/bin/env bash
# Build a self-contained Ubuntu 24.04 amd64 .deb into ./dist.
#
#   ./scripts/package/build-deb.sh
#
# The package ships a wheelhouse and creates the venv at install time, so the
# resulting .deb needs no network access on the target machine.
set -euo pipefail

SCRIPT_PATH="$(readlink -f "${BASH_SOURCE[0]}")"
ROOT="$(cd "$(dirname "$SCRIPT_PATH")/../.." && pwd)"
DEBIAN_DIR="$ROOT/debian"
DIST="$ROOT/dist"
WHEEL_CACHE="$ROOT/.cache/wheels"
ARCH="amd64"
PYTHON_VERSION="3.12"
PYTHON_ABI="cp312"
PLATFORM="manylinux2014_x86_64"

VERSION="$(python3 - "$ROOT/pyproject.toml" <<'PY'
import pathlib
import sys
import tomllib

print(tomllib.loads(pathlib.Path(sys.argv[1]).read_text(encoding="utf-8"))["project"]["version"])
PY
)"

WORK="$(mktemp -d /tmp/opencode/power-monitor-deb.XXXXXX)"
trap 'rm -rf "$WORK"' EXIT
STAGE="$WORK/stage"
LIBDIR="$STAGE/usr/lib/power-monitor"
PKG="power-monitor_${VERSION}_${ARCH}.deb"

echo "building $PKG (version from pyproject.toml)"

# 1. Wheelhouse, cached between builds.
mkdir -p "$WHEEL_CACHE" "$LIBDIR/wheels"
python3 -m pip download --quiet --only-binary=:all: \
    --platform "$PLATFORM" --python-version "$PYTHON_VERSION" \
    --implementation cp --abi "$PYTHON_ABI" \
    --dest "$WHEEL_CACHE" \
    -r "$ROOT/scripts/package/requirements.lock.txt"
cp -a "$WHEEL_CACHE"/. "$LIBDIR/wheels/"

# 2. Application payload (no bytecode or caches).
mkdir -p "$STAGE/DEBIAN" \
    "$STAGE/etc/power-monitor" \
    "$STAGE/etc/sudoers.d" \
    "$STAGE/lib/systemd/system" \
    "$STAGE/usr/bin" \
    "$STAGE/usr/share/doc/power-monitor" \
    "$LIBDIR"
(cd "$ROOT" && tar cf - --exclude='__pycache__' --exclude='*.pyc' app) | \
    (cd "$LIBDIR" && tar xf -)

install -m 0644 "$ROOT/scripts/package/requirements.lock.txt" "$LIBDIR/requirements.lock.txt"
install -m 0755 "$ROOT/deploy/rapl-read" "$LIBDIR/rapl-read"
printf '%s\n' "$VERSION" > "$LIBDIR/version"

# 3. Debian metadata and maintainer scripts.
sed "s|@VERSION@|$VERSION|g" "$DEBIAN_DIR/control.in" > "$STAGE/DEBIAN/control"
for script in postinst prerm postrm; do
    install -m 0755 "$DEBIAN_DIR/$script" "$STAGE/DEBIAN/$script"
done
install -m 0644 "$DEBIAN_DIR/conffiles" "$STAGE/DEBIAN/conffiles"
install -m 0644 "$DEBIAN_DIR/copyright" "$STAGE/DEBIAN/copyright"

install -m 0644 "$DEBIAN_DIR/config.yaml" "$STAGE/etc/power-monitor/config.yaml"
install -m 0440 "$DEBIAN_DIR/sudoers" "$STAGE/etc/sudoers.d/power-monitor-rapl"
install -m 0644 "$DEBIAN_DIR/power-monitor.service" \
    "$STAGE/lib/systemd/system/power-monitor.service"
install -m 0755 "$DEBIAN_DIR/power-monitor" "$STAGE/usr/bin/power-monitor"

install -m 0644 "$ROOT/README.md" "$STAGE/usr/share/doc/power-monitor/README.md"
install -m 0644 "$DEBIAN_DIR/copyright" "$STAGE/usr/share/doc/power-monitor/copyright"
gzip -c "$DEBIAN_DIR/changelog" > "$STAGE/usr/share/doc/power-monitor/changelog.gz"

# 4. Sanity-check the shipped conffile with the project's own config loader.
if [[ -x "$ROOT/.venv/bin/python" ]]; then
    PM_CONFIG="$STAGE/etc/power-monitor/config.yaml" \
        "$ROOT/.venv/bin/python" -c \
        'from app.config import load_config; import os; c = load_config(os.environ["PM_CONFIG"]); assert c.rapl.helper == "/usr/lib/power-monitor/rapl-read"; assert str(c.database_path).startswith("/")'
fi

# 5. Build.
mkdir -p "$DIST"
dpkg-deb --root-owner-group --build "$STAGE" "$DIST/$PKG"

echo
dpkg-deb --info "$DIST/$PKG" | sed -n '1,20p'
echo
echo "wheels: $(find "$LIBDIR/wheels" -name '*.whl' | wc -l)"
echo "output: $DIST/$PKG"
sha256sum "$DIST/$PKG"
if command -v lintian >/dev/null 2>&1; then
    echo
    echo "lintian (informational):"
    lintian "$DIST/$PKG" || true
fi
