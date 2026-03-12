#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DOMAIN="${DOMAIN:-lobs.cc}"
PUBLIC_PORT="${PUBLIC_PORT:-8765}"
RELAY_PORT="${RELAY_PORT:-18765}"
WEB_PORT="${WEB_PORT:-18780}"
STATIC_ROOT="${STATIC_ROOT:-${ROOT_DIR}/static}"

sed -i "s|^AGENTRELAY_DOMAIN=.*|AGENTRELAY_DOMAIN=${DOMAIN}|" /etc/agentrelay.env
sed -i "s|^AGENTRELAY_WS_BASE=.*|AGENTRELAY_WS_BASE=wss://${DOMAIN}|" /etc/agentrelay.env
sed -i "s|^AGENTRELAY_FED_BASE=.*|AGENTRELAY_FED_BASE=https://${DOMAIN}|" /etc/agentrelay.env

cat > /etc/caddy/Caddyfile <<EOF
${DOMAIN} {
    @relay_ws path /ws/*
    reverse_proxy @relay_ws 127.0.0.1:${RELAY_PORT}
    handle_path /static/* {
        root * ${STATIC_ROOT}
        file_server
    }
    reverse_proxy 127.0.0.1:${WEB_PORT}
}

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

systemctl restart agentrelay
systemctl restart agentrelay-web
systemctl restart caddy

systemctl --no-pager --full status agentrelay | sed -n '1,40p'
systemctl --no-pager --full status agentrelay-web | sed -n '1,40p'
systemctl --no-pager --full status caddy | sed -n '1,60p'
ss -ltnp | grep -E ":80|:443|:${PUBLIC_PORT}|:${RELAY_PORT}|:${WEB_PORT}" || true
