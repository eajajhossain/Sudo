"""Desktop control: screen, mouse, any-text keyboard, windows, apps and UI elements."""
from __future__ import annotations

import base64
import ctypes
import io
import os
import re
import subprocess
import time
from ctypes import wintypes

try:
    ctypes.windll.shcore.SetProcessDpiAwareness(2)  # Real pixels, before any GUI library loads.
except OSError:
    pass

import mss
import psutil
import pyautogui
import uiautomation as auto
import win32clipboard
import win32con
import win32gui
import win32process
from PIL import Image

from .. import policy
from .base import ToolContext, ToolResult, tool

pyautogui.FAILSAFE = True  # Slamming the mouse into a screen corner aborts.
pyautogui.PAUSE = 0.03

MAX_WIDTH = 1280
_geom = {"scale": 1.0, "left": 0, "top": 0, "width": 0, "height": 0}
OWN_WINDOW_TITLE = "SUDO"


# ---- Low-level keyboard ------------------------------------------------------

ULONG_PTR = ctypes.c_size_t
INPUT_KEYBOARD, KEYUP, UNICODE = 1, 0x0002, 0x0004
VK_RETURN, VK_TAB, VK_SHIFT, VK_MENU = 0x0D, 0x09, 0x10, 0x12


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [("wVk", wintypes.WORD), ("wScan", wintypes.WORD), ("dwFlags", wintypes.DWORD),
                ("time", wintypes.DWORD), ("dwExtraInfo", ULONG_PTR)]


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [("dx", wintypes.LONG), ("dy", wintypes.LONG), ("mouseData", wintypes.DWORD),
                ("dwFlags", wintypes.DWORD), ("time", wintypes.DWORD), ("dwExtraInfo", ULONG_PTR)]


class _INPUTUNION(ctypes.Union):
    _fields_ = [("ki", KEYBDINPUT), ("mi", MOUSEINPUT)]


class INPUT(ctypes.Structure):
    _fields_ = [("type", wintypes.DWORD), ("u", _INPUTUNION)]


_SendInput = ctypes.windll.user32.SendInput
_SendInput.argtypes = (wintypes.UINT, ctypes.POINTER(INPUT), ctypes.c_int)
_SendInput.restype = wintypes.UINT


def _send(*events: tuple[int, int, int]) -> None:
    items = (INPUT * len(events))()
    for slot, (vk, scan, flags) in zip(items, events):
        slot.type = INPUT_KEYBOARD
        slot.u.ki = KEYBDINPUT(vk, scan, flags, 0, 0)
    if _SendInput(len(events), items, ctypes.sizeof(INPUT)) != len(events):
        raise OSError("Windows rejected the keyboard input (is an admin window focused?).")


def _tap_vk(vk: int) -> None:
    _send((vk, 0, 0), (vk, 0, KEYUP))


EXTENDED_KEY = 0x0001
_EXTENDED_NAMES = {"home", "end", "pageup", "pagedown", "pgup", "pgdn", "left", "right", "up", "down", "insert", "delete", "del"}


def press_combo(names: list[str]) -> None:
    """Press a shortcut. Navigation keys must be flagged 'extended', or Windows treats
    them as number-pad keys and, with NumLock on, Shift+End and friends select nothing."""
    from pyautogui._pyautogui_win import keyboardMapping as mapping

    if any(mapping.get(name) is None or mapping[name] > 0xFF for name in names):
        pyautogui.hotkey(*names)  # Characters that need Shift are worked out by pyautogui.
        return
    codes = [(mapping[name], EXTENDED_KEY if name in _EXTENDED_NAMES else 0) for name in names]
    _send(*[(vk, 0, flag) for vk, flag in codes], *[(vk, 0, flag | KEYUP) for vk, flag in reversed(codes)])
    time.sleep(0.03)


