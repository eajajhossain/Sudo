import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from sudo_agent.agent import TaskManager
from sudo_agent.config import Settings
from sudo_agent.events import EventBus
from sudo_agent.llm import ScriptedProvider
from sudo_agent.server import create_app

TOKEN, PORT = "test-token-abc", 8765
BASE = f"http://127.0.0.1:{PORT}"
AUTH = {"x-sudo-token": TOKEN}


@pytest.fixture
def client(memory):
    manager = TaskManager(Settings(), memory, EventBus(), None, lambda s: ScriptedProvider([]))
    app = create_app(manager, memory, Settings(), TOKEN, PORT)
    with TestClient(app, base_url=BASE) as c:
        yield c
    manager.close()


def test_api_requires_token(client):
    assert client.get("/api/state").status_code == 403
    assert client.get("/api/state", headers={"x-sudo-token": "wrong"}).status_code == 403
    assert client.get("/api/state", headers=AUTH).status_code == 200


def test_commands_require_token(client):
    assert client.post("/api/command", json={"text": "delete everything"}).status_code == 403


def test_other_websites_are_refused_even_with_the_token(client):
    response = client.get("/api/state", headers={**AUTH, "origin": "https://evil.example"})
    assert response.status_code == 403


def test_dns_rebinding_host_is_refused(client):
    assert client.get("/api/state", headers={**AUTH, "host": "evil.example:8765"}).status_code == 403
    assert client.get("/", headers={"host": "evil.example:8765"}).status_code == 403


def test_websocket_needs_token_and_origin(client):
    host = {"host": f"127.0.0.1:{PORT}"}
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect("/ws", headers=host):
            pass
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect(f"/ws?token={TOKEN}", headers={**host, "origin": "https://evil.example"}):
            pass
    with client.websocket_connect(f"/ws?token={TOKEN}", headers={**host, "origin": BASE}):
        pass


def test_page_has_a_strict_content_policy(client):
    response = client.get("/")
    assert response.status_code == 200
    assert "script-src 'self'" in response.headers["content-security-policy"]


def test_command_validation(client):
    assert client.post("/api/command", headers=AUTH, json={"text": "  "}).status_code == 400
    assert client.post("/api/command", headers=AUTH, json={"text": "x", "mode": "odd"}).status_code == 400


def test_memory_api_inspect_correct_delete(client):
    created = client.post("/api/memories", headers=AUTH, json={"kind": "fact", "title": "A", "content": "B"}).json()
    listed = client.get("/api/memories", headers=AUTH).json()["items"]
    assert listed[0]["id"] == created["id"]
    patched = client.patch(f"/api/memories/{created['id']}", headers=AUTH, json={"content": "C"}).json()
    assert patched["content"] == "C"
    assert client.delete(f"/api/memories/{created['id']}", headers=AUTH).status_code == 200
    assert client.get("/api/memories", headers=AUTH).json()["items"] == []


def test_memory_api_refuses_secrets(client):
    response = client.post("/api/memories", headers=AUTH, json={"kind": "fact", "title": "x", "content": "password: hunter2222"})
    assert response.status_code == 422


def test_settings_never_return_key_values(client):
    try:
        response = client.put("/api/keys/openai", headers=AUTH, json={"value": "sudo-fake-value-123"})
        assert response.status_code == 200 and "sudo-fake-value-123" not in response.text
        assert response.json()["keys"]["openai"] is True
    finally:
        response = client.delete("/api/keys/openai", headers=AUTH)
    assert response.json()["keys"]["openai"] is False


def test_settings_validation(client):
    assert client.put("/api/settings", headers=AUTH, json={"autonomy": "reckless"}).status_code == 422
    assert client.put("/api/settings", headers=AUTH, json={"bogus": 1}).status_code == 422


def test_unknown_prompt_404(client):
    assert client.post("/api/prompts/none", headers=AUTH, json={"value": True}).status_code == 404
