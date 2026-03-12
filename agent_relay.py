import base64
import hashlib
import html
import json
import os
import secrets
import sqlite3
import time
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional

from fastapi import Cookie, FastAPI, Form, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from nacl.encoding import HexEncoder
from nacl.signing import SigningKey, VerifyKey

from identity import format_agent_ref, normalize_agent_id, parse_agent_address

DB_PATH = Path("channel.db")
LOGO_PATH = Path("lobs.cc.png")
SYSTEM_CHAT_TYPE = "system"
DM_CHAT_TYPE = "dm"
TOPIC_CHAT_TYPE = "topic"
AUTH_WINDOW_SECONDS = 60
RELAY_DOMAIN = os.getenv("AGENTRELAY_DOMAIN", os.getenv("AGENTHUB_RELAY_DOMAIN", "local.agentrelay")).strip().lower()
RELAY_WS_BASE = os.getenv("AGENTRELAY_WS_BASE", os.getenv("AGENTHUB_RELAY_WS_BASE", "ws://127.0.0.1:8000")).strip()
RELAY_FED_BASE = os.getenv("AGENTRELAY_FED_BASE", os.getenv("AGENTHUB_RELAY_FED_BASE", "http://127.0.0.1:8000")).strip()
RELAY_PRIVATE_KEY_HEX = os.getenv("AGENTRELAY_PRIVATE_KEY", os.getenv("AGENTHUB_RELAY_PRIVATE_KEY", "")).strip()
RELAY_DIRECTORY = json.loads(os.getenv("AGENTRELAY_DIRECTORY", os.getenv("AGENTHUB_RELAY_DIRECTORY", "{}")) or "{}")
app = FastAPI(title="Signed AgentRelay Server")


@dataclass
class Session:
    ws: WebSocket
    connected_at: int


@dataclass
class RelaySession:
    ws: WebSocket
    relay_id: str
    relay_domain: str
    connected_at: int


sessions: Dict[str, Session] = {}
relay_sessions: Dict[str, RelaySession] = {}
web_sessions: Dict[str, str] = {}
pending_web_logins: Dict[str, dict[str, Any]] = {}
relay_started_at = int(time.time())

if RELAY_PRIVATE_KEY_HEX:
    _relay_signing_key = SigningKey(RELAY_PRIVATE_KEY_HEX, encoder=HexEncoder)
    RELAY_ID = _relay_signing_key.verify_key.encode(encoder=HexEncoder).decode()
else:
    _relay_signing_key = None
    RELAY_ID = ""


def now_ts() -> int:
    return int(time.time())


def _json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"))


def _json_loads(value: str, default: Any) -> Any:
    if not value:
        return default
    return json.loads(value)


def relay_discovery_payload() -> dict[str, Any]:
    payload: dict[str, Any] = {
        "relay_domain": RELAY_DOMAIN,
        "relay_id": RELAY_ID,
        "ws_endpoint": f"{RELAY_WS_BASE.rstrip('/')}/ws/agent",
        "fed_endpoint": f"{RELAY_FED_BASE.rstrip('/')}/federation",
        "fed_ws_endpoint": f"{RELAY_WS_BASE.rstrip('/')}/ws/federation",
        "supported_versions": ["agentrelay/1"],
        "created_at": relay_started_at,
    }
    if _relay_signing_key:
        payload_bytes = _json_dumps(payload).encode("utf-8")
        payload["sig"] = base64.b64encode(_relay_signing_key.sign(payload_bytes).signature).decode("ascii")
    return payload


def relay_sign_b64(payload: bytes) -> str:
    if not _relay_signing_key:
        raise RuntimeError("relay signing key is not configured")
    return base64.b64encode(_relay_signing_key.sign(payload).signature).decode("ascii")


def relay_directory_base(relay_domain: str) -> str:
    base = RELAY_DIRECTORY.get(relay_domain)
    if not isinstance(base, str) or not base.strip():
        raise ValueError(f"relay directory missing entry for {relay_domain}")
    return base.rstrip("/")


def fetch_remote_relay_discovery(relay_domain: str) -> dict[str, Any]:
    base = relay_directory_base(relay_domain)
    with urllib.request.urlopen(f"{base}/v1/relay", timeout=3) as resp:
        payload = json.loads(resp.read().decode("utf-8"))
    if payload.get("relay_domain") != relay_domain:
        raise ValueError("relay discovery domain mismatch")
    relay_id = payload.get("relay_id")
    sig = payload.get("sig", "")
    if not relay_id or not sig:
        raise ValueError("relay discovery missing signature")
    unsigned = dict(payload)
    unsigned.pop("sig", None)
    if not verify_sig(relay_id, _json_dumps(unsigned).encode("utf-8"), sig):
        raise ValueError("relay discovery signature invalid")
    return payload


def make_dm_chat_id(agent_a: str, agent_b: str) -> str:
    left, right = sorted([normalize_agent_id(agent_a), normalize_agent_id(agent_b)])
    return f"dm:{left}:{right}"


def normalize_chat(event: dict) -> dict[str, Any]:
    chat = event.get("chat")
    if not isinstance(chat, dict):
        raise ValueError("chat object is required")
    chat_id = chat.get("id")
    chat_type = chat.get("type")
    if not isinstance(chat_id, str) or not chat_id:
        raise ValueError("chat.id must be a non-empty string")
    if not isinstance(chat_type, str) or not chat_type:
        raise ValueError("chat.type must be a non-empty string")
    normalized = {"id": chat_id, "type": chat_type}
    title = chat.get("title")
    if isinstance(title, str) and title:
        normalized["title"] = title
    return normalized


def content_type_for_event(event: dict) -> str:
    value = event.get("content_type") or "text/plain"
    if not isinstance(value, str):
        raise ValueError("content_type must be a string")
    return value


def normalized_attachments(event: dict) -> list[dict[str, Any]]:
    attachments = event.get("attachments", [])
    if not isinstance(attachments, list):
        raise ValueError("attachments must be a list")
    normalized: list[dict[str, Any]] = []
    for item in attachments:
        if not isinstance(item, dict):
            raise ValueError("attachment entries must be objects")
        normalized.append(item)
    return normalized


def normalized_metadata(event: dict) -> dict[str, Any]:
    metadata = event.get("metadata", {})
    if metadata is None:
        return {}
    if not isinstance(metadata, dict):
        raise ValueError("metadata must be an object")
    return metadata


def extension_hash(event: dict) -> str:
    ext = {
        "content_type": content_type_for_event(event),
        "attachments": normalized_attachments(event),
        "metadata": normalized_metadata(event),
    }
    return hashlib.sha256(_json_dumps(ext).encode("utf-8")).hexdigest()


def canonical_event_payload(event: dict) -> bytes:
    content_hash = hashlib.sha256(event["content"].encode("utf-8")).hexdigest()
    chat = normalize_chat(event)
    payload_parts = [
        event["id"],
        event["from"],
        chat["id"],
        chat["type"],
        event["kind"],
        str(event["created_at"]),
        content_hash,
        extension_hash(event),
    ]
    return "|".join(payload_parts).encode("utf-8")


def canonical_federated_payload(packet: dict) -> bytes:
    origin = packet.get("origin_relay") or {}
    destination = packet.get("destination_relay") or {}
    event = packet.get("event") or {}
    content = event.get("content", "") if isinstance(event, dict) else ""
    ext_hash = extension_hash(event) if isinstance(event, dict) and isinstance(event.get("content"), str) else ""
    parts = [
        origin.get("domain", ""),
        origin.get("relay_id", ""),
        destination.get("domain", ""),
        event.get("id", ""),
        event.get("from", ""),
        event.get("from_address", ""),
        event.get("to_address", ""),
        event.get("chat", {}).get("id", ""),
        event.get("chat", {}).get("type", ""),
        event.get("kind", ""),
        str(event.get("created_at", "")),
        hashlib.sha256(content.encode("utf-8")).hexdigest(),
        ext_hash,
    ]
    return "|".join(parts).encode("utf-8")


