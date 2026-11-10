"""Browser control: real Chrome on a dedicated profile, driven through CDP and Playwright.

Chrome stays open across SUDO restarts, so logins the owner performs there persist,
and the same window can also be controlled by the desktop tools when the DOM falls short.
"""
from __future__ import annotations

import base64
import os
import re
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.request import urlopen

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import sync_playwright

from .. import policy
from ..config import data_dir
from .base import ToolContext, ToolResult, tool

_SNAPSHOT_JS = r"""
() => {
  const sel = 'a[href],button,input,select,textarea,summary,[role=button],[role=link],[role=textbox],' +
    '[role=combobox],[role=checkbox],[role=radio],[role=tab],[role=menuitem],[role=option],[role=searchbox],' +
    '[contenteditable=""],[contenteditable=true],[onclick]';
  window.__sudoN = window.__sudoN || 0;
  const out = [];
  for (const el of document.querySelectorAll(sel)) {
    const r = el.getBoundingClientRect();
    const s = getComputedStyle(el);
    if (r.width <= 0 || r.height <= 0 || s.visibility === 'hidden' || s.display === 'none') continue;
    if (!el.dataset.sudoRef) el.dataset.sudoRef = 'e' + (++window.__sudoN);
    const labels = el.labels ? [...el.labels].map(l => l.innerText).join(' ') : '';
    const name = (el.getAttribute('aria-label') || labels || el.innerText || el.getAttribute('title') ||
      el.getAttribute('alt') || el.getAttribute('name') || '').trim().replace(/\s+/g, ' ').slice(0, 90);
    const type = el.getAttribute('type') || '';
    const sensitive = type === 'password' || /^cc-|one-time-code/.test(el.getAttribute('autocomplete') || '');
    const editable = el.isContentEditable;
    const value = sensitive ? '' : ('value' in el ? String(el.value) : (editable ? el.innerText : ''));
    out.push({
      ref: el.dataset.sudoRef, tag: el.tagName.toLowerCase(), type, role: el.getAttribute('role') || '',
      name, value: value.slice(0, 120), placeholder: el.getAttribute('placeholder') || '',
      disabled: !!el.disabled, checked: el.checked === true, sensitive, editable,
      inView: r.bottom > 0 && r.top < innerHeight,
    });
  }
  return out;
}
"""

_DESCRIBE_JS = r"""
el => {
  const form = el.form || el.closest('form');
  const type = (el.getAttribute('type') || '').toLowerCase();
  const fields = form ? [...form.querySelectorAll('input,textarea')] : [];
  const search = !!form && (form.getAttribute('role') === 'search' ||
    /search/i.test(form.getAttribute('action') || '') ||
    fields.some(f => f.type === 'search' || /^(q|query|search|s)$/i.test(f.name || '')));
  return {
    name: (el.getAttribute('aria-label') || el.innerText || el.value || el.getAttribute('title') || '').trim().slice(0, 120),
    tag: el.tagName.toLowerCase(), type, editable: el.isContentEditable, inForm: !!form, searchForm: search,
    submits: !!form && (type === 'submit' || (el.tagName === 'BUTTON' && !type)),
    sensitive: type === 'password' || /^cc-|one-time-code/.test(el.getAttribute('autocomplete') || ''),
  };
}
"""

_FOCUS_JS = r"""
() => {
  let el = document.activeElement;
  while (el && el.shadowRoot && el.shadowRoot.activeElement) el = el.shadowRoot.activeElement;
  if (!el || el === document.body) return null;
  const form = el.form || el.closest('form');
  const fields = form ? [...form.querySelectorAll('input,textarea')] : [];
  const search = !!form && (form.getAttribute('role') === 'search' ||
    /search/i.test(form.getAttribute('action') || '') ||
    fields.some(f => f.type === 'search' || /^(q|query|search|s)$/i.test(f.name || '')));
  return { tag: el.tagName.toLowerCase(), type: (el.getAttribute('type') || '').toLowerCase(),
    editable: el.isContentEditable, inForm: !!form, searchForm: search,
    sensitive: el.type === 'password' || /^cc-|one-time-code/.test(el.getAttribute('autocomplete') || ''),
    name: el.getAttribute('aria-label') || el.getAttribute('name') || '' };
}
"""

