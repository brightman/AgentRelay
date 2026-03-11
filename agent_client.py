import argparse
import base64
import hashlib
import json
import time
import uuid
from contextlib import suppress
from pathlib import Path
from typing import Any

import asyncio
import websockets
from nacl.encoding import HexEncoder
from nacl.signing import SigningKey, VerifyKey

from identity import encode_public_key_bech32, normalize_agent_id, parse_agent_address

DM_CHAT_TYPE = "dm"
TOPIC_CHAT_TYPE = "topic"
SYSTEM_CHAT_TYPE = "system"


def _json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"))


def make_dm_chat_id(agent_a: str, agent_b: str) -> str:
    left, right = sorted([normalize_agent_id(agent_a), normalize_agent_id(agent_b)])
    return f"dm:{left}:{right}"


def make_topic_chat_id(topic: str) -> str:
    topic = topic.strip()
    return topic if topic.startswith("topic:") else f"topic:{topic}"


def canonical_event_payload(event: dict) -> bytes:
    content_hash = hashlib.sha256(event["content"].encode("utf-8")).hexdigest()
    chat = event["chat"]
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


def sign_b64(signing_key: SigningKey, payload: bytes) -> str:
    sig = signing_key.sign(payload).signature
    return base64.b64encode(sig).decode("ascii")


def verify_sig(pubkey_hex: str, payload: bytes, sig_b64: str) -> bool:
    try:
        verify_key = VerifyKey(normalize_agent_id(pubkey_hex), encoder=HexEncoder)
        sig = base64.b64decode(sig_b64)
        verify_key.verify(payload, sig)
        return True
    except Exception:
        return False


def infer_attachment_type(value: str) -> str:
    ext = Path(value).suffix.lower()
    if ext in {".jpg", ".jpeg", ".png", ".gif", ".webp"}:
        return "image"
    if ext in {".ogg", ".mp3", ".wav", ".m4a", ".aac"}:
        return "audio"
    if ext in {".mp4", ".mov", ".mkv", ".webm"}:
        return "video"
    return "file"


def build_chat(
    *,
    agent_id: str,
    peer_id: str,
    topic: str,
    chat_type: str,
    chat_id: str,
) -> dict[str, Any]:
    if chat_id:
        return {"id": chat_id, "type": chat_type}
    if chat_type == TOPIC_CHAT_TYPE:
        if not topic:
            raise ValueError("topic chat requires --topic or --chat-id")
        cid = make_topic_chat_id(topic)
        title = topic[6:] if topic.startswith("topic:") else topic
        return {"id": cid, "type": TOPIC_CHAT_TYPE, "title": title}
    if chat_type == DM_CHAT_TYPE:
        if not peer_id:
            return {"id": f"system:{agent_id}", "type": SYSTEM_CHAT_TYPE}
        return {"id": make_dm_chat_id(agent_id, peer_id), "type": DM_CHAT_TYPE}
    return {"id": chat_id or f"system:{agent_id}", "type": chat_type}


def build_event(
    agent_id: str,
    chat: dict[str, Any],
    kind: str,
    content: str,
    *,
    content_type: str = "text/plain",
    attachments: list[dict[str, Any]] | None = None,
    metadata: dict[str, Any] | None = None,
) -> dict:
    event = {
        "id": str(uuid.uuid4()),
        "from": agent_id,
        "chat": chat,
        "kind": kind,
        "created_at": int(time.time()),
        "content": content,
    }
    if content_type != "text/plain":
        event["content_type"] = content_type
    if attachments:
        event["attachments"] = attachments
    if metadata:
        event["metadata"] = metadata
    return event


async def send_event(ws, signing_key: SigningKey, event: dict) -> None:
    packet = {
        "type": "event",
        "event": event,
        "sig": sign_b64(signing_key, canonical_event_payload(event)),
    }
    await ws.send(json.dumps(packet))


