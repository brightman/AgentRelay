#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEFAULT_SERVER_HOST="127.0.0.1"
DEFAULT_SERVER_PORT="8000"
DEFAULT_SERVER_MODE="demo"
DB_PATH="$ROOT_DIR/channel.db"
FALLBACK_DB_PATH="/tmp/agentrelay/channel.db"
CONFIG_PATH="$ROOT_DIR/server_config.json"
NANOBOT_CONFIG_PATH="${NANOBOT_CONFIG_PATH:-$HOME/.nanobot/config.json}"

# Defaults aligned with run_e2e_demo.sh
DEFAULT_BOT_NAME="nanobot"
DEFAULT_BOT_AGENT_ID="6ed60419f60b078c67837714d740016154ee70d3aca8203cdae040ad2876b5ee"
DEFAULT_BOT_PRIVATE_KEY="2a4af8e3f1e39913e22911dad45a9a22ecb51eb7aca127c93339f212bdc94694"
DEFAULT_BOT_AUTO_ALLOW_FROM="d03ecba46e21d327e9c3f83cde7f652c4e2ea497337860e5222a1a6f1bf3523f"

DEFAULT_CLIENT_NAME="default-client"
DEFAULT_CLIENT_AGENT_ID="d03ecba46e21d327e9c3f83cde7f652c4e2ea497337860e5222a1a6f1bf3523f"
DEFAULT_CLIENT_PRIVATE_KEY="7c8da769a5b9cf5f121e406328b2b4d547ba90e2f09687a441929488c9f7c7c7"
DEFAULT_CLIENT_PEER_BOT_ID="$DEFAULT_BOT_AGENT_ID"
DEFAULT_CHAT_ID="chat-1"

has_modules() {
  local py="$1"
  shift
  "$py" - "$@" <<'PY' >/dev/null 2>&1
import importlib
import sys
mods = sys.argv[1:]
for m in mods:
    importlib.import_module(m)
PY
}

pick_python_for() {
  local candidates=()
  if [[ -x "$ROOT_DIR/../.venv/bin/python" ]]; then
    candidates+=("$ROOT_DIR/../.venv/bin/python")
  fi
  if [[ -x "$ROOT_DIR/.venv/bin/python" ]]; then
    candidates+=("$ROOT_DIR/.venv/bin/python")
  fi
  candidates+=("$(command -v python3)")

  local py
  for py in "${candidates[@]}"; do
    if has_modules "$py" "$@"; then
      echo "$py"
      return 0
    fi
  done
  return 1
}

load_nanobot_agentrelay_defaults() {
  local py assignments
  py="$(pick_python_for json || command -v python3)"

  assignments="$(
    "$py" - "$NANOBOT_CONFIG_PATH" <<'PY'
import json
import sys
from pathlib import Path
from urllib.parse import urlparse


def quote(value: str) -> str:
    return "'" + value.replace("'", "'\"'\"'") + "'"


path = Path(sys.argv[1]).expanduser()
if not path.exists():
    raise SystemExit(0)

try:
    data = json.loads(path.read_text(encoding="utf-8"))
except Exception:
    raise SystemExit(0)

channels = data.get("channels") or {}
cfg = (channels.get("agentrelay")) or (channels.get("agenthub")) or {}
server_url = str(cfg.get("serverUrl") or cfg.get("server_url") or "").strip()
private_key = str(cfg.get("privateKey") or cfg.get("private_key") or "").strip()
agent_id = str(cfg.get("agentId") or cfg.get("agent_id") or "").strip()
auto_allow = cfg.get("autoAllowFrom") or cfg.get("auto_allow_from") or []
if not isinstance(auto_allow, list):
    auto_allow = []

if server_url:
    parsed = urlparse(server_url)
    if parsed.hostname:
        print(f"DEFAULT_SERVER_HOST={quote(parsed.hostname)}")
    if parsed.port:
        print(f"DEFAULT_SERVER_PORT={quote(str(parsed.port))}")

if private_key:
    print(f"DEFAULT_BOT_PRIVATE_KEY={quote(private_key)}")
if agent_id:
    print(f"DEFAULT_BOT_AGENT_ID={quote(agent_id)}")
if auto_allow:
    print(f"DEFAULT_BOT_AUTO_ALLOW_FROM={quote(','.join(str(x).strip() for x in auto_allow if str(x).strip()))}")
PY
  )"

  if [[ -n "$assignments" ]]; then
    eval "$assignments"
    DEFAULT_CLIENT_PEER_BOT_ID="$DEFAULT_BOT_AGENT_ID"
  fi
}