_SELECT_JS = r"""
(el, needle) => {
  if ('value' in el && el.setSelectionRange) {
    const i = needle === null ? 0 : el.value.indexOf(needle);
    if (i < 0) return null;
    const end = needle === null ? el.value.length : i + needle.length;
    el.focus();
    el.setSelectionRange(i, end);
    return el.value.slice(i, end);
  }
  const s = getSelection();
  if (needle === null) {
    const r = document.createRange();
    r.selectNodeContents(el);
    s.removeAllRanges(); s.addRange(r);
    return s.toString();
  }
  const walker = document.createTreeWalker(el, NodeFilter.SHOW_TEXT);
  while (walker.nextNode()) {
    const n = walker.currentNode, i = n.data.indexOf(needle);
    if (i >= 0) {
      const r = document.createRange();
      r.setStart(n, i); r.setEnd(n, i + needle.length);
      s.removeAllRanges(); s.addRange(r);
      return s.toString();
    }
  }
  return null;
}
"""

_CAPTCHA_RE = re.compile(r"(?i)recaptcha|hcaptcha|turnstile|cf-chl|captcha")

BROWSER_PATHS = {
    "chrome": [r"%ProgramFiles%\Google\Chrome\Application\chrome.exe",
               r"%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe",
               r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"],
    "edge": [r"%ProgramFiles(x86)%\Microsoft\Edge\Application\msedge.exe",
             r"%ProgramFiles%\Microsoft\Edge\Application\msedge.exe"],
    "brave": [r"%ProgramFiles%\BraveSoftware\Brave-Browser\Application\brave.exe",
              r"%ProgramFiles(x86)%\BraveSoftware\Brave-Browser\Application\brave.exe",
              r"%LOCALAPPDATA%\BraveSoftware\Brave-Browser\Application\brave.exe"],
}


def find_browser(kind: str) -> str | None:
    for candidate in BROWSER_PATHS[kind]:
        path = os.path.expandvars(candidate)
        if os.path.isfile(path):
            return path
    return None


def _normal(text: str) -> str:
    return (text or "").replace("\r\n", "\n").replace("\r", "\n").replace("\xa0", " ")


class Browser:
    """All Playwright calls run on one dedicated thread, as its sync API requires."""

    def __init__(self, settings):
        self.settings = settings
        self._pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="browser")
        self._pw = None
        self._browser = None
        self._context = None
        self.page = None
        self.kind = settings.browser
        self._launched: dict[str, subprocess.Popen] = {}

    def call(self, fn, *args, timeout: float = 60.0):
        return self._pool.submit(fn, *args).result(timeout=timeout)

    def switch(self, kind: str) -> None:
        """Use a different browser from the next action on. Each keeps its own profile."""
        if kind not in BROWSER_PATHS:
            raise ValueError(f"Unknown browser: {kind}")

        def job():
            if kind != self.kind:
                self._reset()
                self.kind = kind

        self.call(job)

    # -- connection ------------------------------------------------------------
    @property
    def _port(self) -> int:
        return self.settings.cdp_port + list(BROWSER_PATHS).index(self.kind)

    @property
    def _endpoint(self) -> str:
        return f"http://127.0.0.1:{self._port}"

    def _cdp_up(self) -> bool:
        try:
            with urlopen(self._endpoint + "/json/version", timeout=1) as resp:
                return resp.status == 200
        except OSError:
            return False

    def _launch(self) -> None:
        path = find_browser(self.kind)
        if path is None:
            raise RuntimeError(f"{self.kind} is not installed on this PC.")
        profile = data_dir() / "browser-profiles" / self.kind
        self._launched[self.kind] = subprocess.Popen(
            [path, f"--remote-debugging-port={self._port}", f"--user-data-dir={profile}",
             "--no-first-run", "--no-default-browser-check", "about:blank"]
        )
        deadline = time.monotonic() + 25
        while time.monotonic() < deadline:
            if self._cdp_up():
                return
            time.sleep(0.2)
        raise RuntimeError(f"{self.kind} started but its control port never opened.")

    def _connected(self) -> bool:
        try:
            return bool(self._browser and self._browser.is_connected())
        except PlaywrightError:
            return False

    def ensure(self):
        if self._connected() and self.page is not None and not self.page.is_closed():
            return self.page
        if not self._connected():
            self._reset()
            if not self._cdp_up():
                self._launch()
            if self._pw is None:
                self._pw = sync_playwright().start()
            self._browser = self._pw.chromium.connect_over_cdp(self._endpoint, timeout=15000)
            self._context = self._browser.contexts[0]
            self._context.set_default_timeout(10000)
        pages = self._context.pages
        self.page = pages[-1] if pages else self._context.new_page()
        return self.page

    def _reset(self) -> None:
        try:
            if self._browser:
                self._browser.close()
        except Exception:
            pass
        self._browser = self._context = self.page = None

    def run(self, fn, *args, retry_on_disconnect: bool = False):
        """Run fn(page, ...) on the browser thread, reconnecting if Chrome went away."""

        def job():
            page = self.ensure()
            try:
                return fn(page, *args)
            except PlaywrightError as exc:
                closed = re.search(r"(?i)closed|disconnected|connection", str(exc))
                if not (closed and retry_on_disconnect):
                    raise
                self._reset()
                return fn(self.ensure(), *args)

        return self.call(job)

    def shutdown(self, close_chrome: bool = False) -> None:
        def job():
            self._reset()
            if self._pw:
                self._pw.stop()
                self._pw = None

        try:
            self.call(job, timeout=10)
        except Exception:
            pass
        if close_chrome:
            from ..control import kill_process_tree

            for process in self._launched.values():
                if process.poll() is None:
                    kill_process_tree(process.pid)

    # -- element helpers -------------------------------------------------------
    def locate(self, page, ref: str):
        frame = page.main_frame
        if ":" in ref:
            prefix, ref = ref.split(":", 1)
            frame = page.frames[int(prefix[1:])]
        locator = frame.locator(f'[data-sudo-ref="{ref}"]')
        if locator.count() == 0:
            raise LookupError(f"Element {ref} is gone. Call browser_snapshot for fresh refs.")
        return locator.first

    def describe(self, ref: str) -> dict:
        return self.run(lambda page: self.locate(page, ref).evaluate(_DESCRIBE_JS))

    def focused(self) -> dict | None:
        return self.run(lambda page: page.evaluate(_FOCUS_JS))


