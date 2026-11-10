"""Drives the real interface in headless Chrome against the real server."""
import socket
import threading
import time

import pytest
import uvicorn
from playwright.sync_api import expect, sync_playwright

from sudo_agent.agent import TaskManager
from sudo_agent.config import Settings
from sudo_agent.events import EventBus
from sudo_agent.llm import ScriptedProvider
from sudo_agent.server import create_app

pytestmark = pytest.mark.live
TOKEN = "ui-test-token"


@pytest.fixture
def app(memory, tmp_path):
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    target = tmp_path / "doomed.txt"
    target.write_text("x")
    proof = tmp_path / "proof.txt"
    provider = ScriptedProvider([
        [("plan_update", {"steps": [{"text": "Remove the file", "status": "doing"}, {"text": "Write proof", "status": "todo"}]})],
        ["Removing it now."],
    ])
    provider.turns = [
        provider.turns[0],
        [("file_delete", {"path": str(target)})],
        [("file_write", {"path": str(proof), "content": "<img src=x onerror=window.__xss=1>"})],
        [("task_finish", {"status": "done", "summary": "File removed <b>safely</b>.", "checks": [
            {"type": "file_absent", "path": str(target)}, {"type": "file_exists", "path": str(proof)}]})],
    ]
    settings = Settings()
    manager = TaskManager(settings, memory, EventBus(), None, lambda s: provider)
    server = uvicorn.Server(uvicorn.Config(create_app(manager, memory, settings, TOKEN, port), host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    while not server.started:
        time.sleep(0.02)
    yield {"url": f"http://127.0.0.1:{port}/?token={TOKEN}", "target": target, "manager": manager}
    server.should_exit = True
    manager.close()
    thread.join(5)


@pytest.fixture
def page():
    with sync_playwright() as pw:
        browser = pw.chromium.launch(channel="chrome", headless=True)
        page = browser.new_page(viewport={"width": 1320, "height": 840})
        page.problems = []
        page.on("pageerror", lambda e: page.problems.append(str(e)))
        page.on("console", lambda m: page.problems.append(m.text) if m.type == "error" else None)
        yield page
        browser.close()


def overflow(page):
    return page.evaluate("() => document.documentElement.scrollWidth - document.documentElement.clientWidth")


def test_full_flow_with_approval(app, page, tmp_path):
    page.goto(app["url"])
    expect(page.locator("#link")).to_have_text("connected")
    assert "token" not in page.url
    page.screenshot(path=str(tmp_path / "idle.png"))
    page.fill("#input", "Delete the old file")
    page.keyboard.press("Enter")
    expect(page.locator(".msg.owner")).to_have_text("Delete the old file")

    dialog = page.locator(".dialog")
    expect(dialog).to_be_visible()
    expect(page.locator("#d-kind")).to_have_text("APPROVAL NEEDED")
    expect(page.locator("#d-title")).to_have_text("Moving to Recycle Bin")
    assert page.evaluate("() => document.activeElement.id") == "d-no", "safe choice takes focus"
    expect(page.locator("#plan li")).to_have_count(2)
    assert app["target"].exists()
    page.screenshot(path=str(tmp_path / "approval.png"))
    page.click("#d-yes")

    badge = page.locator(".badge.verified")
    expect(badge).to_have_text("VERIFIED")
    expect(page.locator(".result")).to_contain_text("File removed <b>safely</b>.")
    assert not app["target"].exists()
    expect(page.locator(".checks li")).to_have_count(2)
    expect(page.locator("#status")).to_have_text("Idle")
    assert page.locator("#actions li").count() >= 2
    assert page.evaluate("() => window.__xss") is None
    page.screenshot(path=str(tmp_path / "done.png"))
    assert not page.problems, page.problems


def test_memory_and_settings_screens(app, page):
    page.goto(app["url"])
    expect(page.locator("#link")).to_have_text("connected")
    page.click('[data-view="memory"]')
    page.fill("#m-title", "Editor")
    page.fill("#m-content", "Uses VS Code")
    page.click("#memory-form button")
    expect(page.locator("#memory-list .item")).to_have_count(1)
    page.fill("#m-title", "Bad")
    page.fill("#m-content", "password: hunter2222")
    page.click("#memory-form button")
    expect(page.locator("#memory-error")).to_contain_text("not saved")
    page.locator("#memory-list").get_by_text("Delete").click()
    expect(page.locator("#memory-list .item")).to_have_count(0)
    page.click('[data-view="settings"]')
    expect(page.locator('[name="model"]')).to_have_value("claude-opus-5-5")
    page.click('[data-view="tasks"]')
    page.click('[data-view="operate"]')
    expect(page.locator("#input")).to_be_visible()


def test_chat_comes_back_after_reopening(app, page, memory):
    task = memory.create_task("What is the capital of France?")
    memory.set_task(task, status="answered", summary="Paris.")
    other = memory.create_task("Clean the folder")
    memory.set_task(other, status="done", verdict="verified", summary="Cleaned.")
    page.goto(app["url"])
    expect(page.locator("#link")).to_have_text("connected")
    expect(page.locator(".msg.owner")).to_have_count(2)
    expect(page.locator(".msg.assistant")).to_have_text("Paris.")
    expect(page.locator(".badge.verified")).to_have_text("VERIFIED")
    page.reload()
    expect(page.locator("#link")).to_have_text("connected")
    expect(page.locator(".msg.owner")).to_have_count(2)


def test_agent_indicator_and_permissions_screen(app, page):
    page.goto(app["url"])
    expect(page.locator("#agents li")).to_have_count(7)
    page.click('[data-view="settings"]')
    picker = page.locator('[name="perm:files"]')
    expect(picker).to_have_value("auto")
    picker.select_option("ask")
    page.click("#settings-form .primary")
    expect(page.locator("#settings-note")).to_have_text("Saved.")
    page.click('[data-view="tasks"]')
    page.click('[data-view="settings"]')
    expect(page.locator('[name="perm:files"]')).to_have_value("ask")
    page.locator('[name="provider"]').select_option("openai")
    page.click("#settings-form .primary")
    expect(page.locator('[name="model"]')).to_have_value("gpt-6.1-terra")


def test_agent_chip_lights_up_during_work(app, page):
    page.goto(app["url"])
    expect(page.locator("#link")).to_have_text("connected")
    page.fill("#input", "Delete the old file")
    page.keyboard.press("Enter")
    expect(page.locator(".dialog")).to_be_visible()
    expect(page.locator("#agents li.on")).to_have_text("Files")
    page.click("#d-no")


@pytest.mark.parametrize("width,height", [(1320, 840), (820, 1100), (390, 780), (320, 560)])
def test_layout_has_no_sideways_scroll(app, page, width, height, tmp_path):
    page.set_viewport_size({"width": width, "height": height})
    page.goto(app["url"])
    expect(page.locator("#link")).to_have_text("connected")
    for view in ("tasks", "memory", "settings", "operate"):
        page.click(f'[data-view="{view}"]')
        assert overflow(page) <= 0, (view, width)
    box = page.locator("#send").bounding_box()
    assert box and box["y"] + box["height"] <= height and box["x"] + box["width"] <= width
    page.screenshot(path=str(tmp_path / f"operate-{width}.png"))
    page.click('[data-view="settings"]')
    page.wait_for_timeout(250)
    page.screenshot(path=str(tmp_path / f"settings-{width}.png"))


def test_activity_panel_opens_on_small_screens(app, page):
    page.set_viewport_size({"width": 800, "height": 900})
    page.goto(app["url"])
    expect(page.locator("#link")).to_be_attached()
    expect(page.locator("#status")).to_have_text("Idle")
    assert not page.locator("#side").is_visible()
    page.click("#side-toggle")
    expect(page.locator("#side")).to_be_visible()
    page.click("#side-close")
    expect(page.locator("#side")).to_be_hidden()


def test_suggestion_fills_the_command_box(app, page):
    page.goto(app["url"])
    expect(page.locator("#link")).to_have_text("connected")
    page.locator(".suggestion").first.click()
    expect(page.locator("#input")).to_have_value("Find out why my computer is slow and fix it")