def send_text(text: str, ctx: ToolContext, shift_enter: bool = False) -> None:
    """Type any Unicode text, emoji included, into whatever has keyboard focus."""
    raw = text.replace("\r\n", "\n").encode("utf-16-le")
    units = [int.from_bytes(raw[i:i + 2], "little") for i in range(0, len(raw), 2)]
    for index, unit in enumerate(units):
        if index % 25 == 0:
            ctx.control.checkpoint()
        if unit == 10:
            if shift_enter:
                _send((VK_SHIFT, 0, 0), (VK_RETURN, 0, 0), (VK_RETURN, 0, KEYUP), (VK_SHIFT, 0, KEYUP))
            else:
                _tap_vk(VK_RETURN)
        elif unit == 9:
            _tap_vk(VK_TAB)
        else:
            _send((0, unit, UNICODE), (0, unit, UNICODE | KEYUP))
        if index % 25 == 24:
            time.sleep(0.01)


# ---- Clipboard ---------------------------------------------------------------

def _clip_get() -> str | None:
    win32clipboard.OpenClipboard()
    try:
        if win32clipboard.IsClipboardFormatAvailable(win32con.CF_UNICODETEXT):
            return win32clipboard.GetClipboardData(win32con.CF_UNICODETEXT)
        return None
    finally:
        win32clipboard.CloseClipboard()


def _clip_set(text: str | None) -> None:
    win32clipboard.OpenClipboard()
    try:
        win32clipboard.EmptyClipboard()
        if text is not None:
            win32clipboard.SetClipboardData(win32con.CF_UNICODETEXT, text)
    finally:
        win32clipboard.CloseClipboard()


# ---- Screen ------------------------------------------------------------------

def capture(quality: int = 70) -> tuple[str, int, int]:
    with mss.MSS() as grabber:
        monitor = grabber.monitors[1]
        raw = grabber.grab(monitor)
    img = Image.frombytes("RGB", raw.size, raw.rgb)
    scale = max(1.0, img.width / MAX_WIDTH)
    if scale > 1.0:
        img = img.resize((round(img.width / scale), round(img.height / scale)), Image.BILINEAR)
    _geom.update(scale=scale, left=monitor["left"], top=monitor["top"], width=img.width, height=img.height)
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=quality)
    return base64.b64encode(buf.getvalue()).decode("ascii"), img.width, img.height


def _ensure_geom() -> None:
    if not _geom["width"]:
        capture(30)


def to_screen(x: float, y: float) -> tuple[int, int]:
    """Screenshot coordinates to real screen pixels."""
    _ensure_geom()
    return round(_geom["left"] + x * _geom["scale"]), round(_geom["top"] + y * _geom["scale"])


def from_screen(x: float, y: float) -> tuple[int, int]:
    _ensure_geom()
    return round((x - _geom["left"]) / _geom["scale"]), round((y - _geom["top"]) / _geom["scale"])


# ---- Windows -----------------------------------------------------------------

def foreground() -> dict:
    hwnd = win32gui.GetForegroundWindow()
    name = ""
    try:
        _, pid = win32process.GetWindowThreadProcessId(hwnd)
        name = psutil.Process(pid).name()
    except (psutil.Error, OSError, ValueError):
        pass
    return {"hwnd": hwnd, "title": win32gui.GetWindowText(hwnd), "process": name}


def list_windows() -> list[dict]:
    found: list[dict] = []

    def visit(hwnd, _):
        title = win32gui.GetWindowText(hwnd)
        if title and win32gui.IsWindowVisible(hwnd):
            # Skip cloaked (invisible, suspended) UWP frames.
            cloaked = wintypes.DWORD()
            ctypes.windll.dwmapi.DwmGetWindowAttribute(hwnd, 14, ctypes.byref(cloaked), ctypes.sizeof(cloaked))
            if cloaked.value == 0:
                found.append({"hwnd": hwnd, "title": title})

    win32gui.EnumWindows(visit, None)
    return found