# ---- Risk checks -------------------------------------------------------------

def _risk_click(args: dict, ctx: ToolContext) -> policy.Decision:
    info = ctx.browser.describe(args["ref"])
    if policy.is_finalize_label(info["name"]):
        return policy.confirm(f'This clicks "{info["name"]}", which finalizes something.')
    if info["submits"] and not info["searchForm"]:
        return policy.confirm(f'This submits a form with the button "{info["name"]}".')
    return policy.OK


def _risk_press(args: dict, ctx: ToolContext) -> policy.Decision:
    key = args["key"].lower()
    if "enter" not in key:
        return policy.OK
    info = ctx.browser.focused()
    if info is None:
        return policy.OK
    if info["editable"]:
        return policy.confirm("Pressing Enter in this editor may send the message.")
    if info["tag"] == "textarea" and key != "enter":
        return policy.confirm("This shortcut may send the text.")
    if info["tag"] == "input" and info["inForm"] and not info["searchForm"]:
        return policy.confirm("Pressing Enter here submits the form.")
    return policy.OK


def _risk_text(args: dict, ctx: ToolContext) -> policy.Decision:
    text = args.get("text") or args.get("replace") or ""
    if policy.contains_secret(text):
        return policy.confirm("The text looks like it contains a password, key or card number.")
    return policy.OK


def _risk_upload(args: dict, ctx: ToolContext) -> policy.Decision:
    verdict = policy.check_read(args["path"])
    if verdict.level == policy.BLOCK:
        return verdict
    return policy.confirm(f"This uploads {args['path']} to the website.")


# ---- Tools -------------------------------------------------------------------

def _page_line(page) -> str:
    return f"Tab: {page.title()!r} at {page.url}"


def _act_and_settle(page, action) -> None:
    """Run an input action, then wait for any page load it started.
    A load begins a moment after the input event, so checking at once would see the old page."""
    navigations = []

    def on_request(request):
        if request.is_navigation_request() and request.frame == page.main_frame:
            navigations.append(request)

    before = page.url
    page.on("request", on_request)
    try:
        action()
        page.wait_for_timeout(150)
    finally:
        page.remove_listener("request", on_request)
    if not navigations:
        return
    deadline = time.monotonic() + 4
    while page.url == before and time.monotonic() < deadline:
        page.wait_for_timeout(50)
    try:
        page.wait_for_load_state("domcontentloaded", timeout=8000)
    except PlaywrightError:
        pass


