#!/usr/bin/env python3
import argparse
import contextlib
import json
import os
import signal
import subprocess
import sys
import threading
import time
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
    make_dm_chat_id,
    send_event,
    sign_b64,
    verify_sig,
)
from identity import encode_public_key_bech32, normalize_agent_id, parse_agent_address

BASE_DIR = Path.home() / ".agentrelay"
CONFIG_PATH = BASE_DIR / "config.json"
CONTACTS_PATH = BASE_DIR / "contacts.json"
DATA_PATH = BASE_DIR / "data.json"
INBOX_PATH = BASE_DIR / "inbox.json"
LOGS_DIR = BASE_DIR / "logs"
RUN_DIR = BASE_DIR / "run"
DAEMON_PID_PATH = RUN_DIR / "daemon.pid"
DAEMON_LOG_PATH = LOGS_DIR / "daemon.log"


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


def load_inbox() -> dict[str, Any]:
    data = load_json(INBOX_PATH, {"items": []})
    if not isinstance(data, dict):
        return {"items": []}
    items = data.get("items")
    if not isinstance(items, list):
        data["items"] = []
    return data


def save_inbox(data: dict[str, Any]) -> None:
    save_json(INBOX_PATH, data)


def load_runtime_state() -> dict[str, Any]:
    data = load_json(DATA_PATH, {})
    return data if isinstance(data, dict) else {}


def save_runtime_state(data: dict[str, Any]) -> None:
    save_json(DATA_PATH, data)


def process_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def short_text(value: str, limit: int = 120) -> str:
    value = value.strip()
    return value if len(value) <= limit else value[: limit - 3] + "..."


def daemon_command() -> list[str]:
    if getattr(sys, "frozen", False):
        return [sys.executable, "daemon", "run"]
    return [sys.executable, str(Path(__file__).resolve()), "daemon", "run"]


def sender_address_for_event(sender_id: str, event: dict[str, Any], relay_domain: str) -> str:
    metadata = event.get("metadata")
    if isinstance(metadata, dict):
        for key in ("_agentrelay", "_agenthub"):
            value = metadata.get(key)
            if isinstance(value, dict):
                from_address = value.get("from_address")
                if isinstance(from_address, str) and from_address.strip():
                    return from_address.strip().lower()
    base = encode_public_key_bech32(sender_id)
    if relay_domain:
        return f"{base}@{relay_domain}"
    return base


def append_inbox_item(item: dict[str, Any]) -> None:
    ensure_base_dirs()
    inbox = load_inbox()
    items = inbox.setdefault("items", [])
    if not isinstance(items, list):
        items = []
        inbox["items"] = items
    existing = {str(entry.get("id")): entry for entry in items if isinstance(entry, dict)}
    existing[item["id"]] = item
    merged = sorted(existing.values(), key=lambda entry: int(entry.get("received_at") or 0), reverse=True)
    inbox["items"] = merged[:500]
    save_inbox(inbox)


def webhook_posted_ids(state: dict[str, Any]) -> set[str]:
    value = state.get("webhook_posted_ids")
    if not isinstance(value, list):
        return set()
    return {str(item) for item in value if str(item)}


def remember_webhook_posted(state: dict[str, Any], event_id: str) -> None:
    posted = webhook_posted_ids(state)
    posted.add(event_id)
    state["webhook_posted_ids"] = sorted(posted)[-1000:]


def webhook_config(cfg: dict[str, Any]) -> dict[str, Any]:
    value = cfg.get("webhook")
    return value if isinstance(value, dict) else {}


def webhook_enabled(cfg: dict[str, Any]) -> bool:
    hook = webhook_config(cfg)
    return bool(hook.get("enabled")) and bool(str(hook.get("url") or "").strip())