load_nanobot_agentrelay_defaults


resolve_db_path() {
  local py
  py="$(pick_python_for sqlite3 || command -v python3)"

  # Try preferred DB path first
  if "$py" - <<PY >/dev/null 2>&1
import sqlite3
from pathlib import Path
p = Path(r"$DB_PATH")
p.parent.mkdir(parents=True, exist_ok=True)
conn = sqlite3.connect(p)
conn.execute("CREATE TABLE IF NOT EXISTS _probe(id INTEGER PRIMARY KEY)")
conn.commit()
conn.close()
PY
  then
    return
  fi

  # Fallback to /tmp when workspace path is restricted
  DB_PATH="$FALLBACK_DB_PATH"
  "$py" - <<PY
import sqlite3
from pathlib import Path
p = Path(r"$DB_PATH")
p.parent.mkdir(parents=True, exist_ok=True)
conn = sqlite3.connect(p)
conn.execute("CREATE TABLE IF NOT EXISTS _probe(id INTEGER PRIMARY KEY)")
conn.commit()
conn.close()
PY
}

ensure_config_file() {
  if [[ -f "$CONFIG_PATH" ]]; then
    return
  fi
  local py
  py="$(pick_python_for json || command -v python3)"
  "$py" - <<PY > "$CONFIG_PATH"
import json

auto_allow = [s for s in "${DEFAULT_BOT_AUTO_ALLOW_FROM}".split(",") if s]
data = {
    "server": {
        "host": "${DEFAULT_SERVER_HOST}",
        "port": int("${DEFAULT_SERVER_PORT}"),
        "mode": "${DEFAULT_SERVER_MODE}",
        "dbPath": "./channel.db",
    },
    "bot": {
        "name": "${DEFAULT_BOT_NAME}",
        "agentId": "${DEFAULT_BOT_AGENT_ID}",
        "privateKey": "${DEFAULT_BOT_PRIVATE_KEY}",
        "autoAllowFrom": auto_allow,
    },
    "client": {
        "name": "${DEFAULT_CLIENT_NAME}",
        "agentId": "${DEFAULT_CLIENT_AGENT_ID}",
        "privateKey": "${DEFAULT_CLIENT_PRIVATE_KEY}",
        "peerBotAgentId": "${DEFAULT_CLIENT_PEER_BOT_ID}",
        "chatId": "${DEFAULT_CHAT_ID}",
        "interactive": True,
    },
}
print(json.dumps(data, ensure_ascii=False, indent=2))
PY
}