async def _interactive_sender(
    ws,
    signing_key: SigningKey,
    agent_id: str,
    chat: dict[str, Any],
    peer_id: str,
    peer_address: str,
) -> None:
    print("interactive mode: type message and press Enter, /quit to exit")
    while True:
        text = await asyncio.to_thread(input, "you> ")
        if not text.strip():
            continue
        if text.strip().lower() in {"/quit", "/exit"}:
            raise KeyboardInterrupt
        metadata = {"agentrelay": {"to_address": peer_address}} if peer_address else {}
        event = build_event(agent_id, chat, "message", text, metadata=metadata)
        await send_event(ws, signing_key, event)


async def _receiver_loop(
    ws,
    signing_key: SigningKey,
    agent_id: str,
    peer_id: str,
    auto_reply: bool,
) -> None:
    async for raw in ws:
        msg = json.loads(raw)
        mtype = msg.get("type")

        if mtype == "deliver":
            event = msg["event"]
            sig_b64 = msg.get("sig", "")
            chat = event.get("chat")
            if not isinstance(chat, dict):
                raise RuntimeError(f"deliver event missing chat object: {event}")
            sender = event.get("from")
            if not isinstance(sender, str) or not sender:
                raise RuntimeError(f"deliver event missing sender: {event}")
            if not sig_b64 or not verify_sig(sender, canonical_event_payload(event), sig_b64):
                raise RuntimeError(f"deliver event signature invalid: {event}")
            if chat.get("type") == DM_CHAT_TYPE:
                expected_chat_id = make_dm_chat_id(agent_id, sender)
                if chat.get("id") != expected_chat_id:
                    raise RuntimeError(f"deliver dm chat mismatch: expected {expected_chat_id}, got {chat}")
            summary = event["content"]
            attachments = event.get("attachments") or []
            if attachments:
                summary = f"{summary} [attachments={len(attachments)}]"
            print(f"[{chat['id']}] {sender} ({chat['type']}:{event['kind']}): {summary}")
            if attachments:
                print(json.dumps({"attachments": attachments}, ensure_ascii=False, indent=2))

            ack_event = build_event(agent_id, chat, "ack", event["id"])
            await send_event(ws, signing_key, ack_event)

            if auto_reply and chat.get("type") == DM_CHAT_TYPE and peer_id and sender == peer_id and event["kind"] == "message":
                reply = build_event(
                    agent_id,
                    chat,
                    "message",
                    f"echo from {agent_id[:12]}: {event['content']}",
                )
                await send_event(ws, signing_key, reply)

        elif mtype == "ack":
            print(f"server ack: {msg}")
        elif mtype == "error":
            print(f"server error: {msg}")