def find_window(title: str) -> dict | None:
    needle = title.lower()
    matches = [w for w in list_windows() if needle in w["title"].lower()]
    matches.sort(key=lambda w: (w["title"].lower() != needle, len(w["title"])))
    return matches[0] if matches else None


def focus_window(hwnd: int) -> bool:
    if win32gui.IsIconic(hwnd):
        win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
    _tap_vk(VK_MENU)  # Windows only lets the last-input process change the foreground.
    try:
        win32gui.SetForegroundWindow(hwnd)
    except win32gui.error:
        pass
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        if win32gui.GetForegroundWindow() == hwnd:
            return True
        time.sleep(0.05)
    return False


def _guard_own_window() -> str | None:
    if foreground()["title"] == OWN_WINDOW_TITLE:
        return "SUDO's own window is focused. Focus the target window first (window_focus or desktop_click)."
    return None


# ---- UI Automation -----------------------------------------------------------

def _focused():
    return auto.GetFocusedControl()


def _value_of(control) -> str | None:
    try:
        pattern = control.GetPattern(auto.PatternId.ValuePattern)
        if pattern is not None:
            return pattern.Value
    except Exception:
        pass
    try:
        pattern = control.GetPattern(auto.PatternId.TextPattern)
        if pattern is not None:
            return pattern.DocumentRange.GetText(20000)
    except Exception:
        pass
    return None


def _root(title: str | None):
    if title:
        match = find_window(title)
        if match is None:
            raise LookupError(f"No window matching {title!r}.")
        return auto.ControlFromHandle(match["hwnd"])
    return auto.GetForegroundControl()


def _walk(root, max_nodes: int, max_depth: int):
    deadline = time.monotonic() + 8
    stack = [(root, 0)]
    count = 0
    while stack and count < max_nodes and time.monotonic() < deadline:
        control, depth = stack.pop()
        yield control, depth
        count += 1
        if depth < max_depth:
            try:
                stack.extend((c, depth + 1) for c in reversed(control.GetChildren()))
            except Exception:
                pass


def _center(control) -> tuple[int, int] | None:
    rect = control.BoundingRectangle
    if rect.width() <= 0 or rect.height() <= 0:
        return None
    return (rect.left + rect.right) // 2, (rect.top + rect.bottom) // 2


# ---- Risk checks -------------------------------------------------------------

def _risk_click(args: dict, ctx: ToolContext) -> policy.Decision:
    px, py = to_screen(args["x"], args["y"])
    with auto.UIAutomationInitializerInThread(debug=False):
        control = auto.ControlFromPoint(px, py)
        label = control.Name if control else ""
    if policy.is_finalize_label(label):
        return policy.confirm(f'This clicks "{label}", which finalizes something.')
    return policy.OK


def _risk_ui_click(args: dict, ctx: ToolContext) -> policy.Decision:
    if policy.is_finalize_label(args["name"]):
        return policy.confirm(f'This clicks "{args["name"]}", which finalizes something.')
    return policy.OK


def _risk_type(args: dict, ctx: ToolContext) -> policy.Decision:
    if policy.contains_secret(args["text"]):
        return policy.confirm("The text looks like it contains a password, key or card number.")
    if "\n" in args["text"] and policy.is_message_app(foreground()["process"]):
        if args.get("mode") != "paste":
            return policy.confirm("Pressing Enter here may send the message.")
    return policy.OK


def _risk_key(args: dict, ctx: ToolContext) -> policy.Decision:
    keys = args["keys"].lower()
    if "enter" in keys or "return" in keys:
        info = foreground()
        if policy.is_message_app(info["process"]):
            return policy.confirm(f"Pressing Enter in {info['process']} may send or submit something.")
    if keys in ("alt+f4",):
        return policy.confirm("This closes the focused window.")
    return policy.OK


def _risk_open(args: dict, ctx: ToolContext) -> policy.Decision:
    target = args["name"].strip().strip('"')
    if re.search(r"(?i)\.(exe|msi|bat|cmd|ps1|vbs|js|scr|com)$", target) and re.search(r"[\\/]", target):
        return policy.confirm(f"This runs the program file {target}.")
    return policy.OK