@tool("browser_open", "Go to a URL in the active tab, launching Chrome if needed.",
      {"url": {"type": "string"}}, ["url"], group="browser", retries=1, timeout=45, untrusted=True)
def browser_open(args, ctx):
    url = args["url"].strip()
    if not re.match(r"(?i)^(https?://|about:)", url):
        if re.match(r"(?i)^[a-z][a-z0-9+.\-]*:", url):
            return ToolResult("Only http, https and about addresses are allowed.", ok=False)
        url = "https://" + url

    def job(page):
        response = page.goto(url, wait_until="domcontentloaded", timeout=30000)
        return (response.status if response else 0), _page_line(page)

    status, line = ctx.browser.run(job, retry_on_disconnect=True)
    if status >= 400:
        return ToolResult(f"The site answered HTTP {status}. {line}", ok=False)
    return ToolResult(f"Loaded (HTTP {status}). {line}")


@tool("browser_snapshot", "List the page's interactive elements with refs for the other browser tools, plus focus and CAPTCHA detection. Call again after the page changes.",
      group="browser", phase="OBSERVE", retries=1, timeout=30, untrusted=True)
def browser_snapshot(args, ctx):
    def job(page):
        items = []
        for index, frame in enumerate(page.frames[:8]):
            try:
                found = frame.evaluate(_SNAPSHOT_JS)
            except PlaywrightError:
                continue
            for item in found:
                if index:
                    item["ref"] = f"f{index}:{item['ref']}"
                items.append(item)
        html = " ".join(f.url for f in page.frames)
        return items, _page_line(page), page.evaluate(_FOCUS_JS), bool(_CAPTCHA_RE.search(html))

    items, line, focus, captcha = ctx.browser.run(job, retry_on_disconnect=True)
    items.sort(key=lambda i: not i["inView"])
    lines = [line]
    if captcha:
        lines.append("CAPTCHA DETECTED: do not attempt it. Use ask_owner so the owner can solve it.")
    if focus:
        lines.append(f"Focused: {focus['tag']} {focus['name']!r}")
    for item in items[:150]:
        bits = [item["ref"], item["tag"]]
        if item["type"]:
            bits.append(f"type={item['type']}")
        if item["role"]:
            bits.append(f"role={item['role']}")
        if item["name"]:
            bits.append(repr(item["name"]))
        if item["placeholder"]:
            bits.append(f"placeholder={item['placeholder']!r}")
        if item["value"]:
            bits.append(f"value={item['value']!r}")
        if item["sensitive"]:
            bits.append("SENSITIVE(use browser_type_secret)")
        if item["disabled"]:
            bits.append("disabled")
        if item["checked"]:
            bits.append("checked")
        if not item["inView"]:
            bits.append("below-fold")
        lines.append(" ".join(bits))
    if len(items) > 150:
        lines.append(f"... and {len(items) - 150} more; scroll or read the page text.")
    return ToolResult("\n".join(lines))


@tool("browser_read", "Read the visible text of the page.", {"max_chars": {"type": "integer"}},
      group="browser", phase="OBSERVE", retries=1, timeout=20, untrusted=True)
def browser_read(args, ctx):
    limit = min(args.get("max_chars", 12000), 60000)

    def job(page):
        return _page_line(page), page.evaluate("() => document.body ? document.body.innerText : ''")

    line, text = ctx.browser.run(job, retry_on_disconnect=True)
    suffix = f"\n[truncated: page has {len(text)} characters]" if len(text) > limit else ""
    return ToolResult(f"{line}\n{text[:limit]}{suffix}")


@tool("browser_screenshot", "See the current browser tab as an image.", group="browser", phase="OBSERVE", retries=1, timeout=20, untrusted=True)
def browser_screenshot(args, ctx):
    def job(page):
        return page.screenshot(type="jpeg", quality=60), _page_line(page)

    data, line = ctx.browser.run(job, retry_on_disconnect=True)
    return ToolResult(line, images=[("image/jpeg", base64.b64encode(data).decode("ascii"))])


