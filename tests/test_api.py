import importlib.util
from pathlib import Path
import sys
import json
import secrets

import pytest
from fastapi.testclient import TestClient
from jinja2 import Environment, FileSystemLoader, select_autoescape
from nacl.encoding import HexEncoder
from nacl.signing import SigningKey


PROJECT_DIR = Path("/Users/yong.feng/Bright/Project/nanobot/AgentRelay")


def load_module(module_name: str):
    module_path = PROJECT_DIR / f"{module_name}.py"
    if str(PROJECT_DIR) not in sys.path:
        sys.path.insert(0, str(PROJECT_DIR))
    spec = importlib.util.spec_from_file_location(f"test_{module_name}", module_path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


@pytest.fixture()
def relay_module(tmp_path):
    module = load_module("agent_relay")
    module.DB_PATH = tmp_path / "channel.db"
    module.STATIC_DIR = PROJECT_DIR / "static"
    module.TEMPLATES_DIR = PROJECT_DIR / "templates"
    module.LOGO_PATH = module.STATIC_DIR / "lobs.cc.png"
    module.RELAY_DOMAIN = "test.local"
    module.RELAY_WS_BASE = "ws://127.0.0.1:8765"
    module.RELAY_FED_BASE = "http://127.0.0.1:8765"
    module.sessions.clear()
    module.relay_sessions.clear()
    module.web_sessions.clear()
    module.pending_web_logins.clear()
    module.templates_env = Environment(
        loader=FileSystemLoader(str(module.TEMPLATES_DIR)),
        autoescape=select_autoescape(["html", "xml"]),
    )
    module.init_db()
    return module


@pytest.fixture()
def web_module(tmp_path):
    module = load_module("web_server")
    module.DB_PATH = tmp_path / "channel.db"
    module.STATIC_DIR = PROJECT_DIR / "static"
    module.TEMPLATES_DIR = PROJECT_DIR / "templates"
    module.LOGO_PATH = module.STATIC_DIR / "lobs.cc.png"
    module.RELAY_DOMAIN = "test.local"
    module.RELAY_WS_BASE = "ws://127.0.0.1:8765"
    module.RELAY_FED_BASE = "http://127.0.0.1:8765"
    module.sessions.clear()
    module.relay_sessions.clear()
    module.web_sessions.clear()
    module.pending_web_logins.clear()
    module.templates_env = Environment(
        loader=FileSystemLoader(str(module.TEMPLATES_DIR)),
        autoescape=select_autoescape(["html", "xml"]),
    )
    module.init_db()
    return module


def make_message(module, *, event_id: str, sender: str, receiver: str, content: str, created_at: int = 1773200000):
    event = {
        "id": event_id,
        "from": sender,
        "chat": {"id": module.make_dm_chat_id(sender, receiver), "type": module.DM_CHAT_TYPE},
        "kind": "message",
        "created_at": created_at,
        "content": content,
        "content_type": "text/plain",
        "attachments": [],
        "metadata": {},
    }
    module.create_message(event, "sig-placeholder", [receiver])
    module.mark_delivered(event_id, receiver)
    module.mark_read(event_id, receiver)


def signing_key_from_hex(private_key_hex: str) -> SigningKey:
    return SigningKey(private_key_hex, encoder=HexEncoder)


def agent_id_from_private_key(private_key_hex: str) -> str:
    key = signing_key_from_hex(private_key_hex)
    return key.verify_key.encode(encoder=HexEncoder).decode()


def sign_auth(module, private_key_hex: str, challenge_packet: dict) -> dict:
    signing_key = signing_key_from_hex(private_key_hex)
    payload = f"AUTH|{challenge_packet['nonce']}|{challenge_packet['ts']}".encode()
    sig = signing_key.sign(payload).signature
    return {
        "type": "auth",
        "agent_id": signing_key.verify_key.encode(encoder=HexEncoder).decode(),
        "sig": module.base64.b64encode(sig).decode("ascii"),
    }


def signed_event_packet(module, private_key_hex: str, event: dict) -> dict:
    signing_key = signing_key_from_hex(private_key_hex)
    sig = signing_key.sign(module.canonical_event_payload(event)).signature
    return {
        "type": "event",
        "event": event,
        "sig": module.base64.b64encode(sig).decode("ascii"),
    }


def test_relay_health_endpoint(relay_module):
    client = TestClient(relay_module.app)

    response = client.get("/health")

    assert response.status_code == 200
    payload = response.json()
    assert payload["ok"] is True
    assert payload["relay_domain"] == "test.local"
    assert "agents_online" in payload


def test_web_homepage_renders(web_module):
    client = TestClient(web_module.app)

    response = client.get("/")

    assert response.status_code == 200
    text = response.text
    assert "Lobster Commuincation Cluster" in text
    assert "Agent login" in text


def test_web_api_relay_returns_discovery(web_module):
    client = TestClient(web_module.app)

    response = client.get("/api/relay")

    assert response.status_code == 200
    payload = response.json()
    assert payload["relay_domain"] == "test.local"
    assert payload["ws_endpoint"] == "ws://127.0.0.1:8765/ws/agent"
    assert payload["fed_ws_endpoint"] == "ws://127.0.0.1:8765/ws/federation"


def test_web_api_agents_returns_visible_agents_with_online_state(web_module):
    agent_id = "6ed60419f60b078c67837714d740016154ee70d3aca8203cdae040ad2876b5ee"
    web_module.upsert_agent_directory(agent_id, last_seen=True)
    web_module.set_agent_presence(agent_id, True)

    client = TestClient(web_module.app)
    response = client.get("/api/agents")

    assert response.status_code == 200
    items = response.json()["items"]
    assert len(items) == 1
    assert items[0]["agent_id"] == agent_id
    assert items[0]["online"] is True
    assert items[0]["agent_address"].endswith("@test.local")


def test_web_api_topics_returns_join_metadata(web_module):
    owner_id = "6ed60419f60b078c67837714d740016154ee70d3aca8203cdae040ad2876b5ee"
    web_module.set_topic_subscription("topic:team-alpha", owner_id, True, "evt-sub-1")

    client = TestClient(web_module.app)
    response = client.get("/api/topics")

    assert response.status_code == 200
    items = response.json()["items"]
    assert len(items) == 1
    topic = items[0]
    assert topic["topic_id"] == "topic:team-alpha"
    assert topic["title"] == "team-alpha"
    assert topic["visibility"] == "public"
    assert topic["join_mode"] == "open"
    assert topic["topic_owner_id"] == owner_id
    assert topic["topic_owner_address"].endswith("@test.local")
    assert topic["can_subscribe_directly"] is True
    assert topic["can_request_join"] is False


def test_web_api_messages_returns_dm_history(web_module):
    sender = "d03ecba46e21d327e9c3f83cde7f652c4e2ea497337860e5222a1a6f1bf3523f"
    receiver = "6ed60419f60b078c67837714d740016154ee70d3aca8203cdae040ad2876b5ee"
    make_message(
        web_module,
        event_id="evt-msg-1",
        sender=sender,
        receiver=receiver,
        content="hello from test",
    )

    client = TestClient(web_module.app)
    response = client.get("/api/messages", params={"agent_id": receiver, "peer_id": sender})

    assert response.status_code == 200
    items = response.json()["items"]
    assert len(items) == 1
    item = items[0]
    assert item["id"] == "evt-msg-1"
    assert item["text"] == "hello from test"
    assert item["chat_type"] == "dm"
    assert item["status"] == "read"


def test_web_api_topic_page_requires_subscription(web_module):
    owner = "6ed60419f60b078c67837714d740016154ee70d3aca8203cdae040ad2876b5ee"
    viewer = "d03ecba46e21d327e9c3f83cde7f652c4e2ea497337860e5222a1a6f1bf3523f"
    web_module.set_topic_subscription("topic:team-alpha", owner, True, "evt-sub-1")
    session_token = "session-1"
    web_module.web_sessions[session_token] = viewer

    client = TestClient(web_module.app)
    response = client.get("/topic", params={"chat_id": "topic:team-alpha"}, cookies={"session": session_token})

    assert response.status_code == 200
    assert "Topic Access Denied" in response.text


def test_ws_agent_auth_and_acl_allow(relay_module):
    client = TestClient(relay_module.app)
    sender_priv = "7c8da769a5b9cf5f121e406328b2b4d547ba90e2f09687a441929488c9f7c7c7"
    receiver_priv = "2a4af8e3f1e39913e22911dad45a9a22ecb51eb7aca127c93339f212bdc94694"
    sender_id = agent_id_from_private_key(sender_priv)
    receiver_id = agent_id_from_private_key(receiver_priv)

    with client.websocket_connect("/ws/agent") as ws:
        challenge = ws.receive_json()
        assert challenge["type"] == "challenge"
        ws.send_text(json.dumps(sign_auth(relay_module, receiver_priv, challenge)))
        connected = ws.receive_json()
        assert connected == {"type": "connected", "agent_id": receiver_id}

        acl_event = {
            "id": "evt-acl-1",
            "from": receiver_id,
            "chat": {"id": f"system:{receiver_id}", "type": relay_module.SYSTEM_CHAT_TYPE},
            "kind": "acl_allow",
            "created_at": relay_module.now_ts(),
            "content": sender_id,
            "content_type": "text/plain",
            "attachments": [],
            "metadata": {},
        }
        ws.send_text(json.dumps(signed_event_packet(relay_module, receiver_priv, acl_event)))
        ack = ws.receive_json()
        assert ack["type"] == "ack"
        assert ack["status"] == "accepted"

    assert relay_module.is_allowed_for_message(sender_id, receiver_id) is True


def test_ws_agent_message_delivery_and_ack(relay_module):
    client = TestClient(relay_module.app)
    sender_priv = "7c8da769a5b9cf5f121e406328b2b4d547ba90e2f09687a441929488c9f7c7c7"
    receiver_priv = "2a4af8e3f1e39913e22911dad45a9a22ecb51eb7aca127c93339f212bdc94694"
    sender_id = agent_id_from_private_key(sender_priv)
    receiver_id = agent_id_from_private_key(receiver_priv)
    relay_module.set_acl_allow(receiver_id, sender_id, True, "seed-acl")

    with client.websocket_connect("/ws/agent") as receiver_ws:
        receiver_challenge = receiver_ws.receive_json()
        receiver_ws.send_text(json.dumps(sign_auth(relay_module, receiver_priv, receiver_challenge)))
        assert receiver_ws.receive_json()["type"] == "connected"

        with client.websocket_connect("/ws/agent") as sender_ws:
            sender_challenge = sender_ws.receive_json()
            sender_ws.send_text(json.dumps(sign_auth(relay_module, sender_priv, sender_challenge)))
            assert sender_ws.receive_json()["type"] == "connected"

            message_event = {
                "id": "evt-msg-ws-1",
                "from": sender_id,
                "chat": {"id": relay_module.make_dm_chat_id(sender_id, receiver_id), "type": relay_module.DM_CHAT_TYPE},
                "kind": "message",
                "created_at": relay_module.now_ts(),
                "content": "hello over websocket",
                "content_type": "text/plain",
                "attachments": [],
                "metadata": {},
            }
            sender_ws.send_text(json.dumps(signed_event_packet(relay_module, sender_priv, message_event)))
            send_ack = sender_ws.receive_json()
            assert send_ack["type"] == "ack"
            assert send_ack["status"] == "accepted"

            delivered = receiver_ws.receive_json()
            assert delivered["type"] == "deliver"
            assert delivered["event"]["id"] == "evt-msg-ws-1"
            assert delivered["event"]["content"] == "hello over websocket"

            ack_event = {
                "id": "evt-read-1",
                "from": receiver_id,
                "chat": {"id": relay_module.make_dm_chat_id(sender_id, receiver_id), "type": relay_module.DM_CHAT_TYPE},
                "kind": "ack",
                "created_at": relay_module.now_ts(),
                "content": "evt-msg-ws-1",
                "content_type": "text/plain",
                "attachments": [],
                "metadata": {},
            }
            receiver_ws.send_text(json.dumps(signed_event_packet(relay_module, receiver_priv, ack_event)))
            read_ack = receiver_ws.receive_json()
            assert read_ack["type"] == "ack"
            assert read_ack["status"] == "accepted"

    messages = relay_module.list_messages(receiver_id, sender_id)
    assert len(messages) == 1
    assert messages[0]["status"] == "read"


def test_ws_agent_chat_subscribe_creates_topic_metadata(relay_module):
    client = TestClient(relay_module.app)
    agent_priv = "2a4af8e3f1e39913e22911dad45a9a22ecb51eb7aca127c93339f212bdc94694"
    agent_id = agent_id_from_private_key(agent_priv)

    with client.websocket_connect("/ws/agent") as ws:
        challenge = ws.receive_json()
        ws.send_text(json.dumps(sign_auth(relay_module, agent_priv, challenge)))
        assert ws.receive_json()["type"] == "connected"

        subscribe_event = {
            "id": f"evt-sub-{secrets.token_hex(4)}",
            "from": agent_id,
            "chat": {"id": "topic:team-alpha", "type": relay_module.TOPIC_CHAT_TYPE},
            "kind": "chat_subscribe",
            "created_at": relay_module.now_ts(),
            "content": "",
            "content_type": "text/plain",
            "attachments": [],
            "metadata": {},
        }
        ws.send_text(json.dumps(signed_event_packet(relay_module, agent_priv, subscribe_event)))
        ack = ws.receive_json()
        assert ack["type"] == "ack"
        assert ack["status"] == "accepted"

    topics = relay_module.list_topics()
    assert len(topics) == 1
    topic = topics[0]
    assert topic["topic_id"] == "topic:team-alpha"
    assert topic["topic_owner_id"] == agent_id
    assert topic["can_subscribe_directly"] is True