# ---- Tools -------------------------------------------------------------------

@tool("desktop_screenshot", "See the whole primary screen. Coordinates in other desktop tools use this image's pixels.",
      group="computer", phase="OBSERVE", retries=1, timeout=15, untrusted=True)
def desktop_screenshot(args, ctx):
    data, width, height = capture()
    fg = foreground()
    return ToolResult(f"Screen {width}x{height}. Focused window: {fg['title']!r} ({fg['process']}).",
                      images=[("image/jpeg", data)])


_XY = {"x": {"type": "number"}, "y": {"type": "number"}}


@tool("desktop_click", "Click at a screenshot coordinate.",
      {**_XY, "button": {"type": "string", "enum": ["left", "right", "middle"]},
       "count": {"type": "integer", "description": "2 for double-click"}},
      ["x", "y"], group="computer", risk=_risk_click)
def desktop_click(args, ctx):
    x, y = to_screen(args["x"], args["y"])
    pyautogui.click(x, y, clicks=args.get("count", 1), interval=0.08, button=args.get("button", "left"))
    pos = pyautogui.position()
    return ToolResult(f"Clicked. Pointer is at screen ({pos.x},{pos.y}).", ok=abs(pos.x - x) <= 2 and abs(pos.y - y) <= 2)


@tool("desktop_move", "Move the pointer to a screenshot coordinate.", _XY, ["x", "y"], group="computer")
def desktop_move(args, ctx):
    x, y = to_screen(args["x"], args["y"])
    pyautogui.moveTo(x, y, duration=0.1)
    return ToolResult("Moved.")


@tool("desktop_drag", "Drag from one screenshot coordinate to another.",
      {"x1": {"type": "number"}, "y1": {"type": "number"}, "x2": {"type": "number"}, "y2": {"type": "number"}},
      ["x1", "y1", "x2", "y2"], group="computer")
def desktop_drag(args, ctx):
    x1, y1 = to_screen(args["x1"], args["y1"])
    x2, y2 = to_screen(args["x2"], args["y2"])
    pyautogui.moveTo(x1, y1, duration=0.1)
    pyautogui.dragTo(x2, y2, duration=0.4, button="left")
    return ToolResult("Dragged.")


@tool("desktop_scroll", "Scroll the wheel at the pointer or at a coordinate. Positive scrolls up.",
      {"clicks": {"type": "integer"}, **_XY}, ["clicks"], group="computer")
def desktop_scroll(args, ctx):
    if "x" in args and "y" in args:
        pyautogui.moveTo(*to_screen(args["x"], args["y"]), duration=0.05)
    pyautogui.scroll(args["clicks"] * 120)
    return ToolResult("Scrolled.")


@tool("desktop_type", "Type any text into the focused control. mode 'keys' sends each character; 'paste' uses the clipboard (best for long or multi-line text). The focused control is read back when it exposes its text.",
      {"text": {"type": "string"}, "mode": {"type": "string", "enum": ["keys", "paste"]},
       "shift_enter": {"type": "boolean", "description": "Send newlines as Shift+Enter"}},
      ["text"], group="computer", risk=_risk_type, timeout=120)
def desktop_type(args, ctx):
    own = _guard_own_window()
    if own:
        return ToolResult(own, ok=False)
    text = args["text"]
    with auto.UIAutomationInitializerInThread(debug=False):
        focused = _focused()
        if focused is not None and getattr(focused, "IsPassword", False):
            return ToolResult("The focused field is a password field. Use desktop_type_secret.", ok=False)
        if args.get("mode") == "paste":
            saved = _clip_get()
            try:
                _clip_set(text)
                pyautogui.hotkey("ctrl", "v")
                time.sleep(0.25)
            finally:
                _clip_set(saved)
        else:
            send_text(text, ctx, args.get("shift_enter", False))
        time.sleep(0.1)
        focused = _focused()
        value = _value_of(focused) if focused is not None else None
    if value is None:
        return ToolResult("Typed, but this control does not expose its text. Check it with a screenshot.")
    normal = lambda s: s.replace("\r\n", "\n").replace("\r", "\n")
    if normal(text) in normal(value):
        return ToolResult("Typed and read back from the field: text matches.")
    return ToolResult(f"Typed, but read-back did not match. Field holds: {value[:300]!r}", ok=False)