@tool("browser_click", "Click an element by ref.", {"ref": {"type": "string"}}, ["ref"], group="browser", risk=_risk_click, timeout=30, untrusted=True)
def browser_click(args, ctx):
    def job(page):
        before = page.url
        _act_and_settle(page, lambda: ctx.browser.locate(page, args["ref"]).click(timeout=8000))
        return before, page.url, _page_line(page)

    before, after, line = ctx.browser.run(job)
    return ToolResult(f"Clicked. {'Navigated. ' if before != after else ''}{line}")


def _read_field(locator) -> str:
    return locator.evaluate("el => 'value' in el ? String(el.value) : el.innerText")


@tool("browser_type", "Type any text into a field by ref (input, textarea or rich editor). Clears it first unless clear is false, in which case the text goes at the end. Reads the field back to confirm. Never submits.",
      {"ref": {"type": "string"}, "text": {"type": "string"}, "clear": {"type": "boolean"}},
      ["ref", "text"], group="browser", risk=_risk_text, timeout=60)
def browser_type(args, ctx):
    text, clear = args["text"], args.get("clear", True)

    def job(page):
        locator = ctx.browser.locate(page, args["ref"])
        info = locator.evaluate(_DESCRIBE_JS)
        if info["sensitive"]:
            return None, "This is a password or payment field. Use browser_type_secret."
        locator.click(timeout=8000)
        before = _normal(_read_field(locator))
        modifier = "Control"
        if clear:
            page.keyboard.press(f"{modifier}+a")
            page.keyboard.press("Delete")
        else:
            locator.evaluate(
                "el => { if (el.setSelectionRange) { try { el.setSelectionRange(el.value.length, el.value.length) } catch (e) {} }"
                " else { const r = document.createRange(); r.selectNodeContents(el); r.collapse(false);"
                " const s = getSelection(); s.removeAllRanges(); s.addRange(r) } }"
            )
        page.keyboard.insert_text(text)
        expected = _normal(text) if clear else before + _normal(text)
        value = _normal(_read_field(locator))
        if value.rstrip("\n") != expected.rstrip("\n"):
            if info["editable"]:
                return False, value
            locator.fill(expected, timeout=5000)  # React-style fields sometimes need fill().
            value = _normal(_read_field(locator))
        return value.rstrip("\n") == expected.rstrip("\n"), value

    matched, value = ctx.browser.run(job)
    if matched is None:
        return ToolResult(value, ok=False)
    if matched:
        return ToolResult(f"Typed and read back: field matches ({len(value)} characters).")
    return ToolResult(f"Typed, but the field holds something else: {value[:300]!r}", ok=False)


@tool("browser_edit_text", "Find and replace text inside a field by ref, leaving the rest as it is.",
      {"ref": {"type": "string"}, "find": {"type": "string"}, "replace": {"type": "string"}},
      ["ref", "find", "replace"], group="browser", risk=_risk_text, timeout=30)
def browser_edit_text(args, ctx):
    def job(page):
        locator = ctx.browser.locate(page, args["ref"])
        if locator.evaluate(_DESCRIBE_JS)["sensitive"]:
            return None, "This is a password or payment field."
        locator.click(timeout=8000)
        if locator.evaluate(_SELECT_JS, args["find"]) is None:
            return False, "Text not found in the field."
        if args["replace"]:
            page.keyboard.insert_text(args["replace"])
        else:
            page.keyboard.press("Delete")
        return True, _normal(_read_field(locator))

    ok, value = ctx.browser.run(job)
    if ok is None or not ok:
        return ToolResult(value, ok=False)
    return ToolResult(f"Edited. Field now holds: {value[:500]!r}", ok=_normal(args["replace"]) in value)


@tool("browser_select_text", "Select text and return it, ready for copying or typing over. Give a ref (field or element), or none for the whole page; give find for specific text, or none to select everything.",
      {"ref": {"type": "string"}, "find": {"type": "string"}}, group="browser", timeout=20, untrusted=True)
