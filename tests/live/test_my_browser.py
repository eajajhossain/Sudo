"""Loads the real extension into a real browser on a throwaway profile and pairs it through
its own popup. Uses only the local test site, never the owner's real profile."""
import functools
import hashlib
import socket
import subprocess
import threading
import time
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
import uvicorn
from playwright.sync_api import sync_playwright

from conftest import Ctx
from sudo_agent.agent import TaskManager
from sudo_agent.config import ROOT, Settings
from sudo_agent.control import kill_process_tree
from sudo_agent.events import EventBus
from sudo_agent.llm import ScriptedProvider
from sudo_agent.server import create_app
from sudo_agent.tools import execute_tool
from sudo_agent.tools.browser import find_browser

pytestmark = pytest.mark.live
SITE = Path(__file__).parent / "fixture_site"
CODE = "live-pairing-code"


def free_port():
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


class Quiet(SimpleHTTPRequestHandler):
    def log_message(self, *a):
        pass


@pytest.fixture
def linked(memory, tmp_path, monkeypatch):
    monkeypatch.setenv("SUDO_PAIRING_CODE", CODE)
    site = ThreadingHTTPServer(("127.0.0.1", 0), functools.partial(Quiet, directory=str(SITE)))
    threading.Thread(target=site.serve_forever, daemon=True).start()
    port = free_port()
    manager = TaskManager(Settings(), memory, EventBus(), None, lambda s: ScriptedProvider([]))
    server = uvicorn.Server(uvicorn.Config(create_app(manager, memory, Settings(), "t", port), host="127.0.0.1", port=port, log_level="warning"))
    threading.Thread(target=server.run, daemon=True).start()
    while not server.started:
        time.sleep(0.02)
    cdp = free_port()
    extension = str(ROOT / "browser_extension")
    process = subprocess.Popen([find_browser("brave"), f"--remote-debugging-port={cdp}", f"--user-data-dir={tmp_path / 'profile'}",
                                "--no-first-run", "--no-default-browser-check", f"--load-extension={extension}",
                                f"--disable-extensions-except={extension}", "about:blank"])
    pw = sync_playwright().start()
    try:
        deadline = time.monotonic() + 25
        browser = None
        while browser is None:
            try:
                browser = pw.chromium.connect_over_cdp(f"http://127.0.0.1:{cdp}", timeout=3000)
            except Exception:
                assert time.monotonic() < deadline, "browser never opened"
                time.sleep(0.3)
        context = browser.contexts[0]
        # An unpacked extension's ID comes from its folder path. Its background worker can be asleep.
        digest = hashlib.sha256(extension.encode("utf-16-le")).hexdigest()[:32]
        extension_id = "".join(chr(ord("a") + int(ch, 16)) for ch in digest)
        popup = context.new_page()
        while True:
            try:
                popup.goto(f"chrome-extension://{extension_id}/popup.html", timeout=5000)
                popup.wait_for_selector("#code", timeout=3000)
                break
            except Exception:
                assert time.monotonic() < deadline, "extension did not load"
                time.sleep(0.5)
        popup.fill("#code", CODE)
        popup.fill("#port", str(port))
        popup.click("button")
        while not manager.bridge.connected:
            assert time.monotonic() < deadline, "extension never connected"
            time.sleep(0.1)
        popup.close()
        ctx = Ctx(memory)
        ctx.tool.bridge = manager.bridge
        page = context.pages[0]
        yield {"ctx": ctx, "site": f"http://127.0.0.1:{site.server_address[1]}", "page": page, "context": context,
               "popup_url": f"chrome-extension://{extension_id}/popup.html", "manager": manager, "port": port}
    finally:
        pw.stop()
        kill_process_tree(process.pid)
        server.should_exit = True
        manager.close()
        site.shutdown()


def run(ctx, tool_name, /, **args):
    result = execute_tool(tool_name, args, ctx.tool)
    assert result.ok, result.text
    return result