@tool("desktop_select_text", "Select text in the focused app and return what was selected. mode: all, line (the line at the caret, or at x,y), word (double-click at x,y), range (drag from x,y to x2,y2).",
      {"mode": {"type": "string", "enum": ["all", "line", "word", "range"]}, **_XY, "x2": {"type": "number"}, "y2": {"type": "number"}},
      ["mode"], group="computer", timeout=20, untrusted=True)
def desktop_select_text(args, ctx):
    own = _guard_own_window()
    if own:
        return ToolResult(own, ok=False)
    mode = args["mode"]
    if mode in ("word", "range") and not ("x" in args and "y" in args):
        return ToolResult(f"Mode {mode} needs x and y.", ok=False)
    if mode == "range" and not ("x2" in args and "y2" in args):
        return ToolResult("Mode range needs x2 and y2.", ok=False)
    with auto.UIAutomationInitializerInThread(debug=False):
        focused = _focused()
        if focused is not None and getattr(focused, "IsPassword", False):
            return ToolResult("The focused field is a password field.", ok=False)
    if mode == "all":
        pyautogui.hotkey("ctrl", "a")
    elif mode == "word":
        pyautogui.click(*to_screen(args["x"], args["y"]), clicks=2, interval=0.06)
    elif mode == "line":
        if "x" in args and "y" in args:
            pyautogui.click(*to_screen(args["x"], args["y"]))
        press_combo(["home"])
        press_combo(["shift", "end"])
    else:
        pyautogui.moveTo(*to_screen(args["x"], args["y"]), duration=0.05)
        pyautogui.dragTo(*to_screen(args["x2"], args["y2"]), duration=0.3, button="left")
    time.sleep(0.1)
    saved = _clip_get()
    try:
        _clip_set(None)
        pyautogui.hotkey("ctrl", "c")
        time.sleep(0.25)
        selected = _clip_get()
    finally:
        _clip_set(saved)
    if not selected:
        return ToolResult("Nothing was selected, or this app does not copy text. Check with a screenshot.", ok=False)
    return ToolResult(f"Selected {len(selected)} characters: {selected[:4000]}")


@tool("desktop_type_secret", "Ask the owner for a password through a masked box and type it into the focused field. The value never reaches you.",
      {"label": {"type": "string", "description": "What is being asked for"}}, ["label"], group="computer", timeout=900)
def desktop_type_secret(args, ctx):
    own = _guard_own_window()
    if own:
        return ToolResult(own, ok=False)
    value = ctx.control.ask("secret", {"label": args["label"]})
    if not value:
        return ToolResult("The owner did not provide it.", ok=False)
    ctx.control.session_secrets.add(value)
    send_text(value, ctx)
    return ToolResult("Entered by the owner through the masked box.")


@tool("desktop_key", "Press a key or shortcut, such as 'ctrl+c', 'alt+tab', 'win+r', 'enter', 'f5'.",
      {"keys": {"type": "string"}}, ["keys"], group="computer", risk=_risk_key)
def desktop_key(args, ctx):
    own = _guard_own_window()
    if own:
        return ToolResult(own, ok=False)
    names = [k.strip().lower() for k in args["keys"].split("+")]
    names = ["win" if k in ("windows", "super", "meta") else k for k in names]
    unknown = [k for k in names if k not in pyautogui.KEY_NAMES]
    if unknown:
        return ToolResult(f"Unknown key: {unknown}", ok=False)
    press_combo(names)
    return ToolResult(f"Pressed {args['keys']}.")


