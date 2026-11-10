"""The owner's own everyday browser, reached through the SUDO Browser Link extension.
This is where their logins live, so every approval rule from the other browser tools applies."""
from __future__ import annotations

import asyncio
import re
import threading
import uuid
from concurrent.futures import Future

from .. import policy
from .base import ToolContext, ToolResult, tool

NOT_LINKED = ("The SUDO Browser Link extension is not connected. Ask the owner to install and pair it (Settings tab), "
              "or use the browser_* tools with SUDO's own browser.")


class ExtensionBridge:
    """Lets tool threads send commands over the extension's WebSocket and wait for replies."""

    def __init__(self):
        self._socket = None
        self._loop = None
        self._pending: dict[str, Future] = {}
        self._lock = threading.Lock()
        self.focus_area = ""  # Set while the owner has limited SUDO to one part of a page.

    @property
    def connected(self) -> bool:
        return self._socket is not None

    def attach(self, socket, loop) -> None:
        with self._lock:
            self._socket, self._loop = socket, loop

    def detach(self, socket) -> None:
        with self._lock:
            if self._socket is not socket:
                return
            self._socket = self._loop = None
            pending, self._pending = self._pending, {}
        for future in pending.values():
            future.set_exception(ConnectionError("The browser extension disconnected."))

    def deliver(self, message: dict) -> None:
        with self._lock:
            future = self._pending.pop(message.get("id", ""), None)
        if future is None:
            return
        if "error" in message:
            future.set_exception(RuntimeError(message["error"]))
        else:
            future.set_result(message.get("result"))

    def request(self, cmd: str, timeout: float = 40.0, **args):
        with self._lock:
            socket, loop = self._socket, self._loop
            if socket is None:
                raise ConnectionError(NOT_LINKED)
            request_id = uuid.uuid4().hex
            future: Future = Future()
            self._pending[request_id] = future
        asyncio.run_coroutine_threadsafe(socket.send_json({"id": request_id, "cmd": cmd, "args": args}), loop)
        try:
            return future.result(timeout)
        finally:
            with self._lock:
                self._pending.pop(request_id, None)


def _line(page: dict) -> str:
    return f"Tab: {page['title']!r} at {page['url']}"


def _need(ctx: ToolContext) -> ExtensionBridge:
    if ctx.bridge is None or not ctx.bridge.connected:
        raise ConnectionError(NOT_LINKED)
    return ctx.bridge


def _normal(text: str) -> str:
    return (text or "").replace("\r\n", "\n").replace("\r", "\n").replace("\xa0", " ").rstrip("\n")


def _focus_note(bridge, page: dict) -> list[str]:
    """Keeps SUDO's own record of focus mode in step with the extension, which is what enforces it."""
    area = page.get("focus") or ""
    if hasattr(bridge, "focus_area"):
        bridge.focus_area = area
    return [f"FOCUS MODE: the owner lets you see only {area!r}. The rest of the page, its title and its link are hidden."] if area else []


# ---- Risk checks (the same rules as the other browser) ----------------------------

def _risk_click(args: dict, ctx: ToolContext) -> policy.Decision:
    info = _need(ctx).request("describe", ref=args["ref"])
    if policy.is_finalize_label(info["name"]):
        return policy.confirm(f'In your own browser, this clicks "{info["name"]}", which finalizes something.')
    if info["submits"] and not info["searchForm"]:
        return policy.confirm(f'In your own browser, this submits a form with the button "{info["name"]}".')
    return policy.OK


def _risk_press(args: dict, ctx: ToolContext) -> policy.Decision:
    key = args["key"].lower()
    if "enter" not in key:
        return policy.OK
    info = _need(ctx).request("describe")
    if info is None:
        return policy.OK
    if info["editable"]:
        return policy.confirm("Pressing Enter in this editor may send the message.")
    if info["tag"] == "textarea" and key != "enter":
        return policy.confirm("This shortcut may send the text.")
    if info["tag"] == "input" and info["inForm"] and not info["searchForm"]:
        return policy.confirm("Pressing Enter here submits the form.")
    return policy.OK


def _risk_type(args: dict, ctx: ToolContext) -> policy.Decision:
    if policy.contains_secret(args["text"]):
        return policy.confirm("The text looks like it contains a password, key or card number.")
    return policy.OK


# ---- Tools -------------------------------------------------------------------------

_TOO_BROAD = {"http", "https", "www", "com", ".com", "://", "org", "net", "html"}


def _risk_tabs(args: dict, ctx: ToolContext) -> policy.Decision:
    return policy.confirm("This closes one of your own browser tabs.") if args["action"] == "close" else policy.OK


@tool("my_tabs", "Tabs in the owner's own everyday browser. For their privacy you are shown only the tab they are on, tabs you opened, "
      "and tabs they named. action list shows those; new opens a url; switch or close take tab (a number from the list). "
      "To reach a tab the owner mentioned that is not listed, use switch with match set to their own words for it (for example 'gmail').",
      {"action": {"type": "string", "enum": ["list", "new", "switch", "close"]}, "tab": {"type": "integer"},
       "match": {"type": "string"}, "url": {"type": "string"}},
      ["action"], group="browser", timeout=45, untrusted=True, risk=_risk_tabs)
