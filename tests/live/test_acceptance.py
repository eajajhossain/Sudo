"""The owner's acceptance list, on the real Chrome, mouse, keyboard and desktop.
Run with: python -m pytest tests/live -m live -s
Uses only a local test site and the Windows Calculator."""
import functools
import re
import socket
import subprocess
import threading
import time
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import psutil
import pytest

from conftest import Ctx
from sudo_agent.config import Settings
from sudo_agent.control import Stopped
from sudo_agent.tools import execute_tool
from sudo_agent.tools.browser import Browser
from sudo_agent.tools.desktop import find_window, foreground, from_screen

pytestmark = pytest.mark.live
SITE = Path(__file__).parent / "fixture_site"
TEXT = 'Multi-line ✓ Ünïcödé 日本語 🚀\nline two: "quotes" <tags> & $vars\n\ttabbed'


class Quiet(SimpleHTTPRequestHandler):
    def log_message(self, *a):
        pass


@pytest.fixture(scope="module")
def site():
    server = ThreadingHTTPServer(("127.0.0.1", 0), functools.partial(Quiet, directory=str(SITE)))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()


@pytest.fixture(scope="module")
def browser(tmp_path_factory):
    import os

    os.environ["SUDO_DATA_DIR"] = str(tmp_path_factory.mktemp("live"))
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    b = Browser(Settings(cdp_port=port))
    yield b
    b.shutdown(close_chrome=True)


@pytest.fixture
def c(memory, browser):
    return Ctx(memory, browser=browser)


def run(ctx, tool_name, /, **args):
    result = execute_tool(tool_name, args, ctx.tool)
    assert result.ok, result.text
    return result


def ref(ctx, pattern):
    text = run(ctx, "browser_snapshot").text
    for line in text.splitlines():
        if re.search(pattern, line):
            return line.split()[0]
    raise AssertionError(f"{pattern} not in:\n{text}")


def home(ctx, site):
    return run(ctx, "browser_open", url=site + "/index.html")


def field(ctx, selector):
    return ctx.tool.browser.run(lambda p: p.eval_on_selector(selector, "e => 'value' in e ? e.value : e.innerText")).replace("\r\n", "\n")


# 1 and 2 ----------------------------------------------------------------------
def test_01_02_launches_browser_and_opens_a_website(c, site):
    result = home(c, site)
    assert "SUDO Test Home" in result.text
    assert any(p.info["name"] == "chrome.exe" for p in psutil.process_iter(["name"]))


# 3 ----------------------------------------------------------------------------
def test_03_clicks_a_search_field(c, site):
    home(c, site)
    run(c, "browser_click", ref=ref(c, r"type=search"))
    assert c.tool.browser.run(lambda p: p.evaluate("() => document.activeElement.id")) == "q"


# 4 ----------------------------------------------------------------------------
def test_04_types_arbitrary_text_everywhere(c, site):
    home(c, site)
    run(c, "browser_type", ref=ref(c, r"textarea"), text=TEXT)
    assert field(c, "#notes") == TEXT
    run(c, "browser_type", ref=ref(c, r"'Rich editor'"), text="Rich ✓ 日本語")
    assert "Rich ✓ 日本語" in field(c, "#rich")
    run(c, "browser_type", ref=ref(c, r"type=search"), text="short query")
    assert field(c, "#q") == "short query"


def test_04b_types_into_the_focused_field(c, site):
    home(c, site)
    run(c, "browser_click", ref=ref(c, r"'Full name'"))
    run(c, "browser_type_focused", text="Ramiz 🙂")
    assert field(c, "[name=fullname]") == "Ramiz 🙂"


# 5 ----------------------------------------------------------------------------
def test_05_edits_existing_text(c, site):
    home(c, site)
    pre = ref(c, r"'Prefilled'")
    run(c, "browser_edit_text", ref=pre, find="quick", replace="slow")
    assert field(c, "#pre") == "The slow brown fox"
    run(c, "browser_type", ref=pre, text=" jumps", clear=False)
    assert field(c, "#pre") == "The slow brown fox jumps"
    run(c, "browser_type", ref=pre, text="replaced")
    assert field(c, "#pre") == "replaced"