@tool("clipboard_read", "Read the text on the clipboard.", group="computer", phase="OBSERVE", untrusted=True)
def clipboard_read(args, ctx):
    return ToolResult(_clip_get() or "(no text on the clipboard)")


@tool("clipboard_write", "Put text on the clipboard.", {"text": {"type": "string"}}, ["text"], group="computer")
def clipboard_write(args, ctx):
    _clip_set(args["text"])
    return ToolResult("Clipboard set." if _clip_get() == args["text"] else "Clipboard did not update.", ok=_clip_get() == args["text"])


@tool("window_list", "List open windows.", group="computer", phase="OBSERVE", retries=1, untrusted=True)
def window_list(args, ctx):
    fg = win32gui.GetForegroundWindow()
    lines = [("* " if w["hwnd"] == fg else "  ") + w["title"] for w in list_windows()]
    return ToolResult("\n".join(lines) or "(no windows)")


@tool("window_focus", "Bring a window to the front by part of its title.", {"title": {"type": "string"}}, ["title"], group="computer")
def window_focus(args, ctx):
    match = find_window(args["title"])
    if match is None:
        return ToolResult(f"No window matching {args['title']!r}.", ok=False)
    if not focus_window(match["hwnd"]):
        return ToolResult(f"Windows refused to focus {match['title']!r}.", ok=False)
    return ToolResult(f"Focused {match['title']!r}.")


@tool("window_close", "Ask a window to close, as its X button does. The app may prompt to save.",
      {"title": {"type": "string"}}, ["title"], group="computer",
      risk=lambda a, c: policy.confirm(f"This closes the window matching {a['title']!r}."))
def window_close(args, ctx):
    match = find_window(args["title"])
    if match is None:
        return ToolResult(f"No window matching {args['title']!r}.", ok=False)
    win32gui.PostMessage(match["hwnd"], win32con.WM_CLOSE, 0, 0)
    ctx.control.sleep(0.7)
    gone = not any(w["hwnd"] == match["hwnd"] for w in list_windows())
    return ToolResult("Closed." if gone else "Window is still open; it may be showing a save prompt.", ok=gone)


def _new_window(before: set[int], wait: float, ctx) -> dict | None:
    deadline = time.monotonic() + wait
    while time.monotonic() < deadline:
        for window in list_windows():
            if window["hwnd"] not in before:
                return window
        ctx.control.sleep(0.2)
    return None


@tool("app_open", "Open an application, file, folder or URL by name or path (for example 'notepad', 'calc', 'Visual Studio Code', a folder path).",
      {"name": {"type": "string"}, "args": {"type": "string"}}, ["name"], group="computer", risk=_risk_open, timeout=40)
def app_open(args, ctx):
    name = args["name"].strip()
    before = {w["hwnd"] for w in list_windows()}
    attempts = []
    try:
        if args.get("args"):
            os.startfile(name, arguments=args["args"])
        else:
            os.startfile(name)
    except OSError as exc:
        attempts.append(f"direct launch failed: {exc}")
        safe = name.replace("'", "''")
        lookup = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command",
             f"(Get-StartApps | Where-Object {{ $_.Name -like '*{safe}*' }} | Select-Object -First 1).AppID"],
            capture_output=True, text=True, timeout=25, creationflags=subprocess.CREATE_NO_WINDOW,
        )
        app_id = lookup.stdout.strip()
        if not app_id:
            return ToolResult(f"Could not find an app called {name!r}. {'; '.join(attempts)}", ok=False)
        os.startfile(f"shell:AppsFolder\\{app_id}")
    window = _new_window(before, 10, ctx)
    if window is None:
        existing = find_window(name)
        if existing:
            focus_window(existing["hwnd"])
            return ToolResult(f"{name} was already open; focused {existing['title']!r}.")
        return ToolResult(f"Launched {name}, but no new window appeared within 10s. Check with a screenshot.", ok=False)
    focus_window(window["hwnd"])
    return ToolResult(f"Opened: {window['title']!r}.")


