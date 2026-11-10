"""The link to the owner's own browser: who may connect, and that approvals still apply."""
import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from conftest import Ctx
from sudo_agent.agent import TaskManager
from sudo_agent.config import Settings
from sudo_agent.events import EventBus
from sudo_agent.llm import ScriptedProvider
from sudo_agent.server import create_app
from sudo_agent.tools import execute_tool
from sudo_agent.tools.my_browser import ExtensionBridge

TOKEN, PORT, CODE = "t", 8765, "right-code"
HOST = {"host": f"127.0.0.1:{PORT}"}
EXT = "chrome-extension://abcdefghijklmnop"


@pytest.fixture
def client(memory, monkeypatch):
    monkeypatch.setenv("SUDO_PAIRING_CODE", CODE)
    manager = TaskManager(Settings(), memory, EventBus(), None, lambda s: ScriptedProvider([]))
    with TestClient(create_app(manager, memory, Settings(), TOKEN, PORT), base_url=f"http://127.0.0.1:{PORT}") as c:
        c.manager = manager
        yield c
    manager.close()


def test_extension_with_right_code_connects(client):
    with client.websocket_connect(f"/ext?code={CODE}", headers={**HOST, "origin": EXT}):
        assert client.manager.bridge.connected
    assert not client.manager.bridge.connected


@pytest.mark.parametrize("url,origin", [
    ("/ext?code=wrong", EXT),
    ("/ext", EXT),
    (f"/ext?code={CODE}", "https://evil.example"),
    (f"/ext?code={CODE}", f"http://127.0.0.1:{PORT}"),
])
def test_everything_else_is_refused(client, url, origin):
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect(url, headers={**HOST, "origin": origin}) as ws:
            ws.receive_json()
    assert not client.manager.bridge.connected


def test_wrong_code_tells_the_extension_why(client):
    with client.websocket_connect("/ext?code=wrong", headers={**HOST, "origin": EXT}) as ws:
        with pytest.raises(WebSocketDisconnect) as closed:
            ws.receive_json()
    assert closed.value.code == 4403


def test_right_code_gets_a_hello(client):
    with client.websocket_connect(f"/ext?code={CODE}", headers={**HOST, "origin": EXT}) as ws:
        assert ws.receive_json() == {"hello": True}


def test_no_origin_is_refused(client):
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect(f"/ext?code={CODE}", headers=HOST):
            pass


class FakeBridge:
    connected = True

    def __init__(self, describe=None):
        self.calls = []
        self.info = describe

    def request(self, cmd, timeout=40.0, **args):
        self.calls.append((cmd, args))
        if cmd == "describe":
            return self.info
        if cmd == "type":
            return args["text"]
        if cmd == "tabs":
            return {"rows": [{"tab": 7, "title": "Shop", "url": "https://shop.example", "active": True}], "hidden": 4,
                    "matched": 0 if args.get("match") == "nothing-like-this" else None}
        return {"title": "t", "url": "u"}


def linked(memory, info, approve=True):
    ctx = Ctx(memory, approve=approve)
    ctx.tool.bridge = FakeBridge(info)
    return ctx


FIELD = {"name": "Message", "tag": "input", "type": "text", "editable": False, "inForm": True, "searchForm": False, "submits": False, "sensitive": False}


def test_unlinked_browser_gives_a_clear_answer(ctx):
    result = execute_tool("my_read", {}, ctx.tool)
    assert not result.ok and "not connected" in result.text


def test_send_button_in_own_browser_needs_approval(memory):
    ctx = linked(memory, {**FIELD, "name": "Send", "tag": "button"}, approve=False)
    result = execute_tool("my_click", {"ref": "e1"}, ctx.tool)
    assert not result.ok and ctx.prompts and "click" not in [c[0] for c in ctx.tool.bridge.calls]


def test_typing_is_preparation_and_does_not_ask(memory):
    ctx = linked(memory, FIELD, approve=False)
    assert execute_tool("my_type", {"ref": "e1", "text": "draft ✓"}, ctx.tool).ok and not ctx.prompts


def test_enter_in_a_form_needs_approval(memory):
    ctx = linked(memory, FIELD, approve=False)
    assert not execute_tool("my_press", {"key": "Enter"}, ctx.tool).ok
    assert "press" not in [c[0] for c in ctx.tool.bridge.calls]


def test_password_field_refuses_plain_typing(memory):
    ctx = linked(memory, {**FIELD, "type": "password", "sensitive": True})
    result = execute_tool("my_type", {"ref": "e1", "text": "plain"}, ctx.tool)
    assert not result.ok and "my_type_secret" in result.text and "type" not in [c[0] for c in ctx.tool.bridge.calls]


def test_only_web_addresses_can_be_opened(memory):
    ctx = linked(memory, FIELD)
    assert not execute_tool("my_open", {"url": "chrome://settings"}, ctx.tool).ok
    assert not execute_tool("my_open", {"url": "file:///C:/x"}, ctx.tool).ok


def test_closing_their_tab_needs_approval(memory):
    ctx = linked(memory, FIELD, approve=False)
    assert not execute_tool("my_tabs", {"action": "close", "tab": 7}, ctx.tool).ok


def test_other_tabs_stay_private(memory):
    ctx = linked(memory, FIELD)
    listing = execute_tool("my_tabs", {"action": "list"}, ctx.tool)
    assert listing.ok and "tab 7: 'Shop'" in listing.text and "4 other tabs are private" in listing.text
    for broad in ("a", "http", "com"):  # Fishing for every tab is refused before it reaches the browser.
        assert not execute_tool("my_tabs", {"action": "switch", "match": broad}, ctx.tool).ok
    assert [c for c in ctx.tool.bridge.calls if c[0] == "tabs"] == [("tabs", {"action": "list", "tab": None, "match": "", "url": None})]
    assert not execute_tool("my_tabs", {"action": "switch", "match": "nothing-like-this"}, ctx.tool).ok
    assert not execute_tool("my_tabs", {"action": "switch"}, ctx.tool).ok


def test_open_my_browser_when_it_is_already_open(memory):
    ctx = linked(memory, FIELD)
    result = execute_tool("my_browser_open", {}, ctx.tool)
    assert result.ok and "already open" in result.text and ctx.tool.bridge.calls == [("focus", {})]


def test_off_permission_covers_own_browser(memory):
    ctx = linked(memory, FIELD)
    ctx.settings.permissions["browser"] = "off"
    assert "turned off" in execute_tool("my_read", {}, ctx.tool).text


def test_disconnect_fails_waiting_requests_quickly():
    from concurrent.futures import Future

    bridge = ExtensionBridge()

    class Socket:
        pass

    socket = Socket()
    bridge.attach(socket, None)
    future = Future()
    bridge._pending["x"] = future
    bridge.detach(socket)
    assert isinstance(future.exception(timeout=1), ConnectionError) and not bridge.connected


def test_focus_mode_closes_whole_screen_tools_and_needs_a_yes_to_end(memory):
    ctx = linked(memory, FIELD, approve=False)
    ctx.tool.bridge.focus_area = "registration form"
    for name in ("desktop_screenshot", "window_list", "ui_elements"):
        result = execute_tool(name, {}, ctx.tool)
        assert not result.ok and "Focus mode is on" in result.text
    assert not execute_tool("my_focus", {"action": "clear"}, ctx.tool).ok and ctx.prompts
    assert "scope_clear" not in [c[0] for c in ctx.tool.bridge.calls]
    assert not execute_tool("my_focus", {"action": "set"}, ctx.tool).ok
