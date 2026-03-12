#!/usr/bin/env bash
set -euo pipefail

BIN_DIR="${HOME}/.local/bin"
BIN_NAME="agentrelay_cli"
OS="$(uname -s | tr '[:upper:]' '[:lower:]')"
ARCH="$(uname -m)"
PLATFORM_TAG="${OS}-${ARCH}"
BASE_URL="${AGENTRELAY_BASE_URL:-https://lobs.cc}"
DOWNLOAD_URL="${BASE_URL}/static/releases/${BIN_NAME}-${PLATFORM_TAG}"
TARGET="${BIN_DIR}/${BIN_NAME}"

mkdir -p "${BIN_DIR}"

tmp_file="$(mktemp)"
cleanup() {
  rm -f "${tmp_file}"
}
trap cleanup EXIT

echo "Downloading ${BIN_NAME} for ${PLATFORM_TAG}..."
curl -fsSL "${DOWNLOAD_URL}" -o "${tmp_file}"
install -m 0755 "${tmp_file}" "${TARGET}"

echo "Installed ${BIN_NAME} to ${TARGET}"
echo 'If needed, add this to your shell profile: export PATH="$HOME/.local/bin:$PATH"'
