#!/bin/bash
set -euo pipefail
APP_DIR="$(cd "$(dirname "$0")/.." && pwd)"
ROOT_DIR="$(cd "$APP_DIR/.." && pwd)"
BUILD_DIR="$APP_DIR/.sidecar-build"
export PYINSTALLER_CONFIG_DIR="$BUILD_DIR/config"
rm -rf "$BUILD_DIR" "$APP_DIR/sidecar"
mkdir -p "$BUILD_DIR" "$APP_DIR/sidecar"
if ! "$ROOT_DIR/.venv/bin/python" -c 'import PyInstaller' 2>/dev/null; then
  UV_CACHE_DIR="${TMPDIR:-/tmp}/paperlab-uv-cache" uv pip install --python "$ROOT_DIR/.venv/bin/python" 'pyinstaller>=6.15'
fi
"$ROOT_DIR/.venv/bin/python" -m PyInstaller --noconfirm --clean --onefile \
  --name paperlab-sidecar --distpath "$APP_DIR/sidecar" --workpath "$BUILD_DIR/work" \
  --specpath "$BUILD_DIR" --collect-all paperlab_local "$APP_DIR/sidecar_entry.py"
