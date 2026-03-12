#!/usr/bin/env python3
import argparse
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

import asyncio
import websockets
from nacl.encoding import HexEncoder
from nacl.signing import SigningKey

from agent_client import (
    DM_CHAT_TYPE,
    SYSTEM_CHAT_TYPE,
    TOPIC_CHAT_TYPE,
    build_chat,
    build_event,
    canonical_event_payload,
    send_event,
    sign_b64,
)
from identity import encode_public_key_bech32, normalize_agent_id, parse_agent_address

BASE_DIR = Path.home() / ".agentrelay"
CONFIG_PATH = BASE_DIR / "config.json"
CONTACTS_PATH = BASE_DIR / "contacts.json"
DATA_PATH = BASE_DIR / "data.json"
INBOX_PATH = BASE_DIR / "inbox.json"
LOGS_DIR = BASE_DIR / "logs"
RUN_DIR = BASE_DIR / "run"


def load_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    return json.loads(path.read_text(encoding="utf-8"))


def save_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def ensure_base_dirs() -> None:
    BASE_DIR.mkdir(parents=True, exist_ok=True)
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    RUN_DIR.mkdir(parents=True, exist_ok=True)


def derive_agent_id(private_key_hex: str) -> str:
    signing_key = SigningKey(private_key_hex, encoder=HexEncoder)
    return signing_key.verify_key.encode(encoder=HexEncoder).decode()


def default_http_base(base_ws: str) -> str:
    if base_ws.startswith("wss://"):
        return "https://" + base_ws[len("wss://") :].rstrip("/")
    if base_ws.startswith("ws://"):
        return "http://" + base_ws[len("ws://") :].rstrip("/")
    return base_ws.rstrip("/")


def discover_relay_domain(base_ws: str) -> str:
    base = default_http_base(base_ws)
    try:
        with urllib.request.urlopen(f"{base}/api/relay", timeout=3) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except Exception:
        return ""
    value = payload.get("relay_domain")
    return value.strip().lower() if isinstance(value, str) else ""


def load_config() -> dict[str, Any]:
    cfg = load_json(CONFIG_PATH, {})
    if not isinstance(cfg, dict):
        raise SystemExit(f"invalid config file: {CONFIG_PATH}")
    return cfg


def require_config() -> dict[str, Any]:
    cfg = load_config()
    missing = [key for key in ("server_url", "private_key") if not str(cfg.get(key) or "").strip()]
    if missing:
        raise SystemExit(
            f"agentrelay config incomplete: missing {', '.join(missing)} in {CONFIG_PATH}"
        )
    cfg["server_url"] = str(cfg["server_url"]).strip()
    cfg["private_key"] = str(cfg["private_key"]).strip()
    cfg["agent_id"] = str(cfg.get("agent_id") or derive_agent_id(cfg["private_key"]))
    return cfg


def load_contacts() -> dict[str, Any]:
    data = load_json(CONTACTS_PATH, {"contacts": {}})
    if not isinstance(data, dict):
        return {"contacts": {}}
    contacts = data.get("contacts")
    if not isinstance(contacts, dict):
        data["contacts"] = {}
    return data


def resolve_target(raw: str) -> str:
    contacts = load_contacts().get("contacts", {})
    if isinstance(contacts, dict):
        matched = contacts.get(raw.lower())
        if isinstance(matched, dict):
            value = matched.get("target")
            if isinstance(value, str) and value.strip():
                return value.strip()
    return raw.strip()


def normalize_target(raw: str) -> str:
    value = raw.strip()
    if not value:
        raise SystemExit("target is required")
    if value.startswith("topic:"):
        return value
    if "@" in value:
        return parse_agent_address(value)["agent_address"]
    try:
        return normalize_agent_id(value)
    except ValueError:
        return value


async def connect_and_auth(base_ws: str, private_key_hex: str):
    signing_key = SigningKey(private_key_hex, encoder=HexEncoder)
    agent_id = signing_key.verify_key.encode(encoder=HexEncoder).decode()
    url = f"{base_ws.rstrip('/')}/ws/agent"
    ws = await websockets.connect(url, ping_interval=20, ping_timeout=20)
    first = json.loads(await ws.recv())
    if first.get("type") != "challenge":
        raise RuntimeError(f"expected challenge, got {first}")
    challenge = f"AUTH|{first['nonce']}|{first['ts']}".encode("utf-8")
    auth = {"type": "auth", "agent_id": agent_id, "sig": sign_b64(signing_key, challenge)}
    await ws.send(json.dumps(auth))
    connected = json.loads(await ws.recv())
    if connected.get("type") != "connected":
        raise RuntimeError(f"auth failed: {connected}")
    return ws, signing_key, agent_id