def ref(ctx, needle):
    text = run(ctx, "my_snapshot").text
    return next(l.split()[0] for l in text.splitlines() if needle in l)


def test_popup_explains_each_connection_problem(linked):
    from playwright.sync_api import expect

    popup = linked["context"].new_page()
    popup.goto(linked["popup_url"])
    expect(popup.locator("#code")).to_have_attribute("type", "text")
    expect(popup.locator("#state")).to_have_text("Connected to SUDO.")
    popup.fill("#code", "not-the-code")
    popup.click("button")
    expect(popup.locator("#state")).to_contain_text("Wrong pairing code", timeout=8000)
    assert not linked["manager"].bridge.connected
    popup.fill("#code", CODE)
    popup.fill("#port", "9")
    popup.click("button")
    expect(popup.locator("#state")).to_contain_text("Can't reach SUDO", timeout=8000)
    popup.fill("#code", f"  {CODE}\n")
    popup.fill("#port", str(linked["port"]))
    popup.click("button")
    expect(popup.locator("#state")).to_have_text("Connected to SUDO.", timeout=8000)
    assert linked["manager"].bridge.connected
    popup.click("button")  # Pressing Connect again with the same code must still work.
    expect(popup.locator("#state")).to_have_text("Connected to SUDO.", timeout=8000)
    popup.close()


def test_real_extension_operates_the_browser(linked):
    ctx, site, page = linked["ctx"], linked["site"], linked["page"]
    page.bring_to_front()
    assert "SUDO Test Home" in run(ctx, "my_open", url=site + "/index.html").text
    run(ctx, "my_type", ref=ref(ctx, "textarea"), text='Line one ✓ 日本語 🚀\nline "two"')
    assert page.eval_on_selector("#notes", "e => e.value") == 'Line one ✓ 日本語 🚀\nline "two"'
    pre = ref(ctx, "'Prefilled'")
    run(ctx, "my_type", ref=pre, text=" more", clear=False)
    assert page.eval_on_selector("#pre", "e => e.value") == "The quick brown fox more"
    run(ctx, "my_click", ref=ref(ctx, "Add one"))
    assert page.inner_text("#count") == "1"
    run(ctx, "my_press", key="Control+k")
    assert page.inner_text("#kbd") == "shortcut worked"
    run(ctx, "my_select", ref=ref(ctx, "select"), label="Green")
    assert page.eval_on_selector("#colour", "e => e.value") == "Green"
    assert "IGNORE ALL" in run(ctx, "my_read").text
    assert run(ctx, "my_screenshot").images
    assert "Second Page" in run(ctx, "my_click", ref=ref(ctx, "Go to second")).text


def test_search_enter_runs_and_form_submit_is_gated(linked):
    ctx, site = linked["ctx"], linked["site"]
    linked["page"].bring_to_front()
    run(ctx, "my_open", url=site + "/index.html")
    run(ctx, "my_type", ref=ref(ctx, "type=search"), text="hello")
    assert "Search Results" in run(ctx, "my_press", key="Enter").text and not ctx.prompts
    run(ctx, "my_open", url=site + "/index.html")
    ctx.approve = False
    declined = execute_tool("my_click", {"ref": ref(ctx, "Send message")}, ctx.tool)
    assert not declined.ok and ctx.prompts and "SUDO Test Home" in run(ctx, "my_read").text


def test_masked_password_and_tabs(linked):
    ctx, site, page = linked["ctx"], linked["site"], linked["page"]
    page.bring_to_front()
    run(ctx, "my_open", url=site + "/index.html")
    password = ref(ctx, "type=password")
    assert "my_type_secret" in execute_tool("my_type", {"ref": password, "text": "plain"}, ctx.tool).text
    ctx.approve = "correct-horse-battery"
    assert "correct-horse" not in run(ctx, "my_type_secret", ref=password, label="Test").text
    assert page.eval_on_selector("[name=secret]", "e => e.value") == "correct-horse-battery"
    ctx.approve = True
    assert "Second Page" in run(ctx, "my_tabs", action="new", url=site + "/second.html").text
    rows = [l for l in run(ctx, "my_tabs", action="list").text.splitlines() if " tab " in l[:6]]
    assert len(rows) >= 2
    second = int(next(l for l in rows if "Second Page" in l).split()[2].rstrip(":"))
    run(ctx, "my_tabs", action="close", tab=second)
    assert "Second Page" not in run(ctx, "my_tabs", action="list").text