def build_openclaw_hook_payload(cfg: dict[str, Any], item: dict[str, Any]) -> dict[str, Any]:
    hook = webhook_config(cfg)
    sender = str(item.get("from_address") or item.get("from_id") or "unknown").strip()
    body = str(item.get("text") or "").strip()
    chat_type = str(item.get("chat_type") or "")
    chat_id = str(item.get("chat_id") or "")
    prefix = str(hook.get("sessionKeyPrefix") or "agentrelay").strip() or "agentrelay"
    if chat_type == TOPIC_CHAT_TYPE:
        session_key = f"{prefix}:{chat_id}"
        message = f"AgentRelay topic message from {sender} in {chat_id}: {body}"
    else:
        session_key = f"{prefix}:{sender}"
        message = f"AgentRelay message from {sender}: {body}"
    payload: dict[str, Any] = {
        "message": message,
        "name": "AgentRelay",
        "wakeMode": "now",
        "deliver": bool(hook.get("deliver", False)),
        "sessionKey": session_key,
    }
    if str(hook.get("agentId") or "").strip():
        payload["agentId"] = str(hook["agentId"]).strip()
    if str(hook.get("channel") or "").strip():
        payload["channel"] = str(hook["channel"]).strip()
    if str(hook.get("to") or "").strip():
        payload["to"] = str(hook["to"]).strip()
    return payload


def post_openclaw_webhook(cfg: dict[str, Any], item: dict[str, Any]) -> None:
    hook = webhook_config(cfg)
    url = str(hook.get("url") or "").strip()
    if not url:
        return
    payload = build_openclaw_hook_payload(cfg, item)
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    token = str(hook.get("token") or "").strip()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=10) as resp:
        if resp.status not in (200, 202):
            raise RuntimeError(f"openclaw webhook returned {resp.status}")


def load_daemon_pid() -> int:
    if not DAEMON_PID_PATH.exists():
        return 0
    try:
        payload = json.loads(DAEMON_PID_PATH.read_text(encoding="utf-8"))
    except Exception:
        return 0
    return int(payload.get("pid") or 0)


def start_daemon_background() -> dict[str, Any]:
    require_config()
    ensure_base_dirs()
    pid = load_daemon_pid()
    if process_alive(pid):
        return {"running": True, "pid": pid, "log": str(DAEMON_LOG_PATH)}
    with DAEMON_LOG_PATH.open("a", encoding="utf-8") as log_file:
        env = os.environ.copy()
        if getattr(sys, "frozen", False):
            # Force onefile builds to re-extract into their own temp dir.
            env["PYINSTALLER_RESET_ENVIRONMENT"] = "1"
        proc = subprocess.Popen(
            daemon_command(),
            stdout=log_file,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
            cwd=str(Path(__file__).resolve().parent),
            env=env,
        )
    payload = {"running": True, "pid": proc.pid, "started_at": int(time.time()), "log": str(DAEMON_LOG_PATH)}
    save_json(DAEMON_PID_PATH, payload)
    return payload