# 6 ----------------------------------------------------------------------------
def test_06_navigates_to_another_page_and_back(c, site):
    home(c, site)
    result = run(c, "browser_click", ref=ref(c, r"Go to second"))
    assert "Second Page" in result.text and "Navigated" in result.text
    assert "SUDO Test Home" in run(c, "browser_nav", action="back").text


# 7 ----------------------------------------------------------------------------
def test_07_presses_browser_buttons_dropdowns_and_tabs(c, site):
    home(c, site)
    add = ref(c, r"Add one")
    run(c, "browser_click", ref=add)
    run(c, "browser_click", ref=add)
    assert field(c, "#count") == "2"
    run(c, "browser_select", ref=ref(c, r"select"), label="Green")
    assert "*" in run(c, "browser_tabs", action="new", url=site + "/second.html").text
    tabs = [l for l in run(c, "browser_tabs", action="list").text.splitlines() if re.match(r"[* ] \d+:", l)]
    assert len(tabs) >= 2
    run(c, "browser_tabs", action="close", index=len(tabs) - 1)
    run(c, "browser_scroll", to="bottom")
    assert run(c, "browser_screenshot").images


# 8 ----------------------------------------------------------------------------
def test_08_uses_keyboard_shortcuts(c, site):
    home(c, site)
    run(c, "browser_click", ref=ref(c, r"Add one"))
    run(c, "browser_press", key="Control+k")
    assert field(c, "#kbd") == "shortcut worked"


def test_08b_search_submits_with_enter_without_asking(c, site):
    home(c, site)
    run(c, "browser_type", ref=ref(c, r"type=search"), text="hello")
    assert "Search Results" in run(c, "browser_press", key="Enter").text
    assert not c.prompts


# Prepare versus finalize ------------------------------------------------------
def test_final_submit_stops_for_approval_and_can_be_declined(memory, browser, site):
    ctx = Ctx(memory, browser=browser, approve=False)
    home(ctx, site)
    run(ctx, "browser_type", ref=ref(ctx, r"'Full name'"), text="Prepared, not sent")
    assert not ctx.prompts, "typing is preparation and must not ask"
    result = execute_tool("browser_click", {"ref": ref(ctx, r"Send message")}, ctx.tool)
    assert not result.ok and "declined" in result.text and ctx.prompts
    assert "SUDO Test Home" in run(ctx, "browser_read").text
    assert field(ctx, "[name=fullname]") == "Prepared, not sent"


def test_enter_in_a_real_form_needs_approval(memory, browser, site):
    ctx = Ctx(memory, browser=browser, approve=False)
    home(ctx, site)
    run(ctx, "browser_click", ref=ref(ctx, r"'Full name'"))
    result = execute_tool("browser_press", {"key": "Enter"}, ctx.tool)
    assert not result.ok and ctx.prompts


def test_password_field_refuses_plain_typing_and_masked_box_works(memory, browser, site):
    secret = "correct-horse-battery"
    ctx = Ctx(memory, browser=browser)
    home(ctx, site)
    password = ref(ctx, r"type=password")
    refused = execute_tool("browser_type", {"ref": password, "text": "plain"}, ctx.tool)
    assert not refused.ok and "browser_type_secret" in refused.text
    ctx.approve = secret
    result = execute_tool("browser_type_secret", {"ref": password, "label": "Test password"}, ctx.tool)
    assert result.ok and secret not in result.text
    assert field(ctx, "[name=secret]") == secret
    assert secret not in run(ctx, "browser_snapshot").text


def test_page_text_is_labelled_untrusted(c, site):
    home(c, site)
    text = run(c, "browser_read").text
    assert text.startswith('<untrusted_content source="browser_read">') and "IGNORE ALL" in text