def test_only_the_tabs_the_owner_mentions_are_shown(linked):
    ctx, site, page = linked["ctx"], linked["site"], linked["page"]
    for name in ("second.html", "search.html?q=private-thing"):  # The owner's other tabs.
        linked["context"].new_page().goto(f"{site}/{name}")
    page.bring_to_front()
    run(ctx, "my_open", url=site + "/index.html")
    listing = run(ctx, "my_tabs", action="list").text
    assert "SUDO Test Home" in listing and "2 other tabs are private" in listing
    assert "Second Page" not in listing and "private-thing" not in listing
    hidden = next(p for p in linked["context"].pages if p.url.endswith("second.html"))
    hidden_id = run(ctx, "my_tabs", action="list").text  # Guessing a hidden tab's number must not work either.
    assert not execute_tool("my_tabs", {"action": "switch", "tab": 1}, ctx.tool).ok or "Second Page" not in hidden_id
    switched = run(ctx, "my_tabs", action="switch", match="Second Page").text
    assert "Second Page" in switched and "1 other tabs are private" in switched and "private-thing" not in switched
    assert "Second Page" in run(ctx, "my_read").text and hidden.url.endswith("second.html")


def test_form_inside_a_cross_origin_frame(linked):
    """Like ChatterApply: the outer page is fixed and the real form is an embedded page from another origin.
    Playwright cannot reach cross-origin frames over this CDP link, so results are read from the page's own DOM."""
    ctx, site, page = linked["ctx"], linked["site"], linked["page"]
    page.bring_to_front()
    run(ctx, "my_open", url=site + "/framed.html")
    for _ in range(50):
        if "Contact number" in run(ctx, "my_snapshot").text:
            break
        time.sleep(0.1)
    snapshot = run(ctx, "my_snapshot").text
    phone = next(l.split()[0] for l in snapshot.splitlines() if "Contact number" in l)
    assert phone.startswith("f") and "in-frame" in snapshot
    run(ctx, "my_type", ref=phone, text="+91 9083762965")
    assert "value='+91 9083762965'" in next(l for l in run(ctx, "my_snapshot").text.splitlines() if l.startswith(phone + " "))
    run(ctx, "my_click", ref=ref(ctx, "'Continue'"))
    text = run(ctx, "my_read").text
    assert "step 2: true" in text and "[embedded frame" in text  # A real, trusted click inside the frame.
    run(ctx, "my_type", ref=ref(ctx, "Nickname"), text="Ramiz")  # Inside shadow DOM.
    assert "value='Ramiz'" in next(l for l in run(ctx, "my_snapshot").text.splitlines() if "Nickname" in l)


def test_follows_the_tab_the_owner_switches_to(linked):
    ctx, site, page = linked["ctx"], linked["site"], linked["page"]
    page.bring_to_front()
    run(ctx, "my_open", url=site + "/index.html")
    other = linked["context"].new_page()
    other.goto(site + "/second.html")
    other.bring_to_front()
    time.sleep(0.3)
    assert "Second Page" in run(ctx, "my_read").text


def test_input_still_works_after_the_debugger_idles_out(linked):
    ctx, site, page = linked["ctx"], linked["site"], linked["page"]
    page.bring_to_front()
    run(ctx, "my_open", url=site + "/index.html")
    run(ctx, "my_type", ref=ref(ctx, "textarea"), text="first")
    time.sleep(21)  # The extension detaches its debugger after 20 idle seconds.
    run(ctx, "my_type", ref=ref(ctx, "textarea"), text="second")
    assert page.eval_on_selector("#notes", "e => e.value") == "second"
    assert run(ctx, "my_screenshot").images


