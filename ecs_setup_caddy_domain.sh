#!/usr/bin/env bash
set -euo pipefail

DOMAIN="${DOMAIN:-lobs.cc}"
PUBLIC_PORT="${PUBLIC_PORT:-8765}"
APP_PORT="${APP_PORT:-18765}"

sed -i "s|^AGENTRELAY_DOMAIN=.*|AGENTRELAY_DOMAIN=${DOMAIN}|" /etc/agentrelay.env
sed -i "s|^AGENTRELAY_WS_BASE=.*|AGENTRELAY_WS_BASE=wss://${DOMAIN}|" /etc/agentrelay.env
sed -i "s|^AGENTRELAY_FED_BASE=.*|AGENTRELAY_FED_BASE=https://${DOMAIN}|" /etc/agentrelay.env

cat > /etc/caddy/Caddyfile <<EOF
${DOMAIN} {
    reverse_proxy 127.0.0.1:${APP_PORT}
}

:${PUBLIC_PORT} {
    reverse_proxy 127.0.0.1:${APP_PORT}
}
EOF

caddy validate --config /etc/caddy/Caddyfile

systemctl restart agentrelay
systemctl restart caddy

systemctl --no-pager --full status agentrelay | sed -n '1,40p'
systemctl --no-pager --full status caddy | sed -n '1,60p'
ss -ltnp | grep -E ":80|:443|:${PUBLIC_PORT}|:${APP_PORT}" || true
