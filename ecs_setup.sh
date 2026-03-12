#!/usr/bin/env bash
set -euo pipefail

INSTALL_ROOT="${INSTALL_ROOT:-/opt/agentrelay}"
APP_DIR="$INSTALL_ROOT/AgentRelay"
VENV_DIR="$INSTALL_ROOT/.venv"
RELAY_PORT="${RELAY_PORT:-8765}"
WEB_PORT="${WEB_PORT:-8780}"
PUBLIC_HOST="${PUBLIC_HOST:-112.126.60.140}"
RELAY_DOMAIN="${RELAY_DOMAIN:-$PUBLIC_HOST}"

mkdir -p "$INSTALL_ROOT"

if [[ ! -d "$APP_DIR" ]]; then
  echo "Missing app directory: $APP_DIR" >&2
  exit 1
fi

if ! command -v python3.11 >/dev/null 2>&1; then
  yum install -y python3.11 python3.11-pip
fi

PYTHON_BIN="$(command -v python3.11)"
rm -rf "$VENV_DIR"
"$PYTHON_BIN" -m venv "$VENV_DIR"
"$VENV_DIR/bin/python" -m pip install --upgrade pip setuptools wheel
"$VENV_DIR/bin/python" -m pip install --index-url https://pypi.org/simple -r "$APP_DIR/requirements.txt"

RELAY_PRIV="$("$VENV_DIR/bin/python" -c 'from nacl.encoding import HexEncoder; from nacl.signing import SigningKey; print(SigningKey.generate().encode(encoder=HexEncoder).decode())')"

cat > /etc/agentrelay.env <<EOF
AGENTRELAY_DOMAIN=$RELAY_DOMAIN
AGENTRELAY_WS_BASE=ws://$PUBLIC_HOST:$RELAY_PORT
AGENTRELAY_FED_BASE=http://$PUBLIC_HOST:$RELAY_PORT
AGENTRELAY_PRIVATE_KEY=$RELAY_PRIV
EOF

cat > /etc/systemd/system/agentrelay.service <<EOF
[Unit]
Description=AgentRelay Relay Server
After=network.target

[Service]
Type=simple
WorkingDirectory=$APP_DIR
EnvironmentFile=/etc/agentrelay.env
ExecStart=$VENV_DIR/bin/python -m uvicorn agent_relay:app --host 0.0.0.0 --port $RELAY_PORT
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
EOF

cat > /etc/systemd/system/agentrelay-web.service <<EOF
[Unit]
Description=AgentRelay Web Server
After=network.target

[Service]
Type=simple
WorkingDirectory=$APP_DIR
EnvironmentFile=/etc/agentrelay.env
ExecStart=$VENV_DIR/bin/python -m uvicorn web_server:app --host 0.0.0.0 --port $WEB_PORT
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable --now agentrelay agentrelay-web

if systemctl is-active firewalld >/dev/null 2>&1; then
  firewall-cmd --permanent --add-port="${RELAY_PORT}/tcp"
  firewall-cmd --permanent --add-port="${WEB_PORT}/tcp"
  firewall-cmd --reload
fi

systemctl --no-pager --full status agentrelay | sed -n '1,80p'
systemctl --no-pager --full status agentrelay-web | sed -n '1,80p'
curl -sS "http://127.0.0.1:${RELAY_PORT}/health"
printf '\n'
curl -sS "http://127.0.0.1:${WEB_PORT}/health"
