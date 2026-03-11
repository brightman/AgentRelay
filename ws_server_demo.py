import argparse
import asyncio
import base64
import hashlib
import json
import secrets
import time
from collections import defaultdict, deque
from dataclasses import dataclass
from typing import Any, Deque, Dict, Set

import websockets
from nacl.encoding import HexEncoder
from nacl.signing import VerifyKey

from identity import normalize_agent_id

DM_CHAT_TYPE = "dm"
TOPIC_CHAT_TYPE = "topic"
SYSTEM_CHAT_TYPE = "system"
AUTH_WINDOW_SECONDS = 60


@dataclass
class Session:
    ws: websockets.WebSocketServerProtocol
    connected_at: int


def now_ts() -> int:
    return int(time.time())


def _json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"))


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


def canonical_event_payload(event: dict) -> bytes:
    content_hash = hashlib.sha256(event["content"].encode("utf-8")).hexdigest()
    chat = normalize_chat(event)
    ext = {
        "content_type": event.get("content_type") or "text/plain",
        "attachments": event.get("attachments") or [],
        "metadata": event.get("metadata") or {},
    }
    payload_parts = [
        event["id"],
        event["from"],
        chat["id"],
        chat["type"],
        event["kind"],
        str(event["created_at"]),
        content_hash,
        hashlib.sha256(_json_dumps(ext).encode("utf-8")).hexdigest(),
    ]
    return "|".join(payload_parts).encode("utf-8")


def verify_sig(pubkey_hex: str, payload: bytes, sig_b64: str) -> bool:
    try:
        verify_key = VerifyKey(normalize_agent_id(pubkey_hex), encoder=HexEncoder)
        sig = base64.b64decode(sig_b64)
        verify_key.verify(payload, sig)
        return True
    except Exception:
        return False


def parse_dm_peer(chat_id: str, sender_agent: str) -> str | None:
    parts = chat_id.split(":")
    if len(parts) != 3 or parts[0] != "dm":
        return None
    _, left, right = parts
    if sender_agent == left:
        return right
    if sender_agent == right:
        return left
    return None


