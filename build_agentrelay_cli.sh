#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PYTHON_BIN="${PYTHON_BIN:-$ROOT_DIR/../.venv/bin/python}"
OUT_DIR="${OUT_DIR:-$ROOT_DIR/static/releases}"
BIN_NAME="${BIN_NAME:-agentrelay_cli}"
PLATFORM_TAG="${PLATFORM_TAG:-$(uname -s | tr '[:upper:]' '[:lower:]')-$(uname -m)}"
PYINSTALLER_CONFIG_DIR="${PYINSTALLER_CONFIG_DIR:-$ROOT_DIR/build/pyinstaller-config}"
BASE_URL="${BASE_URL:-https://lobs.cc}"

if ! "$PYTHON_BIN" - <<'PY' >/dev/null 2>&1
import importlib.util
raise SystemExit(0 if importlib.util.find_spec("PyInstaller") else 1)
PY
then
  echo "PyInstaller is required in ${PYTHON_BIN} to build agentrelay_cli." >&2
  echo "Install it first, then rerun this script." >&2
  exit 1
fi

mkdir -p "${OUT_DIR}"
mkdir -p "${PYINSTALLER_CONFIG_DIR}"
BUILD_DIR="${ROOT_DIR}/build/pyinstaller-${PLATFORM_TAG}"
DIST_DIR="${ROOT_DIR}/dist/pyinstaller-${PLATFORM_TAG}"
rm -rf "${BUILD_DIR}" "${DIST_DIR}"

PYINSTALLER_CONFIG_DIR="${PYINSTALLER_CONFIG_DIR}" "$PYTHON_BIN" -m PyInstaller \
  --onefile \
  --clean \
  --name "${BIN_NAME}" \
  --paths "${ROOT_DIR}" \
  --hidden-import agent_client \
  --hidden-import identity \
  --hidden-import _cffi_backend \
  --hidden-import cffi \
  --distpath "${DIST_DIR}" \
  --workpath "${BUILD_DIR}" \
  "${ROOT_DIR}/agentrelay_cli.py"

cp "${DIST_DIR}/${BIN_NAME}" "${OUT_DIR}/${BIN_NAME}-${PLATFORM_TAG}"
chmod +x "${OUT_DIR}/${BIN_NAME}-${PLATFORM_TAG}"
echo "Built ${OUT_DIR}/${BIN_NAME}-${PLATFORM_TAG}"