def test_detects_a_failed_navigation(c, site):
    missing = execute_tool("browser_open", {"url": site + "/nope.html"}, c.tool)
    assert not missing.ok and "404" in missing.text
    dead = execute_tool("browser_open", {"url": "http://127.0.0.1:9/"}, c.tool)
    assert not dead.ok


def test_reconnects_after_chrome_connection_is_lost(c, browser, site):
    home(c, site)
    browser.call(browser._reset)
    assert "SUDO Test Home" in home(c, site).text


# 9: real desktop app ----------------------------------------------------------
def test_09_operates_a_desktop_application(memory, browser):
    ctx = Ctx(memory, browser=browser)
    run(ctx, "app_open", name="calc")
    time.sleep(1.5)
    try:
        run(ctx, "window_focus", title="Calculator")
        run(ctx, "desktop_key", keys="escape")
        for name in ("Seven", "Plus", "Five", "Equals"):
            run(ctx, "ui_click", name=name, control_type="Button", title="Calculator")
        time.sleep(0.4)
        tree = run(ctx, "ui_elements", title="Calculator").text
        assert "Display is 12" in tree, tree
        assert run(ctx, "desktop_screenshot").images
    finally:
        run(ctx, "window_close", title="Calculator")
    assert find_window("Calculator") is None


def test_09b_real_mouse_and_keyboard_type_into_chrome(c, browser, site):
    """Desktop-level fallback: real pointer and key events, no DOM at all."""
    home(c, site)
    browser.call(lambda: browser.page.bring_to_front())
    title = browser.run(lambda p: p.title())
    run(c, "window_focus", title=title)
    box = browser.run(lambda p: p.evaluate(
        "() => { const r = document.getElementById('native').getBoundingClientRect();"
        " return [window.screenX + (window.outerWidth - innerWidth) / 2 + r.left + r.width / 2,"
        " window.screenY + (window.outerHeight - innerHeight) + r.top + r.height / 2, devicePixelRatio] }"))
    run(c, "desktop_screenshot")
    ratio = box[2]
    x, y = from_screen(box[0] * ratio, box[1] * ratio)
    run(c, "desktop_click", x=x, y=y)
    time.sleep(0.2)
    run(c, "desktop_type", text="Desktop ✓ 日本語 🚀")
    time.sleep(0.2)
    assert field(c, "#native") == "Desktop ✓ 日本語 🚀"
    run(c, "desktop_key", keys="ctrl+a")
    run(c, "desktop_type", text="pasted text", mode="paste")
    time.sleep(0.2)
    assert field(c, "#native") == "pasted text"
    assert foreground()["process"] == "chrome.exe"


def _page_point(c, browser, selector):
    box = browser.run(lambda p: p.evaluate(
        "s => { const r = document.querySelector(s).getBoundingClientRect();"
        " return [window.screenX + (window.outerWidth - innerWidth) / 2 + r.left, window.screenY + (window.outerHeight - innerHeight) + r.top, r.width, r.height, devicePixelRatio] }", selector))
    return box


def test_09c_real_mouse_drags_and_scrolls(c, browser, site):
    home(c, site)
    browser.call(lambda: browser.page.bring_to_front())
    run(c, "window_focus", title=browser.run(lambda p: p.title()))
    left, top, width, height, ratio = _page_point(c, browser, "#drag")
    run(c, "desktop_screenshot")
    y = top + height / 2
    x1, py = from_screen((left + 40) * ratio, y * ratio)
    x2, _ = from_screen((left + 240) * ratio, y * ratio)
    run(c, "desktop_drag", x1=x1, y1=py, x2=x2, y2=py)
    time.sleep(0.2)
    moved = browser.run(lambda p: p.eval_on_selector("#drag", "e => +e.dataset.moved"))
    assert 170 <= moved <= 230, moved
    run(c, "desktop_scroll", clicks=-5, x=x1, y=py)
    time.sleep(0.5)
    assert browser.run(lambda p: p.evaluate("() => scrollY")) > 0