async def run(
    base_ws: str,
    private_key_hex: str,
    peer_id: str,
    chat_type: str,
    chat_id: str,
    topic: str,
    auto_reply: bool,
    once_text: str,
    once_kind: str,
    once_content_type: str,
    once_attachments: list[dict[str, Any]],
    once_metadata: dict[str, Any],
    allow_agent: str,
    revoke_agent: str,
    blacklist_agent: str,
    unblacklist_agent: str,
    subscribe_topic: str,
    unsubscribe_topic: str,
    interactive: bool,
) -> None:
    signing_key = SigningKey(private_key_hex, encoder=HexEncoder)
    agent_id = signing_key.verify_key.encode(encoder=HexEncoder).decode()
    agent_address = encode_public_key_bech32(agent_id)
    peer_address = ""
    if peer_id:
        if "@" in peer_id:
            parsed_peer = parse_agent_address(peer_id)
            peer_id = parsed_peer["agent_id"]
            peer_address = parsed_peer["agent_address"]
        else:
            peer_id = normalize_agent_id(peer_id)
    allow_agent = normalize_agent_id(allow_agent) if allow_agent else ""
    revoke_agent = normalize_agent_id(revoke_agent) if revoke_agent else ""
    blacklist_agent = normalize_agent_id(blacklist_agent) if blacklist_agent else ""
    unblacklist_agent = normalize_agent_id(unblacklist_agent) if unblacklist_agent else ""
    url = f"{base_ws.rstrip('/')}/ws/agent"
    print(f"agent_id={agent_id}")
    print(f"agent_address={agent_address}")
    print(f"connecting to {url}")

    chat = build_chat(
        agent_id=agent_id,
        peer_id=peer_id,
        topic=topic,
        chat_type=chat_type,
        chat_id=chat_id,
    )

    while True:
        try:
            async with websockets.connect(url, ping_interval=20, ping_timeout=20) as ws:
                first = json.loads(await ws.recv())
                if first.get("type") != "challenge":
                    raise RuntimeError(f"expected challenge, got {first}")

                challenge = f"AUTH|{first['nonce']}|{first['ts']}".encode("utf-8")
                auth = {
                    "type": "auth",
                    "agent_id": agent_id,
                    "sig": sign_b64(signing_key, challenge),
                }
                await ws.send(json.dumps(auth))

                connected = json.loads(await ws.recv())
                if connected.get("type") != "connected":
                    raise RuntimeError(f"auth failed: {connected}")
                print("connected")

                system_chat = {"id": f"system:{agent_id}", "type": SYSTEM_CHAT_TYPE}
                if allow_agent:
                    await send_event(ws, signing_key, build_event(agent_id, system_chat, "acl_allow", normalize_agent_id(allow_agent)))
                if revoke_agent:
                    await send_event(ws, signing_key, build_event(agent_id, system_chat, "acl_revoke", normalize_agent_id(revoke_agent)))
                if blacklist_agent:
                    await send_event(ws, signing_key, build_event(agent_id, system_chat, "blacklist_add", normalize_agent_id(blacklist_agent)))
                if unblacklist_agent:
                    await send_event(ws, signing_key, build_event(agent_id, system_chat, "blacklist_remove", normalize_agent_id(unblacklist_agent)))
                if subscribe_topic:
                    topic_chat = build_chat(
                        agent_id=agent_id,
                        peer_id="",
                        topic=subscribe_topic,
                        chat_type=TOPIC_CHAT_TYPE,
                        chat_id="",
                    )
                    await send_event(ws, signing_key, build_event(agent_id, topic_chat, "chat_subscribe", ""))
                if unsubscribe_topic:
                    topic_chat = build_chat(
                        agent_id=agent_id,
                        peer_id="",
                        topic=unsubscribe_topic,
                        chat_type=TOPIC_CHAT_TYPE,
                        chat_id="",
                    )
                    await send_event(ws, signing_key, build_event(agent_id, topic_chat, "chat_unsubscribe", ""))

                if once_text or once_attachments:
                    event = build_event(
                        agent_id,
                        chat,
                        once_kind,
                        once_text,
                        content_type=once_content_type,
                        attachments=once_attachments,
                        metadata=(
                            {
                                **once_metadata,
                                "agentrelay": {
                                    **(once_metadata.get("agentrelay", {}) if isinstance(once_metadata.get("agentrelay"), dict) else {}),
                                    **(once_metadata.get("agenthub", {}) if isinstance(once_metadata.get("agenthub"), dict) else {}),
                                    **({"to_address": peer_address} if peer_address else {}),
                                },
                            }
                            if isinstance(once_metadata, dict)
                            else {}
                        ),
                    )
                    await send_event(ws, signing_key, event)

                recv_task = asyncio.create_task(
                    _receiver_loop(
                        ws=ws,
                        signing_key=signing_key,
                        agent_id=agent_id,
                        peer_id=peer_id,
                        auto_reply=auto_reply,
                    )
                )
                send_task = None
                if interactive:
                    send_task = asyncio.create_task(
                        _interactive_sender(
                            ws=ws,
                            signing_key=signing_key,
                            agent_id=agent_id,
                            chat=chat,
                            peer_id=peer_id,
                            peer_address=peer_address,
                        )
                    )

                if send_task:
                    done, pending = await asyncio.wait(
                        {recv_task, send_task},
                        return_when=asyncio.FIRST_COMPLETED,
                    )
                    for task in pending:
                        task.cancel()
                        with suppress(asyncio.CancelledError):
                            await task
                    for task in done:
                        task.result()
                else:
                    await recv_task

        except KeyboardInterrupt:
            print("exit requested")
            return
        except Exception as exc:
            print(f"disconnected, retry in 2s: {exc}")
            await asyncio.sleep(2)


