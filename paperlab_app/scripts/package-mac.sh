#!/bin/bash
set -euo pipefail
APP_DIR="$(cd "$(dirname "$0")/.." && pwd)"
ROOT_DIR="$(cd "$APP_DIR/.." && pwd)"
cd "$ROOT_DIR"
"$ROOT_DIR/.venv/bin/python" -m unittest discover -s paperlab_local/tests -p 'test_*.py'
cd "$APP_DIR"
npm run typecheck
npm test
npm run build:web
npm run build:sidecar
env -u HTTP_PROXY -u HTTPS_PROXY -u ALL_PROXY -u http_proxy -u https_proxy \
  ELECTRON_MIRROR="${ELECTRON_MIRROR:-https://npmmirror.com/mirrors/electron/}" \
  ELECTRON_BUILDER_BINARIES_MIRROR="${ELECTRON_BUILDER_BINARIES_MIRROR:-https://npmmirror.com/mirrors/electron-builder-binaries/}" \
  npx electron-builder --mac dmg --x64