async def send_control_event(cfg: dict[str, Any], kind: str, content: str, *, topic: str = "") -> None:
    ws, signing_key, agent_id = await connect_and_auth(cfg["server_url"], cfg["private_key"])
    try:
        chat = (
            build_chat(agent_id=agent_id, peer_id="", topic=topic, chat_type=TOPIC_CHAT_TYPE, chat_id="")
            if topic
            else {"id": f"system:{agent_id}", "type": SYSTEM_CHAT_TYPE}
        )
        event = build_event(agent_id, chat, kind, content)
        await send_event(ws, signing_key, event)
        reply = json.loads(await ws.recv())
        if reply.get("type") != "ack":
            raise RuntimeError(reply)
    finally:
        await ws.close()


async def send_message(cfg: dict[str, Any], target: str, message: str, thread_id: str = "") -> None:
    ws, signing_key, agent_id = await connect_and_auth(cfg["server_url"], cfg["private_key"])
    try:
        metadata: dict[str, Any] = {}
        if target.startswith("topic:"):
            chat = build_chat(agent_id=agent_id, peer_id="", topic=target, chat_type=TOPIC_CHAT_TYPE, chat_id="")
        else:
            peer_id = ""
            if "@" in target:
                parsed = parse_agent_address(target)
                peer_id = parsed["agent_id"]
                metadata = {"agentrelay": {"to_address": parsed["agent_address"]}}
            else:
                peer_id = normalize_agent_id(target)
            chat = build_chat(agent_id=agent_id, peer_id=peer_id, topic="", chat_type=DM_CHAT_TYPE, chat_id="")
        if thread_id:
            metadata["threadId"] = thread_id
        event = build_event(agent_id, chat, "message", message, metadata=metadata)
        packet = {"type": "event", "event": event, "sig": sign_b64(signing_key, canonical_event_payload(event))}
        await ws.send(json.dumps(packet))
        reply = json.loads(await ws.recv())
        if reply.get("type") != "ack":
            raise RuntimeError(reply)
        print("accepted")
    finally:
        await ws.close()


def cmd_init(args: argparse.Namespace) -> None:
    ensure_base_dirs()
    agent_id = args.agent_id or derive_agent_id(args.private_key)
    relay_domain = discover_relay_domain(args.server_url)
    agent_address = encode_public_key_bech32(agent_id)
    config = {
        "server_url": args.server_url,
        "private_key": args.private_key,
        "agent_id": agent_id,
        "relay_domain": relay_domain,
        "agent_address": f"{agent_address}@{relay_domain}" if relay_domain else agent_address,
    }
    save_json(CONFIG_PATH, config)
    print(f"wrote {CONFIG_PATH}")
    print(f"agent_id={agent_id}")
    print(f"agent_address={config['agent_address']}")


def cmd_identity(_: argparse.Namespace) -> None:
    cfg = require_config()
    relay_domain = str(cfg.get("relay_domain") or discover_relay_domain(cfg["server_url"]) or "").strip().lower()
    agent_address = encode_public_key_bech32(cfg["agent_id"])
    if relay_domain:
        agent_address = f"{agent_address}@{relay_domain}"
    print(json.dumps({"agent_id": cfg["agent_id"], "agent_address": agent_address}, ensure_ascii=False, indent=2))


def cmd_status(_: argparse.Namespace) -> None:
    cfg = require_config()
    base = default_http_base(cfg["server_url"])
    try:
        with urllib.request.urlopen(f"{base}/health", timeout=3) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except urllib.error.URLError as exc:
        raise SystemExit(f"relay unreachable: {exc}") from exc
    relay_domain = discover_relay_domain(cfg["server_url"]) or str(cfg.get("relay_domain") or "").strip().lower()
    agent_address = encode_public_key_bech32(cfg["agent_id"])
    if relay_domain:
        agent_address = f"{agent_address}@{relay_domain}"
    payload["configured_agent_id"] = cfg["agent_id"]
    payload["configured_agent_address"] = agent_address
    print(json.dumps(payload, ensure_ascii=False, indent=2))


