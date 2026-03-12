#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PUBLIC_PORT="${PUBLIC_PORT:-8765}"
RELAY_PORT="${RELAY_PORT:-18765}"
WEB_PORT="${WEB_PORT:-18780}"
STATIC_ROOT="${STATIC_ROOT:-${ROOT_DIR}/static}"

yum install -y caddy

cp /etc/systemd/system/agentrelay.service /etc/systemd/system/agentrelay.service.bak
sed -i "s/--host 0.0.0.0 --port ${PUBLIC_PORT}/--host 127.0.0.1 --port ${RELAY_PORT}/" /etc/systemd/system/agentrelay.service
sed -i "s/--host 127.0.0.1 --port ${PUBLIC_PORT}/--host 127.0.0.1 --port ${RELAY_PORT}/" /etc/systemd/system/agentrelay.service

if [[ -f /etc/systemd/system/agentrelay-web.service ]]; then
  cp /etc/systemd/system/agentrelay-web.service /etc/systemd/system/agentrelay-web.service.bak
  sed -i "s/--host 0.0.0.0 --port 8780/--host 127.0.0.1 --port ${WEB_PORT}/" /etc/systemd/system/agentrelay-web.service
  sed -i "s/--host 127.0.0.1 --port 8780/--host 127.0.0.1 --port ${WEB_PORT}/" /etc/systemd/system/agentrelay-web.service
fi

cat > /etc/caddy/Caddyfile <<EOF
:${PUBLIC_PORT} {
    @relay_ws path /ws/*
    reverse_proxy @relay_ws 127.0.0.1:${RELAY_PORT}
    handle_path /static/* {
        root * ${STATIC_ROOT}
        file_server
    }
    reverse_proxy 127.0.0.1:${WEB_PORT}
}
EOF

caddy validate --config /etc/caddy/Caddyfile

systemctl daemon-reload
systemctl enable caddy
systemctl restart agentrelay
systemctl restart agentrelay-web
systemctl restart caddy

systemctl --no-pager --full status agentrelay | sed -n '1,40p'
systemctl --no-pager --full status agentrelay-web | sed -n '1,40p'
systemctl --no-pager --full status caddy | sed -n '1,40p'
ss -ltnp | grep -E ":${PUBLIC_PORT}|:${RELAY_PORT}|:${WEB_PORT}" || true
curl -sS "http://127.0.0.1:${PUBLIC_PORT}/health"
printf '\n'
curl -sS "http://127.0.0.1:${RELAY_PORT}/health"
printf '\n'
curl -sS "http://127.0.0.1:${WEB_PORT}/health"