async def daemon_loop(cfg: dict[str, Any]) -> None:
    ensure_base_dirs()
    stop_requested = False

    def _handle_stop(signum, _frame) -> None:
        nonlocal stop_requested
        stop_requested = True

    previous_int = signal.getsignal(signal.SIGINT)
    previous_term = signal.getsignal(signal.SIGTERM)
    signal.signal(signal.SIGINT, _handle_stop)
    signal.signal(signal.SIGTERM, _handle_stop)

    try:
        while not stop_requested:
            try:
                ws, signing_key, agent_id = await connect_and_auth(cfg["server_url"], cfg["private_key"])
                relay_domain = discover_relay_domain(cfg["server_url"]) or str(cfg.get("relay_domain") or "").strip().lower()
                state = load_runtime_state()
                state["daemon"] = {
                    "connected": True,
                    "pid": os.getpid(),
                    "agent_id": agent_id,
                    "relay_domain": relay_domain,
                    "last_connected_at": int(time.time()),
                    "last_error": "",
                }
                save_runtime_state(state)
                try:
                    async for raw in ws:
                        if stop_requested:
                            break
                        msg = json.loads(raw)
                        mtype = msg.get("type")
                        if mtype == "deliver":
                            event = msg.get("event")
                            sig_b64 = str(msg.get("sig") or "")
                            if not isinstance(event, dict):
                                continue
                            sender = str(event.get("from") or "").strip()
                            chat = event.get("chat")
                            if not sender or not isinstance(chat, dict):
                                continue
                            if not sig_b64 or not verify_sig(sender, canonical_event_payload(event), sig_b64):
                                continue
                            if chat.get("type") == DM_CHAT_TYPE:
                                expected_chat_id = make_dm_chat_id(agent_id, sender)
                                if chat.get("id") != expected_chat_id:
                                    continue
                            item = {
                                "id": event["id"],
                                "from_id": sender,
                                "from_address": sender_address_for_event(sender, event, relay_domain),
                                "chat_id": str(chat.get("id") or ""),
                                "chat_type": str(chat.get("type") or ""),
                                "kind": str(event.get("kind") or ""),
                                "text": str(event.get("content") or ""),
                                "content_type": str(event.get("content_type") or "text/plain"),
                                "attachments": event.get("attachments") or [],
                                "metadata": event.get("metadata") or {},
                                "created_at": int(event.get("created_at") or 0),
                                "received_at": int(time.time()),
                                "sig": sig_b64,
                            }
                            append_inbox_item(item)
                            state = load_runtime_state()
                            if webhook_enabled(cfg) and item["id"] not in webhook_posted_ids(state):
                                try:
                                    await asyncio.to_thread(post_openclaw_webhook, cfg, item)
                                    remember_webhook_posted(state, item["id"])
                                    daemon_state = state.setdefault("daemon", {})
                                    daemon_state["last_webhook_ok_at"] = int(time.time())
                                    daemon_state["last_webhook_error"] = ""
                                    save_runtime_state(state)
                                except Exception as exc:
                                    daemon_state = state.setdefault("daemon", {})
                                    daemon_state["last_webhook_error"] = str(exc)
                                    save_runtime_state(state)
                            ack_event = build_event(agent_id, chat, "ack", event["id"])
                            await send_event(ws, signing_key, ack_event)
                        elif mtype == "error":
                            state = load_runtime_state()
                            daemon_state = state.setdefault("daemon", {})
                            daemon_state["last_error"] = str(msg.get("error") or msg)
                            daemon_state["connected"] = True
                            save_runtime_state(state)
                finally:
                    with contextlib.suppress(Exception):
                        await ws.close()
            except Exception as exc:
                state = load_runtime_state()
                daemon_state = state.setdefault("daemon", {})
                daemon_state["connected"] = False
                daemon_state["last_error"] = str(exc)
                daemon_state["pid"] = os.getpid()
                save_runtime_state(state)
                if stop_requested:
                    break
                await asyncio.sleep(2)
    finally:
        signal.signal(signal.SIGINT, previous_int)
        signal.signal(signal.SIGTERM, previous_term)
        state = load_runtime_state()
        daemon_state = state.setdefault("daemon", {})
        daemon_state["connected"] = False
        daemon_state["stopped_at"] = int(time.time())
        save_runtime_state(state)


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
    webhook: dict[str, Any] = {
        "enabled": bool(args.webhook_url),
        "url": args.webhook_url.strip(),
        "token": args.webhook_token.strip(),
        "agentId": args.webhook_agent_id.strip(),
        "sessionKeyPrefix": args.webhook_session_key_prefix.strip() or "agentrelay",
        "deliver": bool(args.webhook_deliver),
        "channel": args.webhook_channel.strip(),
        "to": args.webhook_to.strip(),
    }
    config = {
        "server_url": args.server_url,
        "private_key": args.private_key,
        "agent_id": agent_id,
        "relay_domain": relay_domain,
        "agent_address": f"{agent_address}@{relay_domain}" if relay_domain else agent_address,
        "webhook": webhook,
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
    hook = webhook_config(cfg)
    payload["webhook"] = {
        "enabled": webhook_enabled(cfg),
        "url": str(hook.get("url") or ""),
        "agentId": str(hook.get("agentId") or ""),
        "sessionKeyPrefix": str(hook.get("sessionKeyPrefix") or "agentrelay"),
        "deliver": bool(hook.get("deliver", False)),
        "channel": str(hook.get("channel") or ""),
    }
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


def cmd_inbox_list(args: argparse.Namespace) -> None:
    inbox = load_inbox()
    items = inbox.get("items", [])
    if not isinstance(items, list):
        items = []
    if args.limit > 0:
        items = items[: args.limit]
    print(json.dumps({"items": items}, ensure_ascii=False, indent=2))


def cmd_inbox_clear(_: argparse.Namespace) -> None:
    ensure_base_dirs()
    save_inbox({"items": []})
    print("cleared")


def cmd_daemon_run(_: argparse.Namespace) -> None:
    cfg = require_config()
    ensure_base_dirs()
    asyncio.run(daemon_loop(cfg))


def cmd_daemon_start(_: argparse.Namespace) -> None:
    print(json.dumps(start_daemon_background(), ensure_ascii=False, indent=2))


def cmd_daemon_stop(_: argparse.Namespace) -> None:
    if not DAEMON_PID_PATH.exists():
        print(json.dumps({"running": False}, ensure_ascii=False, indent=2))
        return
    try:
        payload = json.loads(DAEMON_PID_PATH.read_text(encoding="utf-8"))
    except Exception:
        payload = {}
    pid = int(payload.get("pid") or 0)
    if not process_alive(pid):
        with contextlib.suppress(FileNotFoundError):
            DAEMON_PID_PATH.unlink()
        print(json.dumps({"running": False}, ensure_ascii=False, indent=2))
        return
    os.kill(pid, signal.SIGTERM)
    for _ in range(20):
        if not process_alive(pid):
            break
        time.sleep(0.1)
    running = process_alive(pid)
    if not running:
        with contextlib.suppress(FileNotFoundError):
            DAEMON_PID_PATH.unlink()
    print(json.dumps({"running": running, "pid": pid}, ensure_ascii=False, indent=2))


def cmd_daemon_status(_: argparse.Namespace) -> None:
    ensure_base_dirs()
    pid = load_daemon_pid()
    state = load_runtime_state().get("daemon", {})
    result = {
        "running": process_alive(pid),
        "pid": pid,
        "pid_file": str(DAEMON_PID_PATH),
        "log_file": str(DAEMON_LOG_PATH),
        "state": state if isinstance(state, dict) else {},
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))