def my_tabs(args, ctx):
    match = (args.get("match") or "").strip()
    if args["action"] == "switch" and not match and args.get("tab") is None:
        return ToolResult("Give tab (a number from the list) or match (the owner's words for the tab).", ok=False)
    if args["action"] == "close" and args.get("tab") is None:
        return ToolResult("Give tab, a number from the list.", ok=False)
    if match and (len(match) < 3 or match.lower() in _TOO_BROAD):
        return ToolResult("That match is too broad. Use the specific words the owner used for the tab.", ok=False)
    reply = _need(ctx).request("tabs", action=args["action"], tab=args.get("tab"), match=match, url=args.get("url"))
    lines = [f"{'*' if r['active'] else ' '} tab {r['tab']}: {r['title']!r} {r['url']}" for r in reply["rows"]]
    if reply["hidden"]:
        lines.append(f"({reply['hidden']} other tabs are private and not shown. Only look for one if the owner mentioned it.)")
    if reply.get("matched") == 0:
        return ToolResult("\n".join([f"No open tab matches {match!r}."] + lines), ok=False)
    if (reply.get("matched") or 0) > 1:
        lines.insert(0, f"{reply['matched']} tabs match {match!r}; they are in the list now. Switch with tab set to the right number.")
    return ToolResult("\n".join(lines))


@tool("my_browser_open", "Open the owner's own everyday browser (Brave unless they name chrome or edge) and wait for it to be ready, "
      "or bring it to the front if it is already open. Use this when the owner says 'open my browser'.",
      {"browser": {"type": "string", "enum": ["brave", "chrome", "edge"]}}, group="browser", timeout=60)
def my_browser_open(args, ctx):
    import subprocess
    import time

    from .browser import find_browser

    bridge = ctx.bridge
    if bridge is not None and bridge.connected:
        return ToolResult(f"Your browser was already open; brought it to the front. {_line(bridge.request('focus'))}")
    kind = args.get("browser") or "brave"
    path = find_browser(kind)
    if not path:
        return ToolResult(f"{kind} is not installed on this PC.", ok=False)
    subprocess.Popen([path], creationflags=getattr(subprocess, "DETACHED_PROCESS", 0))
    deadline = time.monotonic() + 40
    while bridge is not None and not bridge.connected and time.monotonic() < deadline:
        ctx.control.sleep(0.5)
    if bridge is None or not bridge.connected:
        return ToolResult(f"Opened {kind}, but the SUDO Browser Link extension has not connected. "
                          "It may not be installed or paired in this browser (Settings tab).", ok=False)
    return ToolResult(f"Opened {kind}. {_line(bridge.request('focus'))}")


@tool("my_open", "Go to a URL in the owner's own browser, in the current tab. Use my_tabs with action new to keep their current page.",
      {"url": {"type": "string"}}, ["url"], group="browser", timeout=45, untrusted=True)
def my_open(args, ctx):
    url = args["url"].strip()
    if not re.match(r"(?i)^https?://", url):
        if re.match(r"(?i)^[a-z][a-z0-9+.\-]*:", url):
            return ToolResult("Only http and https addresses are allowed.", ok=False)
        url = "https://" + url
    return ToolResult(f"Loaded. {_line(_need(ctx).request('open', url=url))}")


@tool("my_snapshot", "List the interactive elements of the tab the owner is looking at in their own browser, with refs for the other my_* tools. "
      "Includes forms inside embedded frames (refs like f12e3) and shadow DOM.",
      group="browser", phase="OBSERVE", timeout=30, untrusted=True)
def my_snapshot(args, ctx):
    bridge = _need(ctx)
    page = bridge.request("snapshot")
    items = sorted(page["items"], key=lambda i: not i["inView"])
    lines = [_line(page)] + _focus_note(bridge, page)
    if page["captcha"]:
        lines.append("CAPTCHA DETECTED: do not attempt it. Use ask_owner so the owner can solve it.")
    for item in items[:150]:
        bits = [item["ref"], item["tag"]]
        bits += [f"type={item['type']}"] if item["type"] else []
        bits += [f"role={item['role']}"] if item["role"] else []
        bits += [repr(item["name"])] if item["name"] else []
        bits += [f"placeholder={item['placeholder']!r}"] if item["placeholder"] else []
        bits += [f"value={item['value']!r}"] if item["value"] else []
        bits += ["SENSITIVE(use my_type_secret)"] if item["sensitive"] else []
        bits += ["disabled"] if item["disabled"] else []
        bits += ["checked"] if item["checked"] else []
        bits += ["below-fold"] if not item["inView"] else []
        bits += ["in-frame"] if item.get("framed") else []
        lines.append(" ".join(bits))
    return ToolResult("\n".join(lines))


@tool("my_read", "Read the text of the tab the owner is looking at in their own browser, including embedded frames.", {"max_chars": {"type": "integer"}},
      group="browser", phase="OBSERVE", timeout=30, untrusted=True)