class DemoServer:
    def __init__(self, allow_pairs: Set[tuple[str, str]]):
        self.sessions: Dict[str, Session] = {}
        self.pending: Dict[str, Deque[dict]] = defaultdict(deque)
        self.allow_pairs = allow_pairs
        self.topic_subscriptions: Dict[str, Set[str]] = defaultdict(set)

    async def send_json(self, ws, payload: dict) -> bool:
        try:
            await ws.send(json.dumps(payload, ensure_ascii=True))
            return True
        except Exception:
            return False

    async def flush_pending(self, agent_id: str) -> None:
        sess = self.sessions.get(agent_id)
        if not sess:
            return
        q = self.pending[agent_id]
        while q:
            pkt = q[0]
            ok = await self.send_json(sess.ws, pkt)
            if not ok:
                break
            q.popleft()

    def recipients_for(self, event: dict) -> list[str]:
        chat = event["chat"]
        if chat["type"] == DM_CHAT_TYPE:
            peer = parse_dm_peer(chat["id"], event["from"])
            return [peer] if peer else []
        if chat["type"] == TOPIC_CHAT_TYPE:
            return [agent for agent in self.topic_subscriptions[chat["id"]] if agent != event["from"]]
        return []

    async def handle(self, ws):
        nonce = secrets.token_hex(16)
        ts = now_ts()
        await self.send_json(ws, {"type": "challenge", "nonce": nonce, "ts": ts})

        agent_id = None
        try:
            raw = await ws.recv()
            auth = json.loads(raw)
            if auth.get("type") != "auth":
                await self.send_json(ws, {"type": "error", "error": "expected auth"})
                return
            claimed = auth.get("agent_id", "")
            sig = auth.get("sig", "")
            challenge = f"AUTH|{nonce}|{ts}".encode("utf-8")
            try:
                claimed = normalize_agent_id(claimed)
            except ValueError:
                await self.send_json(ws, {"type": "error", "error": "invalid agent_id"})
                return
            if abs(now_ts() - ts) > AUTH_WINDOW_SECONDS:
                await self.send_json(ws, {"type": "error", "error": "auth challenge expired"})
                return
            if not verify_sig(claimed, challenge, sig):
                await self.send_json(ws, {"type": "error", "error": "auth verify failed"})
                return

            agent_id = claimed
            self.sessions[agent_id] = Session(ws=ws, connected_at=now_ts())
            await self.send_json(ws, {"type": "connected", "agent_id": agent_id})
            await self.flush_pending(agent_id)

            async for raw in ws:
                msg = json.loads(raw)
                if msg.get("type") != "event":
                    continue
                event = msg.get("event", {})
                sig_b64 = msg.get("sig", "")

                required = {"id", "from", "chat", "kind", "created_at", "content"}
                if any(k not in event for k in required):
                    await self.send_json(ws, {"type": "error", "error": "missing event fields"})
                    continue

                if event["from"] != agent_id:
                    await self.send_json(ws, {"type": "error", "error": "from mismatch"})
                    continue

                if abs(now_ts() - int(event["created_at"])) > 600:
                    await self.send_json(ws, {"type": "error", "error": "timestamp skew"})
                    continue

                if not verify_sig(agent_id, canonical_event_payload(event), sig_b64):
                    await self.send_json(ws, {"type": "error", "error": "bad signature"})
                    continue

                chat = normalize_chat(event)
                event["chat"] = chat

                if event["kind"] == "message":
                    recipients = self.recipients_for(event)
                    if chat["type"] == DM_CHAT_TYPE:
                        if len(recipients) != 1:
                            await self.send_json(ws, {"type": "error", "error": "invalid dm chat"})
                            continue
                        if (event["from"], recipients[0]) not in self.allow_pairs:
                            await self.send_json(ws, {"type": "error", "error": "acl deny"})
                            continue
                    elif chat["type"] == TOPIC_CHAT_TYPE:
                        if event["from"] not in self.topic_subscriptions[chat["id"]]:
                            await self.send_json(ws, {"type": "error", "error": "topic publish deny"})
                            continue
                    else:
                        await self.send_json(ws, {"type": "error", "error": "unsupported chat type"})
                        continue

                    for recipient in recipients:
                        deliver_event = dict(event)
                        deliver = {"type": "deliver", "event": deliver_event, "sig": sig_b64}
                        if recipient in self.sessions:
                            await self.send_json(self.sessions[recipient].ws, deliver)
                        else:
                            self.pending[recipient].append(deliver)
                    await self.send_json(ws, {"type": "ack", "event_id": event["id"], "status": "accepted"})

                elif event["kind"] == "ack":
                    await self.send_json(ws, {"type": "ack", "event_id": event["id"], "status": "accepted"})

                elif event["kind"] == "chat_subscribe":
                    if chat["type"] != TOPIC_CHAT_TYPE:
                        await self.send_json(ws, {"type": "error", "error": "subscribe only supports topic chats"})
                        continue
                    self.topic_subscriptions[chat["id"]].add(agent_id)
                    await self.send_json(ws, {"type": "ack", "event_id": event["id"], "status": "accepted"})

                elif event["kind"] == "chat_unsubscribe":
                    if chat["type"] != TOPIC_CHAT_TYPE:
                        await self.send_json(ws, {"type": "error", "error": "unsubscribe only supports topic chats"})
                        continue
                    self.topic_subscriptions[chat["id"]].discard(agent_id)
                    await self.send_json(ws, {"type": "ack", "event_id": event["id"], "status": "accepted"})

                elif event["kind"] in {"acl_allow", "acl_revoke", "blacklist_add", "blacklist_remove"}:
                    await self.send_json(ws, {"type": "ack", "event_id": event["id"], "status": "accepted"})

                elif event["kind"] == "heartbeat":
                    await self.send_json(ws, {"type": "heartbeat_ack", "ts": now_ts()})

        finally:
            if agent_id and agent_id in self.sessions and self.sessions[agent_id].ws is ws:
                self.sessions.pop(agent_id, None)


def parse_allow(allow_str: str) -> Set[tuple[str, str]]:
    pairs = set()
    if not allow_str.strip():
        return pairs
    for item in allow_str.split(','):
        a, b = item.split(':', 1)
        pairs.add((a, b))
    return pairs


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument('--host', default='127.0.0.1')
    parser.add_argument('--port', type=int, default=8000)
    parser.add_argument('--allow', default='')
    args = parser.parse_args()

    server = DemoServer(parse_allow(args.allow))
    async with websockets.serve(server.handle, args.host, args.port):
        print(f'demo server on ws://{args.host}:{args.port}/ws/agent (V2 chat model)')
        await asyncio.Future()


if __name__ == '__main__':
    asyncio.run(main())
