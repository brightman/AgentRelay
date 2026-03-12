#!/usr/bin/env bash
set -euo pipefail

PUBLIC_PORT="${PUBLIC_PORT:-8765}"
APP_PORT="${APP_PORT:-18765}"

yum install -y caddy

cp /etc/systemd/system/agentrelay.service /etc/systemd/system/agentrelay.service.bak
sed -i "s/--host 0.0.0.0 --port ${PUBLIC_PORT}/--host 127.0.0.1 --port ${APP_PORT}/" /etc/systemd/system/agentrelay.service
sed -i "s/--host 127.0.0.1 --port ${PUBLIC_PORT}/--host 127.0.0.1 --port ${APP_PORT}/" /etc/systemd/system/agentrelay.service

cat > /etc/caddy/Caddyfile <<EOF
:${PUBLIC_PORT} {
    reverse_proxy 127.0.0.1:${APP_PORT}
}
EOF

caddy validate --config /etc/caddy/Caddyfile

systemctl daemon-reload
systemctl enable caddy
systemctl restart agentrelay
systemctl restart caddy

systemctl --no-pager --full status agentrelay | sed -n '1,40p'
systemctl --no-pager --full status caddy | sed -n '1,40p'
ss -ltnp | grep -E ":${PUBLIC_PORT}|:${APP_PORT}" || true
curl -sS "http://127.0.0.1:${PUBLIC_PORT}/health"
curl -sS "http://127.0.0.1:${APP_PORT}/health"