def cmd_contact_add(args: argparse.Namespace) -> None:
    ensure_base_dirs()
    data = load_contacts()
    contacts = data.setdefault("contacts", {})
    target = normalize_target(args.target)
    contacts[args.name.lower()] = {"name": args.name, "target": target}
    save_json(CONTACTS_PATH, data)
    print(f"saved contact {args.name} -> {target}")


def cmd_contact_remove(args: argparse.Namespace) -> None:
    ensure_base_dirs()
    data = load_contacts()
    contacts = data.setdefault("contacts", {})
    removed = contacts.pop(args.name.lower(), None)
    save_json(CONTACTS_PATH, data)
    if removed:
        print(f"removed {args.name}")
    else:
        print(f"contact not found: {args.name}")


def cmd_contact_list(_: argparse.Namespace) -> None:
    contacts = load_contacts().get("contacts", {})
    items = []
    if isinstance(contacts, dict):
        for _, value in sorted(contacts.items()):
            if isinstance(value, dict):
                items.append(value)
    print(json.dumps({"contacts": items}, ensure_ascii=False, indent=2))


def cmd_send(args: argparse.Namespace) -> None:
    cfg = require_config()
    target = normalize_target(resolve_target(args.target))
    asyncio.run(send_message(cfg, target, args.message, thread_id=args.thread_id or ""))


def cmd_allow(args: argparse.Namespace) -> None:
    cfg = require_config()
    target = resolve_target(args.target)
    if "@" in target:
        target = parse_agent_address(target)["agent_id"]
    else:
        target = normalize_agent_id(target)
    asyncio.run(send_control_event(cfg, "acl_allow", target))
    print("accepted")


def cmd_subscribe(args: argparse.Namespace) -> None:
    cfg = require_config()
    topic = args.topic if args.topic.startswith("topic:") else f"topic:{args.topic}"
    asyncio.run(send_control_event(cfg, "chat_subscribe", "", topic=topic))
    print("accepted")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="AgentRelay local CLI")
    sub = parser.add_subparsers(dest="command", required=True)

    p_init = sub.add_parser("init", help="write local AgentRelay config")
    p_init.add_argument("--server-url", required=True, help="relay ws base, e.g. ws://127.0.0.1:8765")
    p_init.add_argument("--private-key", required=True, help="Ed25519 private key hex")
    p_init.add_argument("--agent-id", default="", help="optional public key hex override")
    p_init.set_defaults(func=cmd_init)

    p_identity = sub.add_parser("identity", help="print configured agent identity")
    p_identity.set_defaults(func=cmd_identity)

    p_status = sub.add_parser("status", help="check relay health and local config")
    p_status.set_defaults(func=cmd_status)

    p_contact = sub.add_parser("contact", help="manage contacts")
    p_contact_sub = p_contact.add_subparsers(dest="contact_command", required=True)
    p_contact_add = p_contact_sub.add_parser("add")
    p_contact_add.add_argument("name")
    p_contact_add.add_argument("target")
    p_contact_add.set_defaults(func=cmd_contact_add)
    p_contact_remove = p_contact_sub.add_parser("remove")
    p_contact_remove.add_argument("name")
    p_contact_remove.set_defaults(func=cmd_contact_remove)
    p_contact_list = p_contact_sub.add_parser("list")
    p_contact_list.set_defaults(func=cmd_contact_list)

    p_send = sub.add_parser("send", help="send a DM or topic message")
    p_send.add_argument("target", help="contact name, agent id/address, or topic:name")
    p_send.add_argument("message")
    p_send.add_argument("--thread-id", default="")
    p_send.set_defaults(func=cmd_send)

    p_allow = sub.add_parser("allow", help="allow another agent to DM you")
    p_allow.add_argument("target", help="contact name or agent id/address")
    p_allow.set_defaults(func=cmd_allow)

    p_subscribe = sub.add_parser("subscribe", help="subscribe to a topic")
    p_subscribe.add_argument("topic", help="topic name or topic:name")
    p_subscribe.set_defaults(func=cmd_subscribe)

    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