OUTSIDE = ("Newsletter", "Log out", "Private balance", "Footer secret", "Account Centre", "register.html", "127.0.0.1")


def test_focus_mode_shows_only_the_named_form(linked):
    ctx, site, page = linked["ctx"], linked["site"], linked["page"]
    page.bring_to_front()
    run(ctx, "my_open", url=site + "/register.html")
    outside_ref = ref(ctx, "'Subscribe'")
    assert "Focus mode is on" in run(ctx, "my_focus", action="set", area="registration form").text
    snapshot, text = run(ctx, "my_snapshot").text, run(ctx, "my_read").text
    for seen in (snapshot, text):
        assert "FOCUS MODE" in seen and "(hidden" in seen
        assert not [word for word in OUTSIDE if word in seen], seen
    assert "Full name" in snapshot and "Next step" in snapshot and "registration step 1" in text
    # Work inside the form goes on as normal.
    run(ctx, "my_type", ref=ref(ctx, "Full name"), text="Ramiz")
    assert page.eval_on_selector("#fullname", "e => e.value") == "Ramiz"
    run(ctx, "my_click", ref=ref(ctx, "Next step"))
    assert "registered step 2" in run(ctx, "my_read").text
    assert run(ctx, "my_screenshot").images
    # Every way out of the form is closed.
    assert not execute_tool("my_click", {"ref": outside_ref}, ctx.tool).ok
    assert not execute_tool("my_tabs", {"action": "list"}, ctx.tool).ok
    assert not execute_tool("my_open", {"url": site + "/index.html"}, ctx.tool).ok
    assert not execute_tool("desktop_screenshot", {}, ctx.tool).ok
    assert not execute_tool("window_list", {}, ctx.tool).ok
    assert page.url.endswith("register.html")
    # Seeing the whole page again needs the owner's yes.
    ctx.approve = False
    assert not execute_tool("my_focus", {"action": "clear"}, ctx.tool).ok and ctx.prompts
    assert "Newsletter" not in run(ctx, "my_read").text
    ctx.approve = True
    run(ctx, "my_focus", action="clear")
    assert "Newsletter" in run(ctx, "my_read").text and "Account Centre" in run(ctx, "my_snapshot").text


def test_focus_mode_survives_a_reload_and_reports_a_missing_form(linked):
    ctx, site, page = linked["ctx"], linked["site"], linked["page"]
    page.bring_to_front()
    run(ctx, "my_open", url=site + "/register.html")
    run(ctx, "my_focus", action="set", area="registration form")
    page.reload()
    text = run(ctx, "my_read").text
    assert "registration step 1" in text and "Newsletter" not in text
    page.goto(site + "/second.html")  # The form is gone: nothing may be shown at all.
    gone = execute_tool("my_read", {}, ctx.tool)
    assert not gone.ok and "Second Page" not in gone.text and "second.html" not in gone.text
    run(ctx, "my_focus", action="clear")


def test_focus_mode_on_a_form_in_a_cross_origin_frame(linked):
    ctx, site, page = linked["ctx"], linked["site"], linked["page"]
    page.bring_to_front()
    run(ctx, "my_open", url=site + "/framed.html")
    for _ in range(50):
        if "Contact number" in run(ctx, "my_snapshot").text:
            break
        time.sleep(0.1)
    run(ctx, "my_focus", action="set", area="registration form")
    snapshot, text = run(ctx, "my_snapshot").text, run(ctx, "my_read").text
    assert "Contact number" in snapshot and "Your details" in text
    for seen in (snapshot, text):
        assert "training material" not in seen and "Framed Apply" not in seen and "localhost" not in seen and "framed.html" not in seen
    run(ctx, "my_type", ref=ref(ctx, "Contact number"), text="+91 9083762965")
    assert "value='+91 9083762965'" in run(ctx, "my_snapshot").text
    assert run(ctx, "my_screenshot").images
    run(ctx, "my_focus", action="clear")
