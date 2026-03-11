#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BASE_WS="${BASE_WS:-ws://127.0.0.1:8765}"
MESSAGE="${1:-hello from AgentRelay test client}"

BOT_PRIV="${BOT_PRIV:-2a4af8e3f1e39913e22911dad45a9a22ecb51eb7aca127c93339f212bdc94694}"
BOT_ID="${BOT_ID:-6ed60419f60b078c67837714d740016154ee70d3aca8203cdae040ad2876b5ee}"
CLIENT_PRIV="${CLIENT_PRIV:-7c8da769a5b9cf5f121e406328b2b4d547ba90e2f09687a441929488c9f7c7c7}"
CLIENT_ID="${CLIENT_ID:-d03ecba46e21d327e9c3f83cde7f652c4e2ea497337860e5222a1a6f1bf3523f}"

if [[ -x "$ROOT_DIR/.venv/bin/python" ]]; then
  PYTHON_BIN="$ROOT_DIR/.venv/bin/python"
elif [[ -x "$ROOT_DIR/../.venv/bin/python" ]]; then
  PYTHON_BIN="$ROOT_DIR/../.venv/bin/python"
else
  PYTHON_BIN="${PYTHON_BIN:-python3}"
fi

echo "[1/3] Allow client -> bot"
"$PYTHON_BIN" "$ROOT_DIR/agent_client.py" \
  --base-ws "$BASE_WS" \
  --private-key "$BOT_PRIV" \
  --allow-agent "$CLIENT_ID"

echo "[2/3] Start bot receiver with auto reply"
"$PYTHON_BIN" -u "$ROOT_DIR/agent_client.py" \
  --base-ws "$BASE_WS" \
  --private-key "$BOT_PRIV" \
  --peer-id "$CLIENT_ID" \
  --auto-reply \
  > /tmp/agentrelay-bot-client.log 2>&1 &
BOT_PID=$!

cleanup() {
  if [[ -n "${BOT_PID:-}" ]] && kill -0 "$BOT_PID" >/dev/null 2>&1; then
    kill "$BOT_PID" >/dev/null 2>&1 || true
  fi
}
trap cleanup EXIT

sleep 1

echo "[3/3] Send DM from client -> bot"
"$PYTHON_BIN" -u "$ROOT_DIR/agent_client.py" \
  --base-ws "$BASE_WS" \
  --private-key "$CLIENT_PRIV" \
  --peer-id "$BOT_ID" \
  --send "$MESSAGE"

sleep 2
echo
echo "Bot log: /tmp/agentrelay-bot-client.log"
tail -n 40 /tmp/agentrelay-bot-client.log || true