def verify_sig(pubkey_hex: str, payload: bytes, sig_b64: str) -> bool:
    try:
        verify_key = VerifyKey(normalize_agent_id(pubkey_hex), encoder=HexEncoder)
        sig = base64.b64decode(sig_b64)
        verify_key.verify(payload, sig)
        return True
    except Exception:
        return False


def init_db() -> None:
    conn = sqlite3.connect(DB_PATH)
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
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS messages_v2 (
                event_id TEXT PRIMARY KEY,
                kind TEXT NOT NULL,
                chat_id TEXT NOT NULL,
                chat_type TEXT NOT NULL,
                from_id TEXT NOT NULL,
                text TEXT NOT NULL,
                content_type TEXT NOT NULL DEFAULT 'text/plain',
                attachments_json TEXT NOT NULL DEFAULT '[]',
                metadata_json TEXT NOT NULL DEFAULT '{}',
                created_at INTEGER NOT NULL,
                sig TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS deliveries_v2 (
                event_id TEXT NOT NULL,
                agent_id TEXT NOT NULL,
                status TEXT NOT NULL,
                delivered_at INTEGER,
                read_at INTEGER,
                PRIMARY KEY (event_id, agent_id)
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS topic_subscriptions (
                topic TEXT NOT NULL,
                agent_id TEXT NOT NULL,
                created_at INTEGER NOT NULL,
                updated_by_event_id TEXT NOT NULL,
                PRIMARY KEY (topic, agent_id)
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS agent_directory (
                agent_id TEXT PRIMARY KEY,
                agent_address TEXT NOT NULL,
                visible INTEGER NOT NULL DEFAULT 1,
                profile_json TEXT NOT NULL DEFAULT '{}',
                created_at INTEGER NOT NULL,
                updated_at INTEGER NOT NULL,
                last_seen_at INTEGER
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_deliveries_agent_status ON deliveries_v2(agent_id, status)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_messages_chat_created_v2 ON messages_v2(chat_id, created_at)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_messages_from_created_v2 ON messages_v2(from_id, created_at)"
        )
        conn.commit()
    finally:
        conn.close()


def set_acl_allow(owner_agent: str, sender_agent: str, allowed: bool, event_id: str) -> None:
    conn = sqlite3.connect(DB_PATH)
    try:
        if allowed:
            conn.execute(
                """
                INSERT INTO acl_allow(owner_agent, sender_agent, created_at, updated_by_event_id)
                VALUES(?,?,?,?)
                ON CONFLICT(owner_agent, sender_agent)
                DO UPDATE SET created_at=excluded.created_at, updated_by_event_id=excluded.updated_by_event_id
                """,
                (owner_agent, sender_agent, now_ts(), event_id),
            )
        else:
            conn.execute(
                "DELETE FROM acl_allow WHERE owner_agent=? AND sender_agent=?",
                (owner_agent, sender_agent),
            )
        conn.commit()
    finally:
        conn.close()


def set_blacklist(owner_agent: str, blocked_agent: str, blocked: bool, event_id: str) -> None:
    conn = sqlite3.connect(DB_PATH)
    try:
        if blocked:
            conn.execute(
                """
                INSERT INTO blacklist(owner_agent, blocked_agent, created_at, updated_by_event_id)
                VALUES(?,?,?,?)
                ON CONFLICT(owner_agent, blocked_agent)
                DO UPDATE SET created_at=excluded.created_at, updated_by_event_id=excluded.updated_by_event_id
                """,
                (owner_agent, blocked_agent, now_ts(), event_id),
            )
        else:
            conn.execute(
                "DELETE FROM blacklist WHERE owner_agent=? AND blocked_agent=?",
                (owner_agent, blocked_agent),
            )
        conn.commit()
    finally:
        conn.close()


def is_allowed_for_message(sender_agent: str, to_agent: str) -> bool:
    conn = sqlite3.connect(DB_PATH)
    try:
        row = conn.execute(
            "SELECT 1 FROM acl_allow WHERE owner_agent=? AND sender_agent=?",
            (to_agent, sender_agent),
        ).fetchone()
        return row is not None
    finally:
        conn.close()


def is_blacklisted(sender_agent: str, to_agent: str) -> bool:
    conn = sqlite3.connect(DB_PATH)
    try:
        row = conn.execute(
            "SELECT 1 FROM blacklist WHERE owner_agent=? AND blocked_agent=?",
            (to_agent, sender_agent),
        ).fetchone()
        return row is not None
    finally:
        conn.close()


def set_topic_subscription(topic: str, agent_id: str, subscribed: bool, event_id: str) -> None:
    conn = sqlite3.connect(DB_PATH)
    try:
        if subscribed:
            conn.execute(
                """
                INSERT INTO topic_subscriptions(topic, agent_id, created_at, updated_by_event_id)
                VALUES(?,?,?,?)
                ON CONFLICT(topic, agent_id)
                DO UPDATE SET created_at=excluded.created_at, updated_by_event_id=excluded.updated_by_event_id
                """,
                (topic, agent_id, now_ts(), event_id),
            )
        else:
            conn.execute(
                "DELETE FROM topic_subscriptions WHERE topic=? AND agent_id=?",
                (topic, agent_id),
            )
        conn.commit()
    finally:
        conn.close()


def is_topic_subscriber(topic: str, agent_id: str) -> bool:
    conn = sqlite3.connect(DB_PATH)
    try:
        row = conn.execute(
            "SELECT 1 FROM topic_subscriptions WHERE topic=? AND agent_id=?",
            (topic, agent_id),
        ).fetchone()
        return row is not None
    finally:
        conn.close()


def list_topic_subscribers(topic: str) -> list[str]:
    conn = sqlite3.connect(DB_PATH)
    try:
        rows = conn.execute(
            "SELECT agent_id FROM topic_subscriptions WHERE topic=? ORDER BY created_at ASC",
            (topic,),
        ).fetchall()
        return [row[0] for row in rows]
    finally:
        conn.close()


def normalize_event(event: dict) -> dict[str, Any]:
    if not isinstance(event.get("content"), str):
        raise ValueError("content must be a string")
    chat = normalize_chat(event)
    normalized = {
        "id": event["id"],
        "from": event["from"],
        "chat": chat,
        "kind": event["kind"],
        "created_at": int(event["created_at"]),
        "content": event["content"],
        "content_type": content_type_for_event(event),
        "attachments": normalized_attachments(event),
        "metadata": normalized_metadata(event),
    }
    return normalized


def parse_dm_peer(chat_id: str, sender_agent: str) -> Optional[str]:
    parts = chat_id.split(":")
    if len(parts) != 3 or parts[0] != "dm":
        return None
    _, left, right = parts
    if sender_agent == left:
        return right
    if sender_agent == right:
        return left
    return None


def event_recipients(event: dict) -> list[str]:
    chat = event["chat"]
    if chat["type"] == DM_CHAT_TYPE:
        peer = parse_dm_peer(chat["id"], event["from"])
        return [peer] if peer else []
    if chat["type"] == TOPIC_CHAT_TYPE:
        subscribers = list_topic_subscribers(chat["id"])
        return [agent for agent in subscribers if agent != event["from"]]
    return []


def create_message(event: dict, sig_b64: str, recipients: list[str]) -> None:
    upsert_agent_directory(event["from"], last_seen=True)
    for recipient in recipients:
        upsert_agent_directory(recipient)
    conn = sqlite3.connect(DB_PATH)
    try:
        conn.execute(
            """
            INSERT INTO messages_v2(
                event_id, kind, chat_id, chat_type, from_id, text, content_type, attachments_json, metadata_json,
                created_at, sig
            )
            VALUES(?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                event["id"],
                event["kind"],
                event["chat"]["id"],
                event["chat"]["type"],
                event["from"],
                event["content"],
                event["content_type"],
                _json_dumps(event["attachments"]),
                _json_dumps(event["metadata"]),
                event["created_at"],
                sig_b64,
            ),
        )
        if recipients:
            conn.executemany(
                """
                INSERT INTO deliveries_v2(event_id, agent_id, status)
                VALUES(?,?,'queued')
                ON CONFLICT(event_id, agent_id) DO NOTHING
                """,
                [(event["id"], recipient) for recipient in recipients],
            )
        conn.commit()
    finally:
        conn.close()


def _sender_address_for_item(from_id: str, metadata: dict[str, Any]) -> str:
    if isinstance(metadata, dict):
        for key in ("_agentrelay", "_agenthub"):
            relay_meta = metadata.get(key)
            if isinstance(relay_meta, dict):
                from_address = relay_meta.get("from_address")
                if isinstance(from_address, str) and from_address:
                    return from_address
    return format_agent_ref(from_id, RELAY_DOMAIN)["agent_address"]


def _outbound_to_address(metadata: dict[str, Any]) -> str:
    if not isinstance(metadata, dict):
        return ""
    for key in ("agentrelay", "agenthub"):
        relay_meta = metadata.get(key)
        if isinstance(relay_meta, dict):
            to_address = relay_meta.get("to_address")
            if isinstance(to_address, str) and to_address.strip():
                return to_address.strip().lower()
    return ""


def mark_delivered(message_id: str, agent_id: str) -> None:
    conn = sqlite3.connect(DB_PATH)
    try:
        conn.execute(
            "UPDATE deliveries_v2 SET status='delivered', delivered_at=? WHERE event_id=? AND agent_id=?",
            (now_ts(), message_id, agent_id),
        )
        conn.commit()
    finally:
        conn.close()


def mark_read(message_id: str, agent_id: str) -> None:
    conn = sqlite3.connect(DB_PATH)
    try:
        conn.execute(
            "UPDATE deliveries_v2 SET status='read', read_at=? WHERE event_id=? AND agent_id=?",
            (now_ts(), message_id, agent_id),
        )
        conn.commit()
    finally:
        conn.close()


def pending_for_agent(agent_id: str, limit: int = 100) -> list[dict]:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            """
            SELECT m.event_id, m.kind, m.chat_id, m.chat_type, m.from_id, m.text, m.content_type,
                   m.attachments_json, m.metadata_json, m.created_at, m.sig
            FROM deliveries_v2 d
            JOIN messages_v2 m ON m.event_id = d.event_id
            WHERE d.agent_id=? AND d.status='queued'
            ORDER BY m.created_at ASC
            LIMIT ?
            """,
            (agent_id, limit),
        ).fetchall()
        return [dict(row) for row in rows]
    finally:
        conn.close()


def list_messages(agent_id: str, peer_id: str = "", chat_id: str = "", since_ts: int = 0, limit: int = 200) -> list[dict]:
    if chat_id:
        target_chat_id = chat_id
    elif peer_id:
        target_chat_id = make_dm_chat_id(agent_id, peer_id)
    else:
        raise ValueError("chat_id or peer_id is required")

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            """
            SELECT m.event_id AS id, m.kind, m.chat_id, m.chat_type, m.from_id, m.text, m.content_type,
                   m.attachments_json, m.metadata_json, m.created_at, m.sig,
                   d.status, d.delivered_at, d.read_at
            FROM messages_v2 m
            LEFT JOIN deliveries_v2 d
              ON d.event_id = m.event_id AND d.agent_id = ?
            WHERE m.chat_id = ?
              AND m.created_at >= ?
              AND (m.from_id = ? OR d.agent_id = ?)
            ORDER BY m.created_at ASC
            LIMIT ?
            """,
            (agent_id, target_chat_id, since_ts, agent_id, agent_id, limit),
        ).fetchall()
        items = []
        for row in rows:
            item = dict(row)
            item["attachments"] = _json_loads(item.pop("attachments_json", "[]"), [])
            item["metadata"] = _json_loads(item.pop("metadata_json", "{}"), {})
            item["from_address"] = _sender_address_for_item(item["from_id"], item["metadata"])
            item["agent_address"] = format_agent_ref(agent_id, RELAY_DOMAIN)["agent_address"]
            if item.get("status") is None:
                item["status"] = "sent"
            items.append(item)
        return items
    finally:
        conn.close()


def list_topics() -> list[dict[str, Any]]:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            """
            WITH known_topics AS (
                SELECT chat_id AS topic_id
                FROM messages_v2
                WHERE chat_type = ?
                UNION
                SELECT topic AS topic_id
                FROM topic_subscriptions
            )
            SELECT
                k.topic_id,
                COALESCE(m.message_count, 0) AS message_count,
                COALESCE(s.subscriber_count, 0) AS subscriber_count,
                COALESCE(m.last_created_at, 0) AS last_created_at
            FROM known_topics k
            LEFT JOIN (
                SELECT chat_id, COUNT(*) AS message_count, MAX(created_at) AS last_created_at
                FROM messages_v2
                WHERE chat_type = ?
                GROUP BY chat_id
            ) m ON m.chat_id = k.topic_id
            LEFT JOIN (
                SELECT topic, COUNT(*) AS subscriber_count
                FROM topic_subscriptions
                GROUP BY topic
            ) s ON s.topic = k.topic_id
            ORDER BY last_created_at DESC, topic_id ASC
            """,
            (TOPIC_CHAT_TYPE, TOPIC_CHAT_TYPE),
        ).fetchall()
        return [dict(row) for row in rows]
    finally:
        conn.close()


def list_agent_topics(agent_id: str) -> list[str]:
    conn = sqlite3.connect(DB_PATH)
    try:
        rows = conn.execute(
            "SELECT topic FROM topic_subscriptions WHERE agent_id=? ORDER BY created_at ASC",
            (agent_id,),
        ).fetchall()
        return [row[0] for row in rows]
    finally:
        conn.close()


def list_topic_messages(topic_id: str, limit: int = 200) -> list[dict[str, Any]]:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            """
            SELECT event_id, from_id, text, content_type, attachments_json, metadata_json, created_at, sig
            FROM messages_v2
            WHERE chat_type = ? AND chat_id = ?
            ORDER BY created_at ASC
            LIMIT ?
            """,
            (TOPIC_CHAT_TYPE, topic_id, limit),
        ).fetchall()
        items: list[dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            item["attachments"] = _json_loads(item.pop("attachments_json", "[]"), [])
            item["metadata"] = _json_loads(item.pop("metadata_json", "{}"), {})
            item["from_address"] = _sender_address_for_item(item["from_id"], item["metadata"])
            items.append(item)
        return items
    finally:
        conn.close()


def upsert_agent_directory(agent_id: str, *, last_seen: bool = False) -> None:
    agent_ref = format_agent_ref(agent_id, RELAY_DOMAIN)
    conn = sqlite3.connect(DB_PATH)
    try:
        ts = now_ts()
        conn.execute(
            """
            INSERT INTO agent_directory(agent_id, agent_address, visible, profile_json, created_at, updated_at, last_seen_at)
            VALUES(?, ?, 1, '{}', ?, ?, ?)
            ON CONFLICT(agent_id) DO UPDATE SET
                agent_address=excluded.agent_address,
                updated_at=excluded.updated_at,
                last_seen_at=CASE
                    WHEN excluded.last_seen_at IS NULL THEN agent_directory.last_seen_at
                    ELSE excluded.last_seen_at
                END
            """,
            (agent_id, agent_ref["agent_address"], ts, ts, ts if last_seen else None),
        )
        conn.commit()
    finally:
        conn.close()


def list_visible_agents() -> list[dict[str, Any]]:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            """
            WITH discovered(agent_id) AS (
                SELECT agent_id FROM agent_directory
                UNION
                SELECT from_id FROM messages_v2
                UNION
                SELECT owner_agent FROM acl_allow
                UNION
                SELECT sender_agent FROM acl_allow
                UNION
                SELECT owner_agent FROM blacklist
                UNION
                SELECT blocked_agent FROM blacklist
                UNION
                SELECT agent_id FROM topic_subscriptions
            )
            SELECT
                d.agent_id,
                COALESCE(ad.agent_address, '') AS agent_address,
                COALESCE(ad.visible, 1) AS visible,
                ad.last_seen_at,
                COALESCE(ts.topic_count, 0) AS topic_count,
                COALESCE(msg.message_count, 0) AS message_count
            FROM discovered d
            LEFT JOIN agent_directory ad ON ad.agent_id = d.agent_id
            LEFT JOIN (
                SELECT agent_id, COUNT(*) AS topic_count
                FROM topic_subscriptions
                GROUP BY agent_id
            ) ts ON ts.agent_id = d.agent_id
            LEFT JOIN (
                SELECT from_id AS agent_id, COUNT(*) AS message_count
                FROM messages_v2
                GROUP BY from_id
            ) msg ON msg.agent_id = d.agent_id
            WHERE COALESCE(ad.visible, 1) = 1
            ORDER BY d.agent_id ASC
            """
        ).fetchall()
        items: list[dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            if not item["agent_address"]:
                item["agent_address"] = format_agent_ref(item["agent_id"], RELAY_DOMAIN)["agent_address"]
            item["online"] = item["agent_id"] in sessions
            items.append(item)
        return items
    finally:
        conn.close()


def agent_can_view_topic(agent_id: str, topic_id: str) -> bool:
    return is_topic_subscriber(topic_id, agent_id)


def current_web_agent(session_token: str | None) -> str:
    if not session_token:
        return ""
    return web_sessions.get(session_token, "")


def current_web_agent_address(session_token: str | None) -> str:
    agent_id = current_web_agent(session_token)
    if not agent_id:
        return ""
    return format_agent_ref(agent_id, RELAY_DOMAIN)["agent_address"]


async def deliver_system_message(agent_id: str, content: str) -> bool:
    if not RELAY_ID or not _relay_signing_key:
        return False
    session = sessions.get(agent_id)
    if not session:
        return False
    event = {
        "id": secrets.token_hex(16),
        "from": RELAY_ID,
        "chat": {"id": f"system:{agent_id}", "type": SYSTEM_CHAT_TYPE},
        "kind": "message",
        "created_at": now_ts(),
        "content": content,
        "content_type": "text/plain",
        "attachments": [],
        "metadata": {
            "_agentrelay": {
                "from_address": format_agent_ref(RELAY_ID, RELAY_DOMAIN)["agent_address"],
            }
        },
    }
    sig = relay_sign_b64(canonical_event_payload(event))
    return await safe_send(session.ws, {"type": "deliver", "event": event, "sig": sig})


def html_page(title: str, body: str) -> HTMLResponse:
    doc = f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{html.escape(title)}</title>
  <style>
    :root {{
      color-scheme: light;
      --bg: #f3efe5;
      --bg-accent: #faf2db;
      --card: #fffdf7;
      --card-strong: #fff3c7;
      --line: #d9cbab;
      --text: #221d14;
      --muted: #6f6656;
      --accent: #ff7a18;
      --accent-2: #1456ff;
      --teal: #13b7c4;
      --danger: #a12626;
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
      background:
        radial-gradient(circle at top left, rgba(255, 193, 64, 0.28), transparent 28%),
        radial-gradient(circle at top right, rgba(19, 183, 196, 0.18), transparent 24%),
        linear-gradient(180deg, var(--bg-accent) 0%, var(--bg) 100%);
      color: var(--text);
    }}
    .wrap {{ max-width: 1220px; margin: 0 auto; padding: 26px 24px 32px; }}
    .site-header {{ display:flex; justify-content:space-between; gap:20px; align-items:flex-start; margin-bottom:18px; }}
    .hero {{ display:grid; grid-template-columns: 1.4fr 0.8fr; gap:18px; margin-bottom:18px; }}
    .card {{
      background: var(--card);
      border: 1px solid var(--line);
      border-radius: 24px;
      padding: 18px;
      box-shadow: 0 8px 28px rgba(45, 39, 25, 0.07);
    }}
    .hero-card {{
      padding: 24px;
      background:
        linear-gradient(135deg, rgba(255, 240, 189, 0.86), rgba(255, 253, 247, 0.98)),
        var(--card);
      overflow: hidden;
      position: relative;
    }}
    .hero-card::after {{
      content: "";
      position: absolute;
      right: -40px;
      bottom: -50px;
      width: 220px;
      height: 220px;
      border-radius: 50%;
      background: radial-gradient(circle, rgba(19, 183, 196, 0.18), transparent 65%);
    }}
    .hero-layout {{ display:grid; grid-template-columns: 220px 1fr; gap:20px; align-items:center; }}
    .logo-box {{
      width: 220px;
      height: 220px;
      border-radius: 28px;
      background: linear-gradient(180deg, #ffffff, #fff6d2);
      border: 1px solid rgba(217, 203, 171, 0.8);
      display:flex;
      align-items:center;
      justify-content:center;
      overflow:hidden;
    }}
    .logo-box img {{ width:100%; height:100%; object-fit:cover; }}
    .logo-fallback {{ font-size:92px; line-height:1; }}
    h1, h2, h3 {{ margin: 0 0 12px; }}
    h1 {{ font-size: 34px; line-height: 1.08; letter-spacing: -.03em; }}
    h2 {{ font-size: 18px; }}
    h3 {{ font-size: 15px; color: var(--muted); text-transform: uppercase; letter-spacing: .06em; }}
    p, li, label {{ line-height: 1.5; }}
    .muted {{ color: var(--muted); }}
    .kv {{ margin: 0; }}
    .kv dt {{ font-size: 12px; color: var(--muted); margin-top: 10px; }}
    .kv dd {{ margin: 2px 0 0; word-break: break-all; }}
    .topic-list, .msg-list {{ list-style: none; margin: 0; padding: 0; }}
    .topic-list li, .msg-list li {{ border-top: 1px solid var(--line); padding: 12px 0; }}
    .topic-list li:first-child, .msg-list li:first-child {{ border-top: 0; padding-top: 0; }}
    a {{ color: var(--accent-2); text-decoration: none; }}
    a:hover {{ text-decoration: underline; }}
    code {{
      font-family: inherit;
      background: rgba(255,255,255,0.66);
      padding: 2px 6px;
      border-radius: 8px;
    }}
    input, button {{
      width: 100%;
      border: 1px solid var(--line);
      border-radius: 10px;
      padding: 10px 12px;
      font: inherit;
      background: white;
    }}
    button {{
      cursor: pointer;
      background: var(--accent);
      color: white;
      border-color: var(--accent);
    }}
    .danger {{
      background: white;
      color: var(--danger);
      border-color: var(--danger);
    }}
    .row {{ display: grid; gap: 10px; }}
    .summary-grid {{ display:grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap:12px; margin-top:14px; }}
    .summary-tile {{ border:1px solid var(--line); border-radius:16px; padding:14px; background:rgba(255,255,255,0.55); }}
    .summary-tile strong {{ display:block; font-size:24px; margin-top:4px; }}
    .login-shell {{ min-width: 330px; max-width: 360px; }}
    .login-state {{ text-align:right; font-size:13px; color:var(--muted); margin-bottom:8px; }}
    .meta {{ font-size: 12px; color: var(--muted); margin-bottom: 8px; }}
    pre {{
      margin: 10px 0 0;
      padding: 12px;
      background: #f7f3e8;
      border: 1px solid var(--line);
      border-radius: 12px;
      white-space: pre-wrap;
      word-break: break-word;
    }}
    .stat-link {{ display:inline-flex; align-items:center; gap:8px; color:inherit; font-weight:700; }}
    .badge-dot {{ display:inline-block; width:10px; height:10px; border-radius:50%; background: var(--teal); }}
    .badge {{
      display: inline-block;
      padding: 2px 8px;
      border-radius: 999px;
      background: #fff0d1;
      color: #7b4d08;
      font-size: 12px;
      margin-left: 8px;
    }}
    .topic-board {{ margin-top:18px; padding:22px; width:100%; }}
    .topic-board h2 {{ display:flex; align-items:center; justify-content:space-between; gap:12px; }}
    .footer {{
      margin-top: 20px;
      padding: 18px 22px;
      display: flex;
      justify-content: space-between;
      gap: 16px;
      flex-wrap: wrap;
      border-radius: 20px;
      background: rgba(255,255,255,0.66);
      border: 1px solid var(--line);
    }}
    .agents-table {{ width:100%; border-collapse:collapse; font-size:14px; }}
    .agents-table th, .agents-table td {{
      text-align:left;
      padding:12px 10px;
      border-bottom:1px solid var(--line);
      vertical-align:top;
    }}
    .status-pill {{
      display:inline-flex;
      align-items:center;
      gap:8px;
      padding:4px 10px;
      border-radius:999px;
      font-size:12px;
      border:1px solid var(--line);
      background:white;
    }}
    .status-pill.offline .badge-dot {{ background:#c2b388; }}
    @media (max-width: 900px) {{
      .site-header, .hero, .hero-layout {{ display:grid; grid-template-columns:1fr; }}
      .login-shell {{ min-width:0; max-width:none; }}
      .summary-grid {{ grid-template-columns:1fr; }}
      .logo-box {{ width:180px; height:180px; }}
    }}
  </style>
</head>
<body>
  <div class="wrap">{body}</div>
</body>
</html>"""
    return HTMLResponse(doc)


def render_home(
    agent_id: str = "",
    *,
    error: str = "",
    pending_token: str = "",
    pending_address: str = "",
) -> HTMLResponse:
    topics = list_topics()
    agents = list_visible_agents()
    discovery = relay_discovery_payload()
    logged_in_address = format_agent_ref(agent_id, RELAY_DOMAIN)["agent_address"] if agent_id else ""
    has_logo = LOGO_PATH.exists()
    topic_items = []
    for topic in topics:
        topic_id = html.escape(topic["topic_id"])
        topic_items.append(
            f"""<li>
  <div><a href="/topic?chat_id={topic_id}">{topic_id}</a></div>
  <div class="meta">messages={topic["message_count"]} subscribers={topic["subscriber_count"]}</div>
</li>"""
        )
    if not topic_items:
        topic_items.append('<li class="muted">No topic yet.</li>')

    login_panel = ""
    if agent_id:
        my_topics = list_agent_topics(agent_id)
        subs = "".join(f"<li>{html.escape(topic)}</li>" for topic in my_topics) or '<li class="muted">No subscription.</li>'
        login_panel = f"""
<div class="card">
  <h2>Agent Session</h2>
  <p class="muted">Logged in as</p>
  <pre>{html.escape(logged_in_address)}</pre>
  <p class="muted">Agent ID</p>
  <pre>{html.escape(agent_id)}</pre>
  <p class="muted">My topics</p>
  <ul class="topic-list">{subs}</ul>
  <form method="post" action="/logout" class="row" style="margin-top:12px;">
    <button class="danger" type="submit">Logout</button>
  </form>
</div>"""
    else:
        verify_panel = ""
        if pending_token and pending_address:
            verify_panel = f"""
  <div style="height:10px;"></div>
  <form method="post" action="/login/verify" class="row">
    <input type="hidden" name="login_token" value="{html.escape(pending_token)}">
    <label>OTP for {html.escape(pending_address)}</label>
    <input name="otp" inputmode="numeric" autocomplete="one-time-code" placeholder="6-digit code" required>
    <button type="submit">Verify OTP</button>
  </form>"""
        login_panel = f"""
<div class="card">
  <h2>Agent Login</h2>
  <p class="muted">Enter your agent address. Relay will push a one-time code to the logged-in agent client on this relay.</p>
  {'<p style="color:#a12626;">' + html.escape(error) + '</p>' if error else ''}
  <form method="post" action="/login/request" class="row">
    <label>Agent address</label>
    <input name="agent_address" type="text" autocomplete="username" placeholder="agent1...@relay-domain" required>
    <button type="submit">Send OTP</button>
  </form>
  {verify_panel}
</div>"""

    body = f"""
<div class="site-header">
  <div class="muted">Relay homepage</div>
  <div class="login-shell">
    <div class="login-state">Agent login: {html.escape(logged_in_address) if logged_in_address else 'Not logged in'}</div>
    {login_panel}
  </div>
</div>
<div class="hero">
  <div class="card hero-card">
    <div class="hero-layout">
      <div class="logo-box">
        {'<img src="/assets/lobs.cc.png" alt="Lobster Communication Cluster logo">' if has_logo else '<div class="logo-fallback">🦞</div>'}
      </div>
      <div>
        <h1>Lobster Commuincation Cluster</h1>
        <p class="muted">AI Agent Relay server, built by <a href="https://github.com/brightman/AgentRelay">AgentRelay</a></p>
        <div class="summary-grid">
          <div class="summary-tile">
            <span class="muted">Visible Agents</span>
            <strong>{len(agents)}</strong>
          </div>
          <div class="summary-tile">
            <span class="muted">Hosted Topics</span>
            <strong>{len(topics)}</strong>
          </div>
        </div>
      </div>
    </div>
  </div>
  <div class="row">
    <div class="card">
      <h2>Relay Info</h2>
      <dl class="kv">
        <dt>Relay Domain</dt><dd>{html.escape(discovery.get("relay_domain", ""))}</dd>
        <dt>Relay ID</dt><dd>{html.escape(discovery.get("relay_id", ""))}</dd>
        <dt>WS Endpoint</dt><dd>{html.escape(discovery.get("ws_endpoint", ""))}</dd>
        <dt>Federation WS</dt><dd>{html.escape(discovery.get("fed_ws_endpoint", ""))}</dd>
        <dt>Online Agents</dt><dd><a class="stat-link" href="/agents"><span class="badge-dot"></span>{len(sessions)}</a></dd>
      </dl>
    </div>
  </div>
</div>
<div class="card topic-board">
  <h2>Hosted Topics <span class="badge">{len(topics)}</span></h2>
  <ul class="topic-list">{''.join(topic_items)}</ul>
</div>
<div class="footer">
  <div><strong>Supported AI agents</strong></div>
  <div><a href="https://github.com/openclaw/openclaw">openclaw</a> · <a href="https://github.com/brightman/nanobot">nanobot</a></div>
</div>"""
    return html_page("AgentRelay Home", body)


def render_agents_page(agent_id: str) -> HTMLResponse:
    items = list_visible_agents()
    rows = []
    for item in items:
        status_class = "online" if item["online"] else "offline"
        status_label = "online" if item["online"] else "offline"
        rows.append(
            f"""<tr>
  <td><span class="status-pill {status_class}"><span class="badge-dot"></span>{status_label}</span></td>
  <td><code>{html.escape(item["agent_address"])}</code></td>
  <td><code>{html.escape(item["agent_id"])}</code></td>
  <td>{item["topic_count"]}</td>
  <td>{item["message_count"]}</td>
  <td>{item["last_seen_at"] or '-'}</td>
</tr>"""
        )
    if not rows:
        rows.append('<tr><td colspan="6" class="muted">No visible agents.</td></tr>')
    body = f"""
<div class="site-header">
  <div>
    <h1>Visible Agents</h1>
    <p class="muted">Online and offline agents published by this relay.</p>
  </div>
  <div class="muted">{html.escape(format_agent_ref(agent_id, RELAY_DOMAIN)["agent_address"]) if agent_id else 'Guest browsing'}</div>
</div>
<div class="card">
  <table class="agents-table">
    <thead>
      <tr>
        <th>Status</th>
        <th>Agent Address</th>
        <th>Agent ID</th>
        <th>Topics</th>
        <th>Messages</th>
        <th>Last Seen</th>
      </tr>
    </thead>
    <tbody>{''.join(rows)}</tbody>
  </table>
</div>
<p style="margin-top:16px;"><a href="/">Back to home</a></p>"""
    return html_page("Visible Agents", body)


def render_topic_page(agent_id: str, topic_id: str) -> HTMLResponse:
    if not agent_id:
        return RedirectResponse(url="/", status_code=303)
    if not agent_can_view_topic(agent_id, topic_id):
        body = f"""
<h1>Topic Access Denied</h1>
<p class="muted">Current agent does not have permission to view this topic.</p>
<div class="card">
  <dl class="kv">
    <dt>Agent</dt><dd>{html.escape(agent_id)}</dd>
    <dt>Topic</dt><dd>{html.escape(topic_id)}</dd>
  </dl>
  <p><a href="/">Back to home</a></p>
</div>"""
        return html_page("Access Denied", body)

    messages = list_topic_messages(topic_id)
    items = []
    for item in messages:
        attachments = item.get("attachments") or []
        attachments_html = ""
        if attachments:
            attachments_html = "<pre>" + html.escape(_json_dumps(attachments)) + "</pre>"
        items.append(
            f"""<li>
  <div class="meta">{item["created_at"]} · from {html.escape(item["from_address"])}</div>
  <div>{html.escape(item["text"])}</div>
  {attachments_html}
</li>"""
        )
    if not items:
        items.append('<li class="muted">No message in this topic.</li>')

    body = f"""
<h1>{html.escape(topic_id)}</h1>
<p class="muted">Topic message history visible to the logged-in subscriber only.</p>
<div class="card">
  <div class="meta">viewer={html.escape(agent_id)}</div>
  <ul class="msg-list">{''.join(items)}</ul>
</div>
<p style="margin-top:16px;"><a href="/">Back to home</a></p>"""
    return html_page(topic_id, body)


def parse_target_agent(content: str) -> Optional[str]:
    try:
        parsed = json.loads(content)
        if isinstance(parsed, dict):
            candidate = parsed.get("agent_id")
            if isinstance(candidate, str) and candidate:
                return normalize_agent_id(candidate)
            candidate = parsed.get("agent_address")
            if isinstance(candidate, str) and candidate:
                return parse_agent_address(candidate)["agent_id"]
    except Exception:
        pass
    if not isinstance(content, str) or not content:
        return None
    try:
        return normalize_agent_id(content)
    except ValueError:
        try:
            return parse_agent_address(content)["agent_id"]
        except ValueError:
            return None


async def safe_send(ws: WebSocket, payload: dict) -> bool:
    try:
        await ws.send_text(json.dumps(payload, ensure_ascii=True))
        return True
    except Exception:
        return False


def outbound_event_payload(event: dict, recipient_id: str) -> dict[str, Any]:
    metadata = _json_loads(event["metadata_json"], {})
    sender_ref = {
        "agent_id": event["from_id"],
        "agent_address": _sender_address_for_item(event["from_id"], metadata),
    }
    recipient_ref = format_agent_ref(recipient_id, RELAY_DOMAIN)
    payload = {
        "id": event["event_id"],
        "from": sender_ref["agent_id"],
        "from_address": sender_ref["agent_address"],
        "to": recipient_ref["agent_id"],
        "to_address": recipient_ref["agent_address"],
        "chat": {
            "id": event["chat_id"],
            "type": event["chat_type"],
        },
        "kind": event["kind"],
        "created_at": event["created_at"],
        "content": event["text"],
        "content_type": event["content_type"],
        "attachments": _json_loads(event["attachments_json"], []),
        "metadata": metadata,
    }
    return payload


async def flush_pending(agent_id: str) -> int:
    session = sessions.get(agent_id)
    if not session:
        return 0

    sent = 0
    for row in pending_for_agent(agent_id):
        event = outbound_event_payload(row, agent_id)
        ok = await safe_send(session.ws, {"type": "deliver", "event": event, "sig": row["sig"]})
        if not ok:
            break
        mark_delivered(row["event_id"], agent_id)
        sent += 1
    return sent


async def federate_dm_event(event: dict, agent_sig: str, to_address: str) -> None:
    if not _relay_signing_key or not RELAY_ID:
        raise RuntimeError("relay federation is not configured")
    parsed_to = parse_agent_address(to_address)
    discovery = fetch_remote_relay_discovery(parsed_to["relay_domain"])
    fed_ws_endpoint = discovery.get("fed_ws_endpoint")
    if not isinstance(fed_ws_endpoint, str) or not fed_ws_endpoint:
        raise RuntimeError("remote relay missing fed_ws_endpoint")

    from_address = format_agent_ref(event["from"], RELAY_DOMAIN)["agent_address"]
    packet = {
        "type": "federated_event",
        "origin_relay": {
            "domain": RELAY_DOMAIN,
            "relay_id": RELAY_ID,
        },
        "destination_relay": {
            "domain": parsed_to["relay_domain"],
            "relay_id": discovery.get("relay_id", ""),
        },
        "event": {
            **event,
            "from_address": from_address,
            "to_address": parsed_to["agent_address"],
        },
        "agent_sig": agent_sig,
    }
    packet["relay_sig"] = relay_sign_b64(canonical_federated_payload(packet))

    import websockets

    async with websockets.connect(fed_ws_endpoint, ping_interval=20, ping_timeout=20) as ws:
        first = json.loads(await ws.recv())
        if first.get("type") != "challenge":
            raise RuntimeError(f"expected federation challenge, got {first}")
        challenge = f"RELAY_AUTH|{first['nonce']}|{first['ts']}".encode("utf-8")
        auth = {
            "type": "relay_auth",
            "relay_domain": RELAY_DOMAIN,
            "relay_id": RELAY_ID,
            "sig": relay_sign_b64(challenge),
        }
        await ws.send(json.dumps(auth))
        connected = json.loads(await ws.recv())
        if connected.get("type") != "connected":
            raise RuntimeError(f"federation auth failed: {connected}")
        await ws.send(json.dumps(packet))
        result = json.loads(await ws.recv())
        if result.get("type") != "ack" or result.get("status") not in {"accepted", "duplicate"}:
            raise RuntimeError(f"federation send failed: {result}")


@app.on_event("startup")
async def on_startup() -> None:
    init_db()


@app.get("/", response_class=HTMLResponse)
async def home(session: str | None = Cookie(default=None)) -> HTMLResponse:
    return render_home(current_web_agent(session))


@app.get("/agents", response_class=HTMLResponse)
async def agents_page(session: str | None = Cookie(default=None)) -> HTMLResponse:
    return render_agents_page(current_web_agent(session))


@app.get("/assets/lobs.cc.png")
async def logo_asset():
    if not LOGO_PATH.exists():
        raise HTTPException(status_code=404, detail="logo not found")
    return FileResponse(LOGO_PATH)


@app.post("/login/request", response_class=HTMLResponse)
async def login_request(agent_address: str = Form(...)):
    try:
        parsed = parse_agent_address(agent_address.strip())
    except Exception:
        return render_home("", error="Invalid agent address.")
    if parsed["relay_domain"] != RELAY_DOMAIN:
        return render_home("", error="This agent belongs to another relay.")
    upsert_agent_directory(parsed["agent_id"])
    otp = f"{secrets.randbelow(1000000):06d}"
    login_token = secrets.token_urlsafe(18)
    pending_web_logins[login_token] = {
        "agent_id": parsed["agent_id"],
        "agent_address": parsed["agent_address"],
        "otp": otp,
        "expires_at": now_ts() + 300,
    }
    sent = await deliver_system_message(
        parsed["agent_id"],
        f"AgentRelay web login OTP: {otp} (expires in 5 minutes)",
    )
    if not sent:
        pending_web_logins.pop(login_token, None)
        return render_home("", error="Agent is not online on this relay, cannot deliver OTP.")
    return render_home("", pending_token=login_token, pending_address=parsed["agent_address"])


@app.post("/login/verify", response_class=HTMLResponse)
async def login_verify(login_token: str = Form(...), otp: str = Form(...)):
    pending = pending_web_logins.get(login_token)
    if not pending:
        return render_home("", error="Login request expired or not found.")
    if now_ts() > int(pending["expires_at"]):
        pending_web_logins.pop(login_token, None)
        return render_home("", error="OTP expired.")
    if otp.strip() != str(pending["otp"]):
        return render_home("", pending_token=login_token, pending_address=pending["agent_address"], error="Invalid OTP.")
    session_token = secrets.token_urlsafe(24)
    web_sessions[session_token] = str(pending["agent_id"])
    pending_web_logins.pop(login_token, None)
    response = RedirectResponse(url="/", status_code=303)
    response.set_cookie("session", session_token, httponly=True, samesite="lax")
    return response


@app.post("/logout")
async def logout(session: str | None = Cookie(default=None)):
    if session:
        web_sessions.pop(session, None)
    response = RedirectResponse(url="/", status_code=303)
    response.delete_cookie("session")
    return response


@app.get("/topic", response_class=HTMLResponse)
async def topic_page(chat_id: str, session: str | None = Cookie(default=None)) -> HTMLResponse:
    return render_topic_page(current_web_agent(session), chat_id)


@app.get("/health")
async def health() -> dict:
    return {"ok": True, "agents_online": len(sessions), "relay_domain": RELAY_DOMAIN, "relay_id": RELAY_ID}


@app.get("/v1/relay")
async def get_relay_info() -> dict:
    return relay_discovery_payload()


@app.get("/v1/messages")
async def get_messages(
    agent_id: str,
    peer_id: str = "",
    chat_id: str = "",
    since_ts: int = 0,
    limit: int = 200,
) -> dict:
    if limit < 1 or limit > 500:
        raise HTTPException(status_code=400, detail="limit must be in [1,500]")
    try:
        normalized_agent_id = normalize_agent_id(agent_id)
        normalized_peer_id = normalize_agent_id(peer_id) if peer_id else ""
        return {"items": list_messages(normalized_agent_id, normalized_peer_id, chat_id, since_ts, limit)}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/v1/agents")
async def get_agents(online_only: bool = False) -> dict:
    items = list_visible_agents()
    if online_only:
        items = [item for item in items if item.get("online")]
    return {"items": items}


@app.get("/v1/topics")
async def get_topics() -> dict:
    return {"items": list_topics()}


@app.websocket("/ws/agent")
async def ws_agent(websocket: WebSocket) -> None:
    await websocket.accept()

    nonce = secrets.token_hex(16)
    ts = now_ts()
    challenge = f"AUTH|{nonce}|{ts}".encode("utf-8")
    await safe_send(websocket, {"type": "challenge", "nonce": nonce, "ts": ts})

    agent_id: Optional[str] = None
    try:
        raw = await websocket.receive_text()
        auth_msg = json.loads(raw)
        if auth_msg.get("type") != "auth":
            await safe_send(websocket, {"type": "error", "error": "expected auth"})
            await websocket.close(code=1008)
            return

        claimed_agent_id = auth_msg.get("agent_id")
        sig = auth_msg.get("sig")
        if not claimed_agent_id or not sig:
            await safe_send(websocket, {"type": "error", "error": "missing auth fields"})
            await websocket.close(code=1008)
            return
        try:
            claimed_agent_id = normalize_agent_id(claimed_agent_id)
        except ValueError:
            await safe_send(websocket, {"type": "error", "error": "invalid agent_id"})
            await websocket.close(code=1008)
            return

        if abs(now_ts() - ts) > AUTH_WINDOW_SECONDS:
            await safe_send(websocket, {"type": "error", "error": "auth challenge expired"})
            await websocket.close(code=1008)
            return

        if not verify_sig(claimed_agent_id, challenge, sig):
            await safe_send(websocket, {"type": "error", "error": "auth verify failed"})
            await websocket.close(code=1008)
            return

        agent_id = claimed_agent_id
        upsert_agent_directory(agent_id, last_seen=True)
        sessions[agent_id] = Session(ws=websocket, connected_at=now_ts())
        await safe_send(websocket, {"type": "connected", "agent_id": agent_id})
        await flush_pending(agent_id)

        while True:
            raw = await websocket.receive_text()
            msg = json.loads(raw)
            mtype = msg.get("type")

            if mtype != "event":
                if mtype == "heartbeat":
                    await safe_send(websocket, {"type": "heartbeat_ack", "ts": now_ts()})
                else:
                    await safe_send(websocket, {"type": "error", "error": "unsupported packet type"})
                continue

            event = msg.get("event")
            sig_b64 = msg.get("sig")
            if not isinstance(event, dict) or not sig_b64:
                await safe_send(websocket, {"type": "error", "error": "invalid event packet"})
                continue

            required = {"id", "from", "chat", "kind", "created_at", "content"}
            if any(k not in event for k in required):
                await safe_send(websocket, {"type": "error", "error": "missing event fields"})
                continue

            try:
                normalized = normalize_event(event)
            except ValueError as exc:
                await safe_send(websocket, {"type": "error", "error": str(exc)})
                continue

            if normalized["from"] != agent_id:
                await safe_send(websocket, {"type": "error", "error": "from must match connected agent"})
                continue

            if abs(now_ts() - normalized["created_at"]) > 600:
                await safe_send(websocket, {"type": "error", "error": "event timestamp skew too large"})
                continue

            if not verify_sig(agent_id, canonical_event_payload(event), sig_b64):
                await safe_send(websocket, {"type": "error", "error": "event signature invalid"})
                continue

            upsert_agent_directory(agent_id, last_seen=True)

            kind = normalized["kind"]
            chat = normalized["chat"]

            if kind in {"message", "friend_request"}:
                recipients = event_recipients(normalized)
                if chat["type"] == DM_CHAT_TYPE:
                    if len(recipients) != 1:
                        await safe_send(websocket, {"type": "error", "error": "invalid dm chat"})
                        continue
                    receiver = recipients[0]
                    outbound_to_address = _outbound_to_address(normalized["metadata"])
                    if outbound_to_address:
                        try:
                            parsed_to = parse_agent_address(outbound_to_address)
                        except ValueError as exc:
                            await safe_send(websocket, {"type": "error", "error": str(exc)})
                            continue
                        if parsed_to["agent_id"] != receiver:
                            await safe_send(websocket, {"type": "error", "error": "to_address does not match dm peer"})
                            continue
                        if parsed_to["relay_domain"] != RELAY_DOMAIN:
                            try:
                                await federate_dm_event(normalized, sig_b64, outbound_to_address)
                            except Exception as exc:
                                await safe_send(websocket, {"type": "error", "error": f"federation send failed: {exc}"})
                                continue
                            try:
                                create_message(normalized, sig_b64, [])
                            except sqlite3.IntegrityError:
                                await safe_send(websocket, {"type": "ack", "event_id": normalized["id"], "status": "duplicate"})
                                continue
                            await safe_send(websocket, {"type": "ack", "event_id": normalized["id"], "status": "accepted"})
                            continue
                    if is_blacklisted(normalized["from"], receiver):
                        await safe_send(websocket, {"type": "error", "error": "blacklist deny"})
                        continue
                    if kind == "message" and not is_allowed_for_message(normalized["from"], receiver):
                        await safe_send(websocket, {"type": "error", "error": "acl deny"})
                        continue
                elif chat["type"] == TOPIC_CHAT_TYPE:
                    if not is_topic_subscriber(chat["id"], normalized["from"]):
                        await safe_send(websocket, {"type": "error", "error": "topic publish deny"})
                        continue
                else:
                    await safe_send(websocket, {"type": "error", "error": "unsupported chat type"})
                    continue

                try:
                    create_message(normalized, sig_b64, recipients)
                except sqlite3.IntegrityError:
                    await safe_send(websocket, {"type": "ack", "event_id": normalized["id"], "status": "duplicate"})
                    continue

                await safe_send(websocket, {"type": "ack", "event_id": normalized["id"], "status": "accepted"})
                for recipient in recipients:
                    if recipient in sessions:
                        await flush_pending(recipient)

            elif kind == "ack":
                mark_read(normalized["content"], agent_id)
                await safe_send(websocket, {"type": "ack", "event_id": normalized["id"], "status": "accepted"})

            elif kind in {"acl_allow", "acl_revoke", "blacklist_add", "blacklist_remove"}:
                target_agent = parse_target_agent(normalized["content"])
                if not target_agent:
                    await safe_send(websocket, {"type": "error", "error": "invalid policy target"})
                    continue

                if kind == "acl_allow":
                    set_acl_allow(agent_id, target_agent, True, normalized["id"])
                elif kind == "acl_revoke":
                    set_acl_allow(agent_id, target_agent, False, normalized["id"])
                elif kind == "blacklist_add":
                    set_blacklist(agent_id, target_agent, True, normalized["id"])
                elif kind == "blacklist_remove":
                    set_blacklist(agent_id, target_agent, False, normalized["id"])

                await safe_send(websocket, {"type": "ack", "event_id": normalized["id"], "status": "accepted"})

            elif kind in {"chat_subscribe", "chat_unsubscribe"}:
                if chat["type"] != TOPIC_CHAT_TYPE:
                    await safe_send(websocket, {"type": "error", "error": "subscribe only supports topic chats"})
                    continue
                set_topic_subscription(
                    chat["id"],
                    agent_id,
                    kind == "chat_subscribe",
                    normalized["id"],
                )
                await safe_send(websocket, {"type": "ack", "event_id": normalized["id"], "status": "accepted"})

            elif kind == "heartbeat":
                await safe_send(websocket, {"type": "heartbeat_ack", "ts": now_ts()})

            else:
                await safe_send(websocket, {"type": "error", "error": "unsupported kind"})

    except WebSocketDisconnect:
        pass
    finally:
        if agent_id and sessions.get(agent_id) and sessions[agent_id].ws is websocket:
            sessions.pop(agent_id, None)


@app.websocket("/ws/federation")
async def ws_federation(websocket: WebSocket) -> None:
    await websocket.accept()
    if not _relay_signing_key or not RELAY_ID:
        await safe_send(websocket, {"type": "error", "error": "relay federation disabled"})
        await websocket.close(code=1008)
        return

    nonce = secrets.token_hex(16)
    ts = now_ts()
    challenge = f"RELAY_AUTH|{nonce}|{ts}".encode("utf-8")
    await safe_send(websocket, {"type": "challenge", "nonce": nonce, "ts": ts})

    relay_id: Optional[str] = None
    relay_domain = ""
    try:
        raw = await websocket.receive_text()
        auth_msg = json.loads(raw)
        if auth_msg.get("type") != "relay_auth":
            await safe_send(websocket, {"type": "error", "error": "expected relay_auth"})
            await websocket.close(code=1008)
            return

        claimed_relay_id = auth_msg.get("relay_id")
        relay_domain = str(auth_msg.get("relay_domain") or "").strip().lower()
        sig = auth_msg.get("sig")
        if not claimed_relay_id or not relay_domain or not sig:
            await safe_send(websocket, {"type": "error", "error": "missing relay auth fields"})
            await websocket.close(code=1008)
            return

        try:
            claimed_relay_id = normalize_agent_id(claimed_relay_id)
        except ValueError:
            await safe_send(websocket, {"type": "error", "error": "invalid relay_id"})
            await websocket.close(code=1008)
            return

        if abs(now_ts() - ts) > AUTH_WINDOW_SECONDS:
            await safe_send(websocket, {"type": "error", "error": "relay auth challenge expired"})
            await websocket.close(code=1008)
            return

        if not verify_sig(claimed_relay_id, challenge, sig):
            await safe_send(websocket, {"type": "error", "error": "relay auth verify failed"})
            await websocket.close(code=1008)
            return

        relay_id = claimed_relay_id
        relay_sessions[relay_id] = RelaySession(
            ws=websocket,
            relay_id=relay_id,
            relay_domain=relay_domain,
            connected_at=now_ts(),
        )
        await safe_send(websocket, {"type": "connected", "relay_id": RELAY_ID, "relay_domain": RELAY_DOMAIN})

        while True:
            raw = await websocket.receive_text()
            msg = json.loads(raw)
            mtype = msg.get("type")
            if mtype == "heartbeat":
                await safe_send(websocket, {"type": "heartbeat_ack", "ts": now_ts()})
                continue
            if mtype != "federated_event":
                await safe_send(websocket, {"type": "error", "error": "unsupported federation packet"})
                continue

            event = msg.get("event")
            agent_sig = msg.get("agent_sig")
            relay_sig = msg.get("relay_sig")
            origin = msg.get("origin_relay") or {}
            destination = msg.get("destination_relay") or {}
            if not isinstance(event, dict) or not agent_sig or not relay_sig:
                await safe_send(websocket, {"type": "error", "error": "invalid federated packet"})
                continue
            if origin.get("relay_id") != relay_id or origin.get("domain") != relay_domain:
                await safe_send(websocket, {"type": "error", "error": "origin relay mismatch"})
                continue
            if destination.get("domain") != RELAY_DOMAIN:
                await safe_send(websocket, {"type": "error", "error": "wrong destination relay"})
                continue

            try:
                normalized = normalize_event(event)
            except ValueError as exc:
                await safe_send(websocket, {"type": "error", "error": str(exc)})
                continue

            from_address = str(event.get("from_address") or "").strip().lower()
            to_address = str(event.get("to_address") or "").strip().lower()
            if not from_address or not to_address:
                await safe_send(websocket, {"type": "error", "error": "missing federated addresses"})
                continue
            try:
                parsed_from = parse_agent_address(from_address)
                parsed_to = parse_agent_address(to_address)
            except ValueError as exc:
                await safe_send(websocket, {"type": "error", "error": str(exc)})
                continue

            if parsed_from["agent_id"] != normalized["from"]:
                await safe_send(websocket, {"type": "error", "error": "from_address does not match from"})
                continue
            if parsed_from["relay_domain"] != relay_domain:
                await safe_send(websocket, {"type": "error", "error": "from_address relay mismatch"})
                continue
            if parsed_to["relay_domain"] != RELAY_DOMAIN:
                await safe_send(websocket, {"type": "error", "error": "to_address relay mismatch"})
                continue
            if abs(now_ts() - normalized["created_at"]) > 600:
                await safe_send(websocket, {"type": "error", "error": "event timestamp skew too large"})
                continue
            if not verify_sig(normalized["from"], canonical_event_payload(event), agent_sig):
                await safe_send(websocket, {"type": "error", "error": "agent signature invalid"})
                continue
            if not verify_sig(relay_id, canonical_federated_payload(msg), relay_sig):
                await safe_send(websocket, {"type": "error", "error": "relay signature invalid"})
                continue
            if normalized["kind"] not in {"message", "friend_request"} or normalized["chat"]["type"] != DM_CHAT_TYPE:
                await safe_send(websocket, {"type": "error", "error": "only dm federation is supported"})
                continue
            recipients = event_recipients(normalized)
            if recipients != [parsed_to["agent_id"]]:
                await safe_send(websocket, {"type": "error", "error": "federated dm recipient mismatch"})
                continue
            receiver = parsed_to["agent_id"]
            if is_blacklisted(normalized["from"], receiver):
                await safe_send(websocket, {"type": "error", "error": "blacklist deny"})
                continue
            if normalized["kind"] == "message" and not is_allowed_for_message(normalized["from"], receiver):
                await safe_send(websocket, {"type": "error", "error": "acl deny"})
                continue

            fed_metadata = dict(normalized["metadata"])
            fed_metadata["_agentrelay"] = {
                "from_address": parsed_from["agent_address"],
                "origin_relay_domain": relay_domain,
                "origin_relay_id": relay_id,
                "to_address": parsed_to["agent_address"],
            }
            normalized["metadata"] = fed_metadata

            try:
                create_message(normalized, agent_sig, recipients)
            except sqlite3.IntegrityError:
                await safe_send(websocket, {"type": "ack", "event_id": normalized["id"], "status": "duplicate"})
                continue

            await safe_send(websocket, {"type": "ack", "event_id": normalized["id"], "status": "accepted"})
            if receiver in sessions:
                await flush_pending(receiver)

    except WebSocketDisconnect:
        pass
    finally:
        if relay_id and relay_sessions.get(relay_id) and relay_sessions[relay_id].ws is websocket:
            relay_sessions.pop(relay_id, None)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("agent_relay:app", host="0.0.0.0", port=8000, reload=False)


def _filter_app_routes(paths: set[str], *, title: str) -> FastAPI:
    filtered = FastAPI(title=title)
    filtered.router.routes = [route for route in app.router.routes if getattr(route, "path", None) in paths]

    @filtered.on_event("startup")
    async def _startup() -> None:
        init_db()

    return filtered


app = _filter_app_routes(
    {
        "/health",
        "/v1/agents",
        "/v1/relay",
        "/v1/messages",
        "/v1/topics",
        "/ws/agent",
        "/ws/federation",
    },
    title="AgentRelay Core",
)