def test_browser_upload_and_download(c, site, tmp_path):
    home(c, site)
    source = tmp_path / "attach-me.txt"
    source.write_text("hello")
    run(c, "browser_upload", ref=ref(c, r"type=file"), path=str(source))
    assert c.prompts and c.prompts[0]["kind"] == "approval"
    assert c.tool.browser.run(lambda p: p.eval_on_selector("#attach", "e => e.files[0].name")) == "attach-me.txt"
    folder = tmp_path / "saved"
    run(c, "browser_download", ref=ref(c, r"Save copy"), folder=str(folder))
    assert "second page" in (folder / "got.html").read_text()


def test_upload_of_a_credentials_file_is_refused(c, site, tmp_path):
    home(c, site)
    secret = tmp_path / "server.pem"
    secret.write_text("x")
    result = execute_tool("browser_upload", {"ref": ref(c, r"type=file"), "path": str(secret)}, c.tool)
    assert not result.ok and "Blocked" in result.text and not c.prompts


def test_clipboard_round_trip(c):
    run(c, "clipboard_write", text="round trip ✓")
    assert "round trip ✓" in run(c, "clipboard_read").text


def test_opens_a_visible_terminal(c, tmp_path):
    def shells():
        return {p.pid for p in psutil.process_iter(["name"]) if p.info["name"] == "powershell.exe"}

    before = shells()
    run(c, "terminal_open", cwd=str(tmp_path))
    time.sleep(1.5)
    opened = shells() - before
    try:
        assert opened
        assert any("-NoExit" in psutil.Process(pid).cmdline() for pid in opened)
    finally:
        for pid in opened:
            psutil.Process(pid).kill()


def test_internet_research_and_network_tools(c):
    results = run(c, "web_search", query="Python programming language official site")
    assert "python.org" in results.text.lower()
    page = run(c, "web_fetch", url="https://www.python.org/")
    assert "python" in page.text.lower() and page.text.startswith("<untrusted_content")
    network = run(c, "network_diagnose", host="www.python.org")
    assert "DNS:" in network.text and "HTTPS: 200" in network.text


def test_browser_selects_text(c, site):
    home(c, site)
    assert "quick brown" in run(c, "browser_select_text", ref=ref(c, r"'Prefilled'"), find="quick brown").text
    assert c.tool.browser.run(lambda p: p.eval_on_selector("#pre", "e => e.value.slice(e.selectionStart, e.selectionEnd)")) == "quick brown"
    assert "Test home" in run(c, "browser_select_text", find="Test home").text


def test_desktop_selects_text_and_restores_clipboard(memory, browser):
    ctx = Ctx(memory, browser=browser)
    proc = subprocess.Popen(["powershell", "-NoProfile", "-Command",
        "Add-Type -AssemblyName System.Windows.Forms; $f=New-Object Windows.Forms.Form; $f.Text='SUDOSELECT';"
        "$t=New-Object Windows.Forms.TextBox; $t.Multiline=$true; $t.Dock='Fill'; $t.Text='alpha beta'; $f.Controls.Add($t);"
        "$f.Add_Shown({$f.Activate();$t.Focus()}); [void]$f.ShowDialog()"])
    try:
        deadline = time.monotonic() + 15
        while find_window("SUDOSELECT") is None and time.monotonic() < deadline:
            time.sleep(0.2)
        time.sleep(0.5)
        run(ctx, "window_focus", title="SUDOSELECT")
        run(ctx, "clipboard_write", text="keep me")
        assert "alpha beta" in run(ctx, "desktop_select_text", mode="all").text
        assert "alpha beta" in run(ctx, "desktop_select_text", mode="line").text
        run(ctx, "desktop_key", keys="end")
        run(ctx, "desktop_key", keys="shift+left")
        run(ctx, "desktop_key", keys="ctrl+c")
        time.sleep(0.2)
        assert "a" == run(ctx, "clipboard_read").text.split("\n")[1]
        run(ctx, "clipboard_write", text="keep me")
        run(ctx, "desktop_select_text", mode="all")
        assert "keep me" in run(ctx, "clipboard_read").text
    finally:
        psutil.Process(proc.pid).kill()