def browser_select_text(args, ctx):
    def job(page):
        locator = ctx.browser.locate(page, args["ref"]) if args.get("ref") else page.locator("body")
        if locator.evaluate(_DESCRIBE_JS)["sensitive"]:
            return None, "This is a password or payment field."
        selected = locator.evaluate(_SELECT_JS, args.get("find"))
        return selected, "" if selected is not None else "Text not found."

    selected, problem = ctx.browser.run(job)
    if selected is None:
        return ToolResult(problem, ok=False)
    return ToolResult(f"Selected {len(selected)} characters: {selected[:4000]}")


@tool("browser_switch", "Switch to a different browser for the following actions. Chrome, Edge and Brave are controlled directly. For anything else, such as Firefox, use app_open and the desktop tools.",
      {"browser": {"type": "string", "enum": list(BROWSER_PATHS)}}, ["browser"], group="browser", timeout=30)
def browser_switch(args, ctx):
    if find_browser(args["browser"]) is None:
        return ToolResult(f"{args['browser']} is not installed on this PC.", ok=False)
    ctx.browser.switch(args["browser"])
    return ToolResult(f"Now using {args['browser']}.")


@tool("browser_type_focused", "Rarely needed: insert text at the caret of the field that already has focus, adding to what is there. Prefer browser_type with a ref.",
      {"text": {"type": "string"}}, ["text"], group="browser", risk=_risk_text, timeout=60)
def browser_type_focused(args, ctx):
    def job(page):
        info = page.evaluate(_FOCUS_JS)
        if info is None:
            return "No field has focus. Click one first."
        if info["sensitive"]:
            return "The focused field is a password or payment field. Use browser_type_secret."
        page.keyboard.insert_text(args["text"])
        return None

    problem = ctx.browser.run(job)
    return ToolResult(problem, ok=False) if problem else ToolResult("Typed into the focused field.")


@tool("browser_type_secret", "Ask the owner for a password or card detail through a masked box and put it in a field by ref. The value never reaches you and is never logged.",
      {"ref": {"type": "string"}, "label": {"type": "string"}}, ["ref", "label"], group="browser", timeout=900)
def browser_type_secret(args, ctx):
    value = ctx.control.ask("secret", {"label": args["label"]})
    if not value:
        return ToolResult("The owner did not provide it.", ok=False)
    ctx.control.session_secrets.add(value)

    def job(page):
        locator = ctx.browser.locate(page, args["ref"])
        locator.fill(value, timeout=8000)
        return locator.evaluate("el => el.value.length")

    length = ctx.browser.run(job)
    return ToolResult("Entered by the owner through the masked box.", ok=length == len(value))


@tool("browser_press", "Press a key in the page, such as 'Enter', 'Tab', 'Control+a', 'Escape'.",
      {"key": {"type": "string"}}, ["key"], group="browser", risk=_risk_press)
def browser_press(args, ctx):
    def job(page):
        _act_and_settle(page, lambda: page.keyboard.press(args["key"]))
        return _page_line(page)

    return ToolResult(f"Pressed {args['key']}. {ctx.browser.run(job)}")


@tool("browser_select", "Choose a dropdown option by its visible label.",
      {"ref": {"type": "string"}, "label": {"type": "string"}}, ["ref", "label"], group="browser")
def browser_select(args, ctx):
    def job(page):
        locator = ctx.browser.locate(page, args["ref"])
        locator.select_option(label=args["label"], timeout=8000)
        return locator.evaluate("el => el.options[el.selectedIndex].text")

    chosen = ctx.browser.run(job)
    return ToolResult(f"Selected {chosen!r}.", ok=chosen.strip() == args["label"].strip())


@tool("browser_check", "Tick or untick a checkbox or radio by ref.",
      {"ref": {"type": "string"}, "checked": {"type": "boolean"}}, ["ref", "checked"], group="browser")
def browser_check(args, ctx):
    ctx.browser.run(lambda page: ctx.browser.locate(page, args["ref"]).set_checked(args["checked"], timeout=8000))
    return ToolResult("Done.")


@tool("browser_scroll", "Scroll the page by pixels (positive is down), or to 'top' or 'bottom'.",
      {"pixels": {"type": "integer"}, "to": {"type": "string", "enum": ["top", "bottom"]}}, group="browser")