@tool("terminal_open", "Open a visible PowerShell window for the owner, in a folder.",
      {"cwd": {"type": "string"}}, group="terminal")
def terminal_open(args, ctx):
    subprocess.Popen(["powershell", "-NoExit"], cwd=args.get("cwd") or None, creationflags=subprocess.CREATE_NEW_CONSOLE)
    return ToolResult("Opened a PowerShell window.")


@tool("ui_elements", "List the buttons, fields and other UI elements of the focused window (or a window by title) with their names, values and screenshot coordinates. Often more reliable than reading a screenshot.",
      {"title": {"type": "string"}, "max_nodes": {"type": "integer"}}, group="computer", phase="OBSERVE", retries=1, timeout=20, untrusted=True)
def ui_elements(args, ctx):
    with auto.UIAutomationInitializerInThread(debug=False):
        root = _root(args.get("title"))
        lines = []
        for control, depth in _walk(root, args.get("max_nodes", 250), 14):
            name = (control.Name or "")[:80]
            kind = control.ControlTypeName.replace("Control", "")
            if not name and kind in ("Pane", "Group", "Custom"):
                continue
            center = _center(control)
            at = "at (%d,%d)" % from_screen(*center) if center else "offscreen"
            value = _value_of(control) if kind in ("Edit", "Document", "ComboBox") else None
            extra = f" value={value[:120]!r}" if value else ""
            lines.append(f"{'  ' * min(depth, 8)}[{kind}] {name!r} {at}{extra}")
    return ToolResult("\n".join(lines) or "(no elements)")


@tool("ui_click", "Click a UI element by its name, in the focused window or a window by title.",
      {"name": {"type": "string"}, "control_type": {"type": "string", "description": "For example Button"},
       "title": {"type": "string"}}, ["name"], group="computer", risk=_risk_ui_click, timeout=20)
def ui_click(args, ctx):
    wanted = args["name"].lower()
    kind = (args.get("control_type") or "").lower().replace("control", "")
    with auto.UIAutomationInitializerInThread(debug=False):
        root = _root(args.get("title"))
        exact, partial = None, None
        for control, _ in _walk(root, 800, 16):
            name = (control.Name or "").lower()
            if kind and control.ControlTypeName.lower().replace("control", "") != kind:
                continue
            if name == wanted:
                exact = control
                break
            if partial is None and name and wanted in name:
                partial = control
        target = exact or partial
        if target is None:
            return ToolResult(f"No element named {args['name']!r}.", ok=False)
        if policy.is_finalize_label(target.Name or "") and not policy.is_finalize_label(args["name"]):
            return ToolResult(f"The match {target.Name!r} would finalize something. Call ui_click with that exact name.", ok=False)
        pattern = None
        try:
            pattern = target.GetPattern(auto.PatternId.InvokePattern)
        except Exception:
            pass
        if pattern is not None:
            pattern.Invoke()
            return ToolResult(f"Invoked {target.Name!r}.")
        center = _center(target)
        if center is None:
            return ToolResult(f"{target.Name!r} is off screen.", ok=False)
        pyautogui.click(*center)
        return ToolResult(f"Clicked {target.Name!r}.")


@tool("ui_read_focused", "Read the text of the field that has keyboard focus.", group="computer", phase="OBSERVE", untrusted=True)
def ui_read_focused(args, ctx):
    with auto.UIAutomationInitializerInThread(debug=False):
        control = _focused()
        if control is None:
            return ToolResult("Nothing has focus.", ok=False)
        if getattr(control, "IsPassword", False):
            return ToolResult("The focused field is a password field; its text is not read.", ok=False)
        value = _value_of(control)
        name, kind = control.Name, control.ControlTypeName
    return ToolResult(f"{kind} {name!r}: {value!r}" if value is not None else f"{kind} {name!r}: no readable text")