def test_file_explorer(memory, browser, tmp_path):
    ctx = Ctx(memory, browser=browser)
    (tmp_path / "one.txt").write_text("1")
    (tmp_path / "two.txt").write_text("2")
    run(ctx, "explorer_open", path=str(tmp_path))
    try:
        view = run(ctx, "explorer_view").text
        assert "one.txt" in view and "two.txt" in view
        assert "one.txt, two.txt" in run(ctx, "explorer_select", folder=str(tmp_path), names=["one.txt", "two.txt"]).text
        assert "Selected: two.txt" in run(ctx, "explorer_select", folder=str(tmp_path), names=["two.txt"]).text
    finally:
        run(ctx, "explorer_close", folder=str(tmp_path))
    assert str(tmp_path) not in run(ctx, "explorer_view").text


def test_explorer_opens_a_file_selected(memory, browser, tmp_path):
    ctx = Ctx(memory, browser=browser)
    target = tmp_path / "pick.txt"
    target.write_text("1")
    run(ctx, "explorer_open", path=str(target))
    try:
        time.sleep(1.0)
        assert "selected: pick.txt" in run(ctx, "explorer_view").text
    finally:
        run(ctx, "explorer_close", folder=str(tmp_path))


def test_app_launch_falls_back_to_the_start_menu(memory, browser):
    ctx = Ctx(memory, browser=browser)
    run(ctx, "app_open", name="Calculator")
    time.sleep(1.0)
    try:
        assert find_window("Calculator") is not None
    finally:
        run(ctx, "window_close", title="Calculator")


@pytest.mark.parametrize("kind", ["edge", "brave"])
def test_other_browsers_work_fully(memory, site, kind):
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    other = Browser(Settings(cdp_port=port, browser="chrome"))
    ctx = Ctx(memory, browser=other)
    try:
        run(ctx, "browser_switch", browser=kind)
        assert "SUDO Test Home" in home(ctx, site).text
        run(ctx, "browser_type", ref=ref(ctx, r"textarea"), text=f"typed in {kind} ✓")
        assert field(ctx, "#notes") == f"typed in {kind} ✓"
        executable = {"edge": "msedge.exe", "brave": "brave.exe"}[kind]
        assert psutil.Process(other._launched[kind].pid).name() == executable
    finally:
        other.shutdown(close_chrome=True)


# 10 and 11 --------------------------------------------------------------------
def test_10_11_runs_a_command_and_inspects_an_error(memory, browser):
    ctx = Ctx(memory, browser=browser)
    ok = run(ctx, "run_command", command="$PSVersionTable.PSVersion.Major")
    assert "5" in ok.text
    failed = execute_tool("run_command", {"command": "python -c \"import not_a_real_module\""}, ctx.tool)
    assert not failed.ok and "ModuleNotFoundError" in failed.text
    recovered = run(ctx, "run_command", command="python -c \"print('recovered')\"")
    assert "recovered" in recovered.text