def main() -> None:
    parser = argparse.ArgumentParser(description="Signed agent client")
    parser.add_argument("--base-ws", default="ws://127.0.0.1:8000")
    parser.add_argument("--private-key", required=True, help="Ed25519 private key hex")
    parser.add_argument("--peer-id", default="", help="Peer agent id for DM chat")
    parser.add_argument("--chat-type", default=DM_CHAT_TYPE, choices=[DM_CHAT_TYPE, TOPIC_CHAT_TYPE], help="Chat type")
    parser.add_argument("--chat-id", default="", help="Explicit chat id override")
    parser.add_argument("--topic", default="", help="Topic name or topic:... chat id")
    parser.add_argument("--auto-reply", action="store_true")
    parser.add_argument("--send", default="", help="Send one payload after connect")
    parser.add_argument(
        "--kind",
        default="message",
        choices=["message", "friend_request"],
        help="Kind used with --send",
    )
    parser.add_argument("--content-type", default="text/plain", help="Optional content type for --send")
    parser.add_argument("--attachments-json", default="", help="JSON array of attachment objects for --send")
    parser.add_argument("--metadata-json", default="", help="JSON object metadata for --send")
    parser.add_argument("--attach", action="append", default=[], help="Convenience attachment URI/path; may be repeated")
    parser.add_argument("--allow-agent", default="", help="Self-signed ACL allow target agent")
    parser.add_argument("--revoke-agent", default="", help="Self-signed ACL revoke target agent")
    parser.add_argument("--blacklist-agent", default="", help="Self-signed blacklist add target agent")
    parser.add_argument("--unblacklist-agent", default="", help="Self-signed blacklist remove target agent")
    parser.add_argument("--topic-subscribe", default="", help="Subscribe to a topic chat")
    parser.add_argument("--topic-unsubscribe", default="", help="Unsubscribe from a topic chat")
    parser.add_argument("--interactive", action="store_true", help="Interactive chat mode")
    args = parser.parse_args()

    if args.interactive and args.chat_type == DM_CHAT_TYPE and not (args.peer_id or args.chat_id):
        raise SystemExit("--interactive dm chat requires --peer-id or --chat-id")
    if args.interactive and args.chat_type == TOPIC_CHAT_TYPE and not (args.topic or args.chat_id):
        raise SystemExit("--interactive topic chat requires --topic or --chat-id")

    try:
        attachments = json.loads(args.attachments_json) if args.attachments_json else []
        if not isinstance(attachments, list):
            raise ValueError("attachments-json must decode to a list")
        metadata = json.loads(args.metadata_json) if args.metadata_json else {}
        if not isinstance(metadata, dict):
            raise ValueError("metadata-json must decode to an object")
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc
    except json.JSONDecodeError as exc:
        raise SystemExit(f"invalid JSON argument: {exc}") from exc

    for value in args.attach:
        attachments.append(
            {
                "type": infer_attachment_type(value),
                "uri": value,
                "name": Path(value).name or value,
            }
        )

    asyncio.run(
        run(
            base_ws=args.base_ws,
            private_key_hex=args.private_key,
            peer_id=args.peer_id,
            chat_type=args.chat_type,
            chat_id=args.chat_id,
            topic=args.topic,
            auto_reply=args.auto_reply,
            once_text=args.send,
            once_kind=args.kind,
            once_content_type=args.content_type,
            once_attachments=attachments,
            once_metadata=metadata,
            allow_agent=args.allow_agent,
            revoke_agent=args.revoke_agent,
            blacklist_agent=args.blacklist_agent,
            unblacklist_agent=args.unblacklist_agent,
            subscribe_topic=args.topic_subscribe,
            unsubscribe_topic=args.topic_unsubscribe,
            interactive=args.interactive,
        )
    )


if __name__ == "__main__":
    main()