def inbox_items_for_target(agent_id: str, target: str) -> list[dict[str, Any]]:
    inbox = load_inbox()
    items = inbox.get("items", [])
    if not isinstance(items, list):
        return []
    if target.startswith("topic:"):
        return [item for item in items if isinstance(item, dict) and item.get("chat_id") == target]
    peer_id = parse_agent_address(target)["agent_id"] if "@" in target else normalize_agent_id(target)
    chat_id = make_dm_chat_id(agent_id, peer_id)
    return [
        item
        for item in items
        if isinstance(item, dict)
        and item.get("chat_id") == chat_id
        and str(item.get("from_id") or "") == peer_id
    ]


def cmd_chat(args: argparse.Namespace) -> None:
    cfg = require_config()
    target = normalize_target(resolve_target(args.target))
    daemon_info = start_daemon_background()
    print(f"interactive chat with {target}")
    print(f"daemon pid={daemon_info['pid']}")
    print("type message and press Enter, /quit to exit")

    stop_event = threading.Event()
    seen_ids = {
        str(item.get("id"))
        for item in inbox_items_for_target(cfg["agent_id"], target)
        if isinstance(item, dict) and item.get("id")
    }

    def _watch_inbox() -> None:
        while not stop_event.is_set():
            for item in inbox_items_for_target(cfg["agent_id"], target):
                item_id = str(item.get("id") or "")
                if not item_id or item_id in seen_ids:
                    continue
                seen_ids.add(item_id)
                sender = str(item.get("from_address") or item.get("from_id") or "peer")
                text = short_text(str(item.get("text") or ""))
                print(f"\n<{sender}> {text}")
            stop_event.wait(1.0)

    watcher = threading.Thread(target=_watch_inbox, daemon=True)
    watcher.start()
    try:
        while True:
            text = input("you> ").strip()
            if not text:
                continue
            if text.lower() in {"/quit", "/exit"}:
                break
            asyncio.run(send_message(cfg, target, text))
    finally:
        stop_event.set()
        watcher.join(timeout=1.0)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="AgentRelay local CLI")
    sub = parser.add_subparsers(dest="command", required=True)

    p_init = sub.add_parser("init", help="write local AgentRelay config")
    p_init.add_argument("--server-url", required=True, help="relay ws base, e.g. ws://127.0.0.1:8765")
    p_init.add_argument("--private-key", required=True, help="Ed25519 private key hex")
    p_init.add_argument("--agent-id", default="", help="optional public key hex override")
    p_init.add_argument("--webhook-url", default="", help="OpenClaw /hooks/agent URL")
    p_init.add_argument("--webhook-token", default="", help="OpenClaw hook bearer token")
    p_init.add_argument("--webhook-agent-id", default="", help="Optional OpenClaw agentId for /hooks/agent")
    p_init.add_argument("--webhook-session-key-prefix", default="agentrelay", help="Session key prefix for OpenClaw hook delivery")
    p_init.add_argument("--webhook-channel", default="", help="Optional OpenClaw delivery channel when webhook deliver=true")
    p_init.add_argument("--webhook-to", default="", help="Optional OpenClaw recipient when webhook deliver=true")
    p_init.add_argument("--webhook-deliver", action="store_true", help="Ask OpenClaw to deliver final replies to a channel")
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

    p_chat = sub.add_parser("chat", help="interactive chat with an agent or topic")
    p_chat.add_argument("target", help="contact name, agent id/address, or topic:name")
    p_chat.set_defaults(func=cmd_chat)

    p_allow = sub.add_parser("allow", help="allow another agent to DM you")
    p_allow.add_argument("target", help="contact name or agent id/address")
    p_allow.set_defaults(func=cmd_allow)

    p_subscribe = sub.add_parser("subscribe", help="subscribe to a topic")
    p_subscribe.add_argument("topic", help="topic name or topic:name")
    p_subscribe.set_defaults(func=cmd_subscribe)

    p_inbox = sub.add_parser("inbox", help="read local inbox")
    p_inbox_sub = p_inbox.add_subparsers(dest="inbox_command", required=True)
    p_inbox_list = p_inbox_sub.add_parser("list")
    p_inbox_list.add_argument("--limit", type=int, default=20)
    p_inbox_list.set_defaults(func=cmd_inbox_list)
    p_inbox_clear = p_inbox_sub.add_parser("clear")
    p_inbox_clear.set_defaults(func=cmd_inbox_clear)

    p_daemon = sub.add_parser("daemon", help="manage background receive daemon")
    p_daemon_sub = p_daemon.add_subparsers(dest="daemon_command", required=True)
    p_daemon_run = p_daemon_sub.add_parser("run")
    p_daemon_run.set_defaults(func=cmd_daemon_run)
    p_daemon_start = p_daemon_sub.add_parser("start")
    p_daemon_start.set_defaults(func=cmd_daemon_start)
    p_daemon_stop = p_daemon_sub.add_parser("stop")
    p_daemon_stop.set_defaults(func=cmd_daemon_stop)
    p_daemon_status = p_daemon_sub.add_parser("status")
    p_daemon_status.set_defaults(func=cmd_daemon_status)

    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