# 12 ---------------------------------------------------------------------------
def test_12_stop_halts_typing_midway(memory, browser):
    ctx = Ctx(memory, browser=browser)
    proc = subprocess.Popen(["powershell", "-NoProfile", "-Command",
        "Add-Type -AssemblyName System.Windows.Forms; $f=New-Object Windows.Forms.Form; $f.Text='SUDOTYPETARGET';"
        "$t=New-Object Windows.Forms.TextBox; $t.Multiline=$true; $t.MaxLength=0; $t.Dock='Fill'; $f.Controls.Add($t);"
        "$f.Add_Shown({$f.Activate();$t.Focus()}); [void]$f.ShowDialog()"])
    try:
        deadline = time.monotonic() + 15
        while find_window("SUDOTYPETARGET") is None and time.monotonic() < deadline:
            time.sleep(0.2)
        time.sleep(0.5)
        run(ctx, "window_focus", title="SUDOTYPETARGET")
        caught = []

        def worker():
            try:
                execute_tool("desktop_type", {"text": "x" * 40000}, ctx.tool)
            except Stopped as exc:
                caught.append(exc)

        thread = threading.Thread(target=worker)
        thread.start()
        time.sleep(0.25)
        started = time.monotonic()
        ctx.control.stop()
        thread.join(4)
        assert caught and not thread.is_alive() and time.monotonic() - started < 2
        with pytest.raises(Stopped):
            execute_tool("desktop_key", {"keys": "a"}, ctx.tool)
    finally:
        psutil.Process(proc.pid).kill()


def test_stop_hotkey_from_anywhere(harness):
    import pyautogui

    from sudo_agent.agent import start_stop_hotkey

    h = harness([[("run_command", {"command": "Start-Sleep 30", "timeout": 60})]])
    start_stop_hotkey(h.manager, h.events)
    time.sleep(0.5)
    task = h.manager.submit("x")
    h.wait_event("tool_start", tool="run_command")
    time.sleep(0.5)
    pyautogui.hotkey("ctrl", "alt", "q")
    assert h.wait(task, timeout=5)["status"] == "stopped"


def test_voice_recognition_on_generated_speech(tmp_path):
    from sudo_agent.server import VOICE_SCRIPT

    wav = tmp_path / "s.wav"
    subprocess.run(["powershell", "-NoProfile", "-Command",
        "Add-Type -AssemblyName System.Speech; $s=New-Object System.Speech.Synthesis.SpeechSynthesizer;"
        f"$s.SetOutputToWaveFile('{wav}'); $s.Speak('open the browser please'); $s.Dispose()"], check=True, timeout=30)
    done = subprocess.run(["powershell", "-NoProfile", "-File", str(VOICE_SCRIPT), "-WavPath", str(wav)], capture_output=True, timeout=40)
    if done.returncode == 3:
        pytest.skip("Windows has no speech recognizer for this language")
    assert done.returncode == 0, done.stderr.decode("utf-8", "replace")
    assert "browser" in done.stdout.decode("utf-8", "replace").lower()


# End to end: the whole supervisor drives the real browser -----------------------
def test_full_loop_drives_the_real_browser(memory, browser, site):
    from sudo_agent.agent import TaskManager
    from sudo_agent.events import EventBus
    from sudo_agent.llm import ScriptedProvider

    def type_step(messages):
        last = messages[-1]["content"][0]["content"][0]["text"]
        target = next(l.split()[0] for l in last.splitlines() if "type=search" in l)
        return [("browser_type", {"ref": target, "text": "end to end ✓"})]

    def field_check(messages):
        snapshot = messages[-3]["content"][0]["content"][0]["text"]
        target = next(l.split()[0] for l in snapshot.splitlines() if "type=search" in l)
        return [("task_finish", {"status": "done", "summary": "Typed it.", "checks": [
            {"type": "page_url_contains", "text": "/index.html"},
            {"type": "field_value_equals", "ref": target, "text": "end to end ✓"}]})]

    provider = ScriptedProvider([
        [("browser_open", {"url": site + "/index.html"})],
        [("browser_snapshot", {})],
        type_step,
        field_check,
    ])
    manager = TaskManager(Settings(), memory, EventBus(), browser, lambda s: provider)
    try:
        task = manager.submit("Type into the search box")
        deadline = time.monotonic() + 40
        while memory.get_task(task)["status"] not in ("done", "failed", "stopped") and time.monotonic() < deadline:
            time.sleep(0.05)
        row = memory.get_task(task)
        assert (row["status"], row["verdict"]) == ("done", "verified"), row
    finally:
        manager.close()