def browser_scroll(args, ctx):
    def job(page):
        if args.get("to"):
            target = "0" if args["to"] == "top" else "document.documentElement.scrollHeight"
            page.evaluate(f"() => window.scrollTo(0, {target})")
        else:
            page.mouse.wheel(0, args.get("pixels", 600))
            page.wait_for_timeout(250)
        return page.evaluate("() => [Math.round(scrollY), document.documentElement.scrollHeight]")

    y, height = ctx.browser.run(job)
    return ToolResult(f"Scrolled to {y} of {height}.")


@tool("browser_nav", "Go back, forward or reload.", {"action": {"type": "string", "enum": ["back", "forward", "reload"]}},
      ["action"], group="browser", timeout=30)
def browser_nav(args, ctx):
    def job(page):
        before = page.url
        action = {"back": page.go_back, "forward": page.go_forward, "reload": page.reload}[args["action"]]
        try:
            action(wait_until="commit", timeout=8000)
        except PlaywrightError:
            # Pages restored from Chrome's back/forward cache report no load events.
            if args["action"] == "reload" or page.url == before:
                raise
        return _page_line(page)

    return ToolResult(ctx.browser.run(job))


@tool("browser_tabs", "List tabs, or open, switch to, or close one.",
      {"action": {"type": "string", "enum": ["list", "new", "switch", "close"]}, "index": {"type": "integer"},
       "url": {"type": "string"}}, ["action"], group="browser", timeout=30, untrusted=True)
def browser_tabs(args, ctx):
    action = args["action"]

    def job(page):
        browser = ctx.browser
        pages = browser._context.pages
        if action == "new":
            browser.page = browser._context.new_page()
            if args.get("url"):
                browser.page.goto(args["url"], wait_until="domcontentloaded", timeout=30000)
        elif action in ("switch", "close"):
            index = args.get("index", -1)
            if not 0 <= index < len(pages):
                raise IndexError(f"No tab {index}; there are {len(pages)}.")
            if action == "switch":
                browser.page = pages[index]
                browser.page.bring_to_front()
            else:
                was_active = pages[index] == browser.page
                pages[index].close()
                if was_active:
                    browser.page = None
                    browser.ensure()
        return [(i, p.title(), p.url, p == browser.page) for i, p in enumerate(browser._context.pages)]

    rows = ctx.browser.run(job)
    return ToolResult("\n".join(f"{'*' if active else ' '} {i}: {title!r} {url}" for i, title, url, active in rows))


@tool("browser_wait", "Wait until text appears on the page.", {"text": {"type": "string"}, "timeout": {"type": "integer"}},
      ["text"], group="browser", phase="OBSERVE", timeout=30)
def browser_wait(args, ctx):
    seconds = min(args.get("timeout", 10), 25)

    def job(page):
        page.get_by_text(args["text"]).first.wait_for(timeout=seconds * 1000)
        return _page_line(page)

    return ToolResult(f"Found it. {ctx.browser.run(job)}")


@tool("browser_upload", "Attach a local file to a file input by ref.",
      {"ref": {"type": "string"}, "path": {"type": "string"}}, ["ref", "path"], group="browser", risk=_risk_upload)
def browser_upload(args, ctx):
    if not Path(args["path"]).is_file():
        return ToolResult(f"No such file: {args['path']}", ok=False)

    def job(page):
        locator = ctx.browser.locate(page, args["ref"])
        locator.set_input_files(args["path"], timeout=8000)
        return locator.evaluate("el => el.files.length ? el.files[0].name : ''")

    name = ctx.browser.run(job)
    return ToolResult(f"Attached {name}.", ok=name == Path(args["path"]).name)


@tool("browser_download", "Click a link or button by ref and save the download. Returns the saved path.",
      {"ref": {"type": "string"}, "folder": {"type": "string"}}, ["ref"], group="browser", timeout=60)
def browser_download(args, ctx):
    folder = Path(args.get("folder") or data_dir() / "downloads")
    folder.mkdir(parents=True, exist_ok=True)

    def job(page):
        with page.expect_download(timeout=45000) as info:
            ctx.browser.locate(page, args["ref"]).click(timeout=8000)
        download = info.value
        target = folder / Path(download.suggested_filename).name
        download.save_as(str(target))
        return str(target)

    path = ctx.browser.call(lambda: job(ctx.browser.ensure()), timeout=55)
    size = Path(path).stat().st_size
    return ToolResult(f"Saved {path} ({size} bytes). Treat this file as untrusted.", ok=size > 0)