seed_default_db() {
  local py
  py="$(pick_python_for sqlite3 || command -v python3)"
  "$py" - <<PY
import sqlite3
import time
from pathlib import Path

p = Path(r"$DB_PATH")
conn = sqlite3.connect(p)
try:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS acl_allow (
            owner_agent TEXT NOT NULL,
            sender_agent TEXT NOT NULL,
            created_at INTEGER NOT NULL,
            updated_by_event_id TEXT NOT NULL,
            PRIMARY KEY (owner_agent, sender_agent)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS blacklist (
            owner_agent TEXT NOT NULL,
            blocked_agent TEXT NOT NULL,
            created_at INTEGER NOT NULL,
            updated_by_event_id TEXT NOT NULL,
            PRIMARY KEY (owner_agent, blocked_agent)
        )
        """
    )

    now = int(time.time())
    # Bidirectional defaults for convenient local testing.
    rows = [
        (r"$DEFAULT_BOT_AGENT_ID", r"$DEFAULT_CLIENT_AGENT_ID", now, "manage-default-seed-bot"),
        (r"$DEFAULT_CLIENT_AGENT_ID", r"$DEFAULT_BOT_AGENT_ID", now, "manage-default-seed-client"),
    ]
    conn.executemany(
        """
        INSERT OR IGNORE INTO acl_allow(owner_agent, sender_agent, created_at, updated_by_event_id)
        VALUES(?,?,?,?)
        """,
        rows,
    )
    conn.commit()
finally:
    conn.close()
PY
}

choose_profile() {
  printf '%s\n' "Profile:" >&2
  printf '%s\n' "  1) default" >&2
  printf '%s\n' "  2) manual input" >&2
  read -r -p "Choose [1/2, default 1]: " profile
  echo "${profile:-1}"
}

usage() {
  cat <<'EOF'
Usage:
  manage.sh menu
  manage.sh server [--profile default|manual|1|2] [--host HOST] [--port PORT] [--mode full|demo|1|2] [--allow A:B,C:D]
  manage.sh server-stop
  manage.sh web [--host HOST] [--port PORT]
  manage.sh web-stop
  manage.sh client [--profile default|manual|1|2] [--base-ws WS_URL] [--private-key HEX]
                   [--chat-type dm|topic] [--peer-id AGENT_ID] [--topic TOPIC] [--chat-id CHAT_ID]
                   [--mode interactive|send|subscribe|unsubscribe|1|2|3|4] [--message TEXT]
                   [--topic-subscribe TOPIC] [--topic-unsubscribe TOPIC]
  manage.sh new-agent
  manage.sh info
EOF
}

start_agentrelay_server() {
  local profile host port mode py allow_pairs arg
  profile=""
  host=""
  port=""
  mode=""
  allow_pairs=""

  while [[ $# -gt 0 ]]; do
    arg="$1"
    case "$arg" in
      --profile)
        profile="${2:-}"
        shift 2
        ;;
      --host)
        host="${2:-}"
        shift 2
        ;;
      --port)
        port="${2:-}"
        shift 2
        ;;
      --mode)
        mode="${2:-}"
        shift 2
        ;;
      --allow)
        allow_pairs="${2:-}"
        shift 2
        ;;
      -h|--help)
        usage
        exit 0
        ;;
      *)
        echo "Unknown server option: $arg"
        usage
        exit 1
        ;;
    esac
  done

  if [[ -z "$profile" ]]; then
    profile="$(choose_profile)"
  fi
  case "$profile" in
    manual|2) profile="2" ;;
    default|1|"") profile="1" ;;
    *)
      echo "Invalid profile: $profile"
      exit 1
      ;;
  esac

  if [[ -n "$mode" ]]; then
    case "$mode" in
      full|1) mode="1" ;;
      demo|2) mode="2" ;;
      *)
        echo "Invalid server mode: $mode"
        exit 1
        ;;
    esac
  fi

  if [[ "$profile" == "2" ]]; then
    if [[ -z "$host" ]]; then
      read -r -p "Host [${DEFAULT_SERVER_HOST}]: " host
    fi
    host="${host:-$DEFAULT_SERVER_HOST}"
    if [[ -z "$port" ]]; then
      read -r -p "Port [${DEFAULT_SERVER_PORT}]: " port
    fi
    port="${port:-$DEFAULT_SERVER_PORT}"
    if [[ -z "$mode" ]]; then
      echo "Choose server mode:"
      echo "  1) full (agent_relay.py, FastAPI)"
      echo "  2) demo (ws_server_demo.py, lightweight)"
      read -r -p "Mode [1/2, default 2]: " mode
    fi
    mode="${mode:-2}"
  else
    host="${host:-$DEFAULT_SERVER_HOST}"
    port="${port:-$DEFAULT_SERVER_PORT}"
    mode="${mode:-2}"
  fi

  if [[ "$mode" == "1" ]]; then
    if ! py="$(pick_python_for fastapi uvicorn nacl websockets)"; then
      echo "No Python runtime with fastapi+uvicorn+nacl+websockets found."
      echo "Tip: use demo mode (2) or install requirements first."
      exit 1
    fi
    echo "Starting AgentRelay server on ${host}:${port} with $py"
    cd "$ROOT_DIR"
    exec "$py" -m uvicorn agent_relay:app --host "$host" --port "$port"
  else
    if ! py="$(pick_python_for nacl websockets)"; then
      echo "No Python runtime with nacl+websockets found."
      exit 1
    fi
    if [[ -z "$allow_pairs" && "$profile" == "2" ]]; then
      read -r -p "Allow pairs (a:b,c:d, optional): " allow_pairs
    elif [[ -z "$allow_pairs" ]]; then
      allow_pairs="${DEFAULT_BOT_AGENT_ID}:${DEFAULT_CLIENT_AGENT_ID},${DEFAULT_CLIENT_AGENT_ID}:${DEFAULT_BOT_AGENT_ID}"
      echo "Using default allow pairs: $allow_pairs"
    fi
    echo "Starting demo AgentRelay server on ${host}:${port} with $py"
    exec "$py" "$ROOT_DIR/ws_server_demo.py" --host "$host" --port "$port" --allow "$allow_pairs"
  fi
}

start_web_server() {
  local host port py arg
  host=""
  port=""

  while [[ $# -gt 0 ]]; do
    arg="$1"
    case "$arg" in
      --host)
        host="${2:-}"
        shift 2
        ;;
      --port)
        port="${2:-}"
        shift 2
        ;;
      -h|--help)
        usage
        exit 0
        ;;
      *)
        echo "Unknown web option: $arg"
        usage
        exit 1
        ;;
    esac
  done

  host="${host:-$DEFAULT_SERVER_HOST}"
  port="${port:-8780}"
  if ! py="$(pick_python_for fastapi uvicorn)"; then
    echo "No Python runtime with fastapi+uvicorn found."
    exit 1
  fi
  echo "Starting AgentRelay web on ${host}:${port} with $py"
  cd "$ROOT_DIR"
  exec "$py" -m uvicorn web_server:app --host "$host" --port "$port"
}

stop_agentrelay_server() {
  local pids
  pids="$(
    ps -ax -o pid= -o command= | awk -v root="$ROOT_DIR" '
      index($0, root "/server.py") || index($0, root "/agent_relay.py") || index($0, root "/web_server.py") || index($0, root "/ws_server_demo.py") {
        print $1
      }
    '
  )"

  if [[ -z "${pids//[[:space:]]/}" ]]; then
    echo "No AgentRelay server process found for $ROOT_DIR"
    return 0
  fi

  echo "Stopping AgentRelay server process(es): $pids"
  # Send SIGTERM first so the server can shut down cleanly.
  kill $pids
}

stop_web_server() {
  local pids
  pids="$(
    ps -ax -o pid= -o command= | awk -v root="$ROOT_DIR" '
      index($0, root "/web_server.py") {
        print $1
      }
    '
  )"

  if [[ -z "${pids//[[:space:]]/}" ]]; then
    echo "No AgentRelay web process found for $ROOT_DIR"
    return 0
  fi

  echo "Stopping AgentRelay web process(es): $pids"
  kill $pids
}

start_agent_client() {
  local py profile private_key peer_id base_ws chat_id mode msg chat_type topic topic_subscribe topic_unsubscribe arg
  local -a cmd
  if ! py="$(pick_python_for nacl websockets)"; then
    echo "No Python runtime with nacl+websockets found."
    exit 1
  fi

  profile=""
  private_key=""
  peer_id=""
  base_ws=""
  chat_id=""
  mode=""
  msg=""
  chat_type=""
  topic=""
  topic_subscribe=""
  topic_unsubscribe=""

  while [[ $# -gt 0 ]]; do
    arg="$1"
    case "$arg" in
      --profile)
        profile="${2:-}"
        shift 2
        ;;
      --private-key)
        private_key="${2:-}"
        shift 2
        ;;
      --peer-id)
        peer_id="${2:-}"
        shift 2
        ;;
      --base-ws)
        base_ws="${2:-}"
        shift 2
        ;;
      --chat-id)
        chat_id="${2:-}"
        shift 2
        ;;
      --chat-type)
        chat_type="${2:-}"
        shift 2
        ;;
      --topic)
        topic="${2:-}"
        shift 2
        ;;
      --mode)
        mode="${2:-}"
        shift 2
        ;;
      --message|--send)
        msg="${2:-}"
        shift 2
        ;;
      --topic-subscribe)
        topic_subscribe="${2:-}"
        shift 2
        ;;
      --topic-unsubscribe)
        topic_unsubscribe="${2:-}"
        shift 2
        ;;
      --interactive)
        mode="1"
        shift
        ;;
      -h|--help)
        usage
        exit 0
        ;;
      *)
        echo "Unknown client option: $arg"
        usage
        exit 1
        ;;
    esac
  done

  if [[ -z "$profile" ]]; then
    profile="$(choose_profile)"
  fi
  case "$profile" in
    manual|2) profile="2" ;;
    default|1|"") profile="1" ;;
    *)
      echo "Invalid profile: $profile"
      exit 1
      ;;
  esac

  if [[ "$profile" == "2" ]]; then
    if [[ -z "$private_key" ]]; then
      read -r -p "Private key (hex): " private_key
    fi
    if [[ -z "$base_ws" ]]; then
      read -r -p "Base WS [ws://${DEFAULT_SERVER_HOST}:${DEFAULT_SERVER_PORT}]: " base_ws
    fi
    base_ws="${base_ws:-ws://${DEFAULT_SERVER_HOST}:${DEFAULT_SERVER_PORT}}"
  else
    private_key="${private_key:-$DEFAULT_CLIENT_PRIVATE_KEY}"
    base_ws="${base_ws:-ws://${DEFAULT_SERVER_HOST}:${DEFAULT_SERVER_PORT}}"
    echo "Using default client=${DEFAULT_CLIENT_AGENT_ID}"
  fi

  if [[ -n "$chat_type" ]]; then
    case "$chat_type" in
      topic|2) chat_type="topic" ;;
      dm|1|"") chat_type="dm" ;;
      *)
        echo "Invalid chat type: $chat_type"
        exit 1
        ;;
    esac
  else
    echo "Chat type:"
    echo "  1) dm"
    echo "  2) topic"
    read -r -p "Type [1/2, default 1]: " chat_type
    case "${chat_type:-1}" in
      2) chat_type="topic" ;;
      *) chat_type="dm" ;;
    esac
  fi

  if [[ "$chat_type" == "dm" ]]; then
    if [[ "$profile" == "2" ]]; then
      if [[ -z "$peer_id" ]]; then
        read -r -p "Peer agent id: " peer_id
      fi
      if [[ -z "$chat_id" ]]; then
        read -r -p "Chat id override (optional): " chat_id
      fi
    else
      peer_id="${peer_id:-$DEFAULT_CLIENT_PEER_BOT_ID}"
      echo "Using default dm peer=${peer_id}"
    fi
  else
    if [[ "$profile" == "2" ]]; then
      if [[ -z "$topic" ]]; then
        read -r -p "Topic name: " topic
      fi
      if [[ -z "$chat_id" ]]; then
        read -r -p "Chat id override (optional, e.g. topic:team-alpha): " chat_id
      fi
    else
      topic="${topic:-team-alpha}"
      echo "Using default topic=${topic}"
    fi
  fi

  if [[ -n "$mode" ]]; then
    case "$mode" in
      interactive|1) mode="1" ;;
      send|2) mode="2" ;;
      subscribe|3) mode="3" ;;
      unsubscribe|4) mode="4" ;;
      *)
        echo "Invalid client mode: $mode"
        exit 1
        ;;
    esac
  else
    echo "Start mode:"
    echo "  1) interactive chat"
    echo "  2) send once"
    echo "  3) subscribe only"
    echo "  4) unsubscribe only"
    read -r -p "Mode [1-4, default 1]: " mode
    mode="${mode:-1}"
  fi

  if [[ "$chat_type" == "topic" ]]; then
    if [[ "$mode" == "3" && -z "$topic_subscribe" ]]; then
      topic_subscribe="${topic:-$chat_id}"
    elif [[ "$mode" == "4" && -z "$topic_unsubscribe" ]]; then
      topic_unsubscribe="${topic:-$chat_id}"
    elif [[ ( "$mode" == "1" || "$mode" == "2" ) && -z "$topic_subscribe" ]]; then
      topic_subscribe="${topic:-$chat_id}"
    fi
  fi

  cmd=(
    "$py" "$ROOT_DIR/agent_client.py"
    --base-ws "$base_ws"
    --private-key "$private_key"
    --chat-type "$chat_type"
  )
  if [[ -n "$peer_id" ]]; then
    cmd+=(--peer-id "$peer_id")
  fi
  if [[ -n "$topic" ]]; then
    cmd+=(--topic "$topic")
  fi
  if [[ -n "$chat_id" ]]; then
    cmd+=(--chat-id "$chat_id")
  fi

  if [[ "$mode" == "2" ]]; then
    if [[ -z "$msg" ]]; then
      read -r -p "Message text: " msg
    fi
    if [[ -n "$topic_subscribe" ]]; then
      cmd+=(--topic-subscribe "$topic_subscribe")
    fi
    cmd+=(--send "$msg")
    exec "${cmd[@]}"
  elif [[ "$mode" == "3" ]]; then
    cmd=( "$py" "$ROOT_DIR/agent_client.py" --base-ws "$base_ws" --private-key "$private_key" --chat-type topic )
    if [[ -n "$topic" ]]; then
      cmd+=(--topic "$topic")
    fi
    if [[ -n "$chat_id" ]]; then
      cmd+=(--chat-id "$chat_id")
    fi
    cmd+=(--topic-subscribe "$topic_subscribe")
    exec "${cmd[@]}"
  elif [[ "$mode" == "4" ]]; then
    cmd=( "$py" "$ROOT_DIR/agent_client.py" --base-ws "$base_ws" --private-key "$private_key" --chat-type topic )
    if [[ -n "$topic" ]]; then
      cmd+=(--topic "$topic")
    fi
    if [[ -n "$chat_id" ]]; then
      cmd+=(--chat-id "$chat_id")
    fi
    cmd+=(--topic-unsubscribe "$topic_unsubscribe")
    exec "${cmd[@]}"
  else
    if [[ -n "$topic_subscribe" ]]; then
      cmd+=(--topic-subscribe "$topic_subscribe")
    fi
    cmd+=(--interactive)
    exec "${cmd[@]}"
  fi
}

create_new_agent() {
  local py out private_key agent_id agent_address
  if ! py="$(pick_python_for nacl)"; then
    echo "No Python runtime with nacl found."
    exit 1
  fi

  out="$($py "$ROOT_DIR/gen_agent_key.py")"
  private_key="$(echo "$out" | awk -F= '/^private_key=/{print $2}')"
  agent_id="$(echo "$out" | awk -F= '/^agent_id=/{print $2}')"
  agent_address="$(echo "$out" | awk -F= '/^agent_address=/{print $2}')"

  echo
  echo "New Agent Created"
  echo "private_key=${private_key}"
  echo "agent_id=${agent_id}"
  echo "agent_address=${agent_address}"
  echo
  echo "agent_client example:"
  cat <<EOF
$py $ROOT_DIR/agent_client.py \\
  --base-ws ws://${DEFAULT_SERVER_HOST}:${DEFAULT_SERVER_PORT} \\
  --private-key ${private_key} \\
  --peer-id <PEER_AGENT_ID> \\
  --interactive
EOF
  echo
  echo "nanobot channel config snippet:"
  cat <<EOF
"agentrelay": {
  "enabled": true,
  "serverUrl": "ws://${DEFAULT_SERVER_HOST}:${DEFAULT_SERVER_PORT}",
  "privateKey": "${private_key}",
  "agentId": "${agent_id}",
  "autoAllowFrom": ["<PEER_AGENT_ID>"],
  "allowFrom": []
}
EOF
}

show_config_and_acl() {
  local py
  py="$(pick_python_for sqlite3 || command -v python3)"

  ensure_config_file
  resolve_db_path
  seed_default_db

  echo "AgentRelay Paths"
  echo "project_root: $ROOT_DIR"
  echo "relay_file  : $ROOT_DIR/agent_relay.py"
  echo "web_file    : $ROOT_DIR/web_server.py"
  echo "config_file : $CONFIG_PATH"
  echo "nanobot_cfg: $NANOBOT_CONFIG_PATH"
  echo "db_path     : $DB_PATH"
  echo "python_bin  : $py"
  echo

  echo "AgentRelay Server Config"
  cat "$CONFIG_PATH"
  echo

  echo "Nanobot Channel Config Template"
  "$py" - <<PY
import json

auto_allow = [s for s in "${DEFAULT_BOT_AUTO_ALLOW_FROM}".split(",") if s]
data = {
    "channels": {
        "agentrelay": {
            "enabled": True,
            "serverUrl": "ws://${DEFAULT_SERVER_HOST}:${DEFAULT_SERVER_PORT}",
            "privateKey": "${DEFAULT_BOT_PRIVATE_KEY}",
            "agentId": "${DEFAULT_BOT_AGENT_ID}",
            "autoAllowFrom": auto_allow,
            "allowFrom": [],
        }
    }
}
print(json.dumps(data, ensure_ascii=False, indent=2))
PY
  echo
  echo "Current Bot Defaults"
  echo "bot_agent_id     : $DEFAULT_BOT_AGENT_ID"
  echo "bot_private_key  : $DEFAULT_BOT_PRIVATE_KEY"
  echo "bot_auto_allow   : $DEFAULT_BOT_AUTO_ALLOW_FROM"
  echo "default_server   : ws://${DEFAULT_SERVER_HOST}:${DEFAULT_SERVER_PORT}"
  echo

  echo "ACL Allow Rows"
  "$py" - <<PY
import sqlite3
from pathlib import Path
p = Path(r"$DB_PATH")
conn = sqlite3.connect(p)
try:
    cur = conn.cursor()
    cur.execute("SELECT owner_agent, sender_agent, created_at, updated_by_event_id FROM acl_allow ORDER BY created_at DESC")
    rows = cur.fetchall()
    if not rows:
        print("(empty)")
    else:
        for r in rows:
            print(f"owner={r[0]} sender={r[1]} created_at={r[2]} event={r[3]}")

    print("\nBlacklist Rows")
    cur.execute("SELECT owner_agent, blocked_agent, created_at, updated_by_event_id FROM blacklist ORDER BY created_at DESC")
    rows = cur.fetchall()
    if not rows:
        print("(empty)")
    else:
        for r in rows:
            print(f"owner={r[0]} blocked={r[1]} created_at={r[2]} event={r[3]}")
finally:
    conn.close()
PY
}

menu() {
  echo ""
  echo "AgentRelay Manage"
  echo "1) Start AgentRelay Server"
  echo "2) Stop AgentRelay Server"
  echo "3) Start AgentRelay Web"
  echo "4) Stop AgentRelay Web"
  echo "5) Start agent_client"
  echo "6) Create new Agent and print config"
  echo "7) Show AgentRelay config and ACL info"
  read -r -p "Choose [1-7]: " choice

  case "$choice" in
    1) start_agentrelay_server ;;
    2) stop_agentrelay_server ;;
    3) start_web_server ;;
    4) stop_web_server ;;
    5) start_agent_client ;;
    6) create_new_agent ;;
    7) show_config_and_acl ;;
    *) echo "Invalid option: $choice"; exit 1 ;;
  esac
}

case "${1:-menu}" in
  1|server)
    shift
    start_agentrelay_server "$@"
    ;;
  2|server-stop|stop-server) stop_agentrelay_server ;;
  3|web)
    shift
    start_web_server "$@"
    ;;
  4|web-stop|stop-web) stop_web_server ;;
  5|client)
    shift
    start_agent_client "$@"
    ;;
  6|new-agent) create_new_agent ;;
  7|info) show_config_and_acl ;;
  menu)
    shift || true
    menu
    ;;
  -h|--help|help)
    usage
    ;;
  *)
    usage
    exit 1
    ;;
esac
