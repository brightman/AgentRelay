#!/usr/bin/env bash
set -euo pipefail

ROOT="/Users/yong.feng/Bright/Project/nanobot/AgentRelay"
NANOBOT_ROOT="/Users/yong.feng/Bright/Project/nanobot/nanobot"
NANOBOT_VENV_PY="/Users/yong.feng/Bright/Project/nanobot/.venv/bin/python"
CONFIG_PATH="/tmp/nanobot-agentrelay-config.json"

NANOBOT_ID="6ed60419f60b078c67837714d740016154ee70d3aca8203cdae040ad2876b5ee"
CLIENT_PRIV="7c8da769a5b9cf5f121e406328b2b4d547ba90e2f09687a441929488c9f7c7c7"
CLIENT_ID="d03ecba46e21d327e9c3f83cde7f652c4e2ea497337860e5222a1a6f1bf3523f"
TEST_MSG="${1:-你好 nanobot，收到请回复一句话}"

SERVER_PID=""
GATEWAY_PID=""
CLIENT_PID=""

cleanup() {
  for pid in "$CLIENT_PID" "$GATEWAY_PID" "$SERVER_PID"; do
    if [[ -n "${pid}" ]] && kill -0 "${pid}" >/dev/null 2>&1; then
      kill "${pid}" >/dev/null 2>&1 || true
    fi
  done
}
trap cleanup EXIT

python3 - <<'PY'
import json
from pathlib import Path
cfg = json.loads(Path.home().joinpath('.nanobot/config.json').read_text())
for _, v in cfg.get('channels', {}).items():
    if isinstance(v, dict):
        v['enabled'] = False
cfg.setdefault('channels', {})['agentrelay'] = {
    'enabled': True,
    'serverUrl': 'ws://127.0.0.1:8000',
    'privateKey': '2a4af8e3f1e39913e22911dad45a9a22ecb51eb7aca127c93339f212bdc94694',
    'agentId': '6ed60419f60b078c67837714d740016154ee70d3aca8203cdae040ad2876b5ee',
    'autoAllowFrom': ['d03ecba46e21d327e9c3f83cde7f652c4e2ea497337860e5222a1a6f1bf3523f'],
    'allowFrom': [],
}
Path('/tmp/nanobot-agentrelay-config.json').write_text(json.dumps(cfg, ensure_ascii=False, indent=2))
print('/tmp/nanobot-agentrelay-config.json')
PY

echo "[1/4] start AgentRelay demo server"
python3 -u "$ROOT/ws_server_demo.py" \
  --host 127.0.0.1 --port 8000 \
  --allow "$NANOBOT_ID:$CLIENT_ID,$CLIENT_ID:$NANOBOT_ID" \
  > /tmp/agentrelay-server.log 2>&1 &
SERVER_PID=$!
sleep 1

echo "[2/4] start nanobot gateway with AgentRelay channel"
PYTHONPATH="$NANOBOT_ROOT" "$NANOBOT_VENV_PY" -u -m nanobot.cli.commands \
  --config "$CONFIG_PATH" gateway -p 18790 \
  > /tmp/nanobot-agentrelay-gateway.log 2>&1 &
GATEWAY_PID=$!
sleep 6

echo "[3/4] start agent_client and send message"
python3 -u "$ROOT/agent_client.py" \
  --base-ws ws://127.0.0.1:8000 \
  --private-key "$CLIENT_PRIV" \
  --peer-id "$NANOBOT_ID" \
  --send "$TEST_MSG" \
  > /tmp/agentrelay-client.log 2>&1 &
CLIENT_PID=$!

echo "waiting for reply (up to 60s)..."
for _ in {1..60}; do
  if grep -q -- "-> .* (message):" /tmp/agentrelay-client.log; then
    break
  fi
  sleep 1
done

echo "[4/4] recent client output"
tail -n 40 /tmp/agentrelay-client.log || true

echo "logs:"
echo "  server : /tmp/agentrelay-server.log"
echo "  gateway: /tmp/nanobot-agentrelay-gateway.log"
echo "  client : /tmp/agentrelay-client.log"