def my_read(args, ctx):
    bridge = _need(ctx)
    page = bridge.request("read")
    limit = min(args.get("max_chars", 12000), 60000)
    return ToolResult("\n".join([_line(page)] + _focus_note(bridge, page) + [page["text"][:limit]]))


def _risk_focus(args: dict, ctx: ToolContext) -> policy.Decision:
    if args["action"] == "clear":
        return policy.confirm("SUDO wants to see your whole page again, not only the part you allowed.")
    return policy.OK


@tool("my_focus", "Limit what you can see in the owner's own browser to one part of the page they are on, such as 'registration form'. "
      "Use action set with area in the owner's own words as soon as they say to look only at something. From then on the rest of the page, "
      "its title, its link and their other tabs are hidden from you. Use action clear only when the owner says you may see the whole page again.",
      {"action": {"type": "string", "enum": ["set", "clear"]}, "area": {"type": "string"}}, ["action"],
      group="browser", risk=_risk_focus, timeout=45)
def my_focus(args, ctx):
    bridge = _need(ctx)
    if args["action"] == "clear":
        bridge.request("scope_clear")
        bridge.focus_area = ""
        return ToolResult("Focus mode is off. You can see the whole page again.")
    area = (args.get("area") or "").strip()
    if not area:
        return ToolResult("Give area: the owner's words for the part to look at.", ok=False)
    bridge.focus_area = area  # Set first, so nothing else is looked at even if finding it fails.
    found = bridge.request("scope_set", area=area)
    return ToolResult(f"Focus mode is on. You now see only {area!r} ({found['fields']} fields, labelled {found['label']!r}). "
                      "Everything else on the page, its title and its link are hidden. Take my_snapshot to work in it.")


@tool("my_screenshot", "See the current tab of the owner's own browser as an image.", group="browser", phase="OBSERVE", timeout=30, untrusted=True)
def my_screenshot(args, ctx):
    page = _need(ctx).request("screenshot")
    return ToolResult(_line(page), images=[("image/jpeg", page["data"])])


@tool("my_click", "Click an element by ref in the owner's own browser.", {"ref": {"type": "string"}}, ["ref"],
      group="browser", risk=_risk_click, timeout=45, untrusted=True)
def my_click(args, ctx):
    return ToolResult(f"Clicked. {_line(_need(ctx).request('click', ref=args['ref']))}")


@tool("my_type", "Type any text into a field by ref in the owner's own browser, then read it back. Never submits.",
      {"ref": {"type": "string"}, "text": {"type": "string"}, "clear": {"type": "boolean"}}, ["ref", "text"],
      group="browser", risk=_risk_type, timeout=45)
def my_type(args, ctx):
    bridge = _need(ctx)
    if bridge.request("describe", ref=args["ref"])["sensitive"]:
        return ToolResult("This is a password or payment field. Use my_type_secret.", ok=False)
    clear = args.get("clear", True)
    value = _normal(bridge.request("type", ref=args["ref"], text=args["text"], clear=clear))
    if value == _normal(args["text"]) or (not clear and value.endswith(_normal(args["text"]))):
        return ToolResult(f"Typed and read back: field matches ({len(value)} characters).")
    return ToolResult(f"Typed, but the field holds something else: {value[:300]!r}", ok=False)


@tool("my_type_secret", "Ask the owner for a password or card detail through a masked box and put it in a field by ref in their own browser. The value never reaches you.",
      {"ref": {"type": "string"}, "label": {"type": "string"}}, ["ref", "label"], group="browser", timeout=900)
def my_type_secret(args, ctx):
    bridge = _need(ctx)
    value = ctx.control.ask("secret", {"label": args["label"]})
    if not value:
        return ToolResult("The owner did not provide it.", ok=False)
    ctx.control.session_secrets.add(value)
    filled = bridge.request("type", ref=args["ref"], text=value, clear=True)
    return ToolResult("Entered by the owner through the masked box.", ok=len(filled or "") == len(value))


@tool("my_press", "Press a key in the owner's own browser, such as 'Enter', 'Tab', 'Control+a', 'Escape'.",
      {"key": {"type": "string"}}, ["key"], group="browser", risk=_risk_press, timeout=30)
def my_press(args, ctx):
    return ToolResult(f"Pressed {args['key']}. {_line(_need(ctx).request('press', key=args['key']))}")


@tool("my_select", "Choose a dropdown option by its visible label in the owner's own browser.",
      {"ref": {"type": "string"}, "label": {"type": "string"}}, ["ref", "label"], group="browser")
def my_select(args, ctx):
    chosen = _need(ctx).request("select", ref=args["ref"], label=args["label"])
    return ToolResult(f"Selected {chosen!r}." if chosen else "That option is not in the list.", ok=bool(chosen))


@tool("my_scroll", "Scroll the current tab of the owner's own browser by pixels (positive is down).",
      {"pixels": {"type": "integer"}}, ["pixels"], group="browser")
def my_scroll(args, ctx):
    y, height = _need(ctx).request("scroll", pixels=args["pixels"])
    return ToolResult(f"Scrolled to {y} of {height}.")
