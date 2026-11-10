"""Windows File Explorer: open folders, see what is showing and selected, select items."""
from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path

import pythoncom
import win32com.client

from .base import ToolResult, tool
from .desktop import focus_window


def _norm(path: str) -> str:
    return os.path.normcase(os.path.normpath(path))


def _name(item) -> str:
    """The real file name. item.Name drops extensions when Explorer is set to hide them."""
    return os.path.basename(item.Path) or item.Name


def _windows() -> list:
    found = []
    for window in win32com.client.Dispatch("Shell.Application").Windows():
        try:
            if window.FullName.lower().endswith("explorer.exe"):
                window.Document.Folder.Self.Path  # noqa: B018 - skips windows with no real folder
                found.append(window)
        except Exception:  # noqa: BLE001 - windows can vanish while being listed
            continue
    return found


def _find(path: str):
    target = _norm(path)
    for window in _windows():
        if _norm(window.Document.Folder.Self.Path) == target:
            return window
    return None


def _com(fn):
    def wrapper(args, ctx):
        pythoncom.CoInitialize()
        try:
            return fn(args, ctx)
        finally:
            pythoncom.CoUninitialize()

    wrapper.__name__ = fn.__name__
    return wrapper


@tool("explorer_open", "Open a folder in File Explorer, or open the folder containing a file with that file selected.",
      {"path": {"type": "string"}}, ["path"], group="computer", timeout=25)
@_com
def explorer_open(args, ctx):
    path = Path(args["path"]).expanduser()
    if not path.exists():
        return ToolResult(f"No such path: {path}", ok=False)
    folder = path if path.is_dir() else path.parent
    argument = [str(path)] if path.is_dir() else [f"/select,{path}"]
    subprocess.Popen(["explorer", *argument])
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        window = _find(str(folder))
        if window is not None:
            focus_window(window.HWND)
            return ToolResult(f"File Explorer is showing {folder}.")
        ctx.control.sleep(0.2)
    return ToolResult("Explorer did not show the folder within 10s.", ok=False)


@tool("explorer_view", "See which folders File Explorer is showing, their contents and what is selected.",
      group="computer", phase="OBSERVE", timeout=20, untrusted=True)
@_com
def explorer_view(args, ctx):
    lines = []
    for window in _windows():
        view = window.Document
        items = [_name(item) for item in view.Folder.Items()]
        selected = [_name(item) for item in view.SelectedItems()]
        lines.append(f"{view.Folder.Self.Path}\n  items ({len(items)}): {', '.join(items[:80])}\n  selected: {', '.join(selected) or '(none)'}")
    return ToolResult("\n".join(lines) or "No File Explorer windows are open.")


@tool("explorer_select", "Select files in an open File Explorer folder by name, replacing the current selection.",
      {"folder": {"type": "string"}, "names": {"type": "array", "items": {"type": "string"}}}, ["folder", "names"],
      group="computer", timeout=20)
@_com
def explorer_select(args, ctx):
    window = _find(args["folder"])
    if window is None:
        return ToolResult("That folder is not open in File Explorer. Use explorer_open first.", ok=False)
    view = window.Document
    deselect_others, select_item, show, focus = 4, 1, 8, 16
    first = True
    for name in args["names"]:
        item = view.Folder.ParseName(name)
        if item is None:
            return ToolResult(f"No item named {name!r} there.", ok=False)
        view.SelectItem(item, select_item | show | ((deselect_others | focus) if first else 0))
        first = False
    time.sleep(0.2)
    chosen = sorted(_name(item) for item in view.SelectedItems())
    return ToolResult(f"Selected: {', '.join(chosen)}", ok=chosen == sorted(args["names"]))


@tool("explorer_close", "Close a File Explorer folder window.", {"folder": {"type": "string"}}, ["folder"], group="computer", timeout=15)
@_com
def explorer_close(args, ctx):
    window = _find(args["folder"])
    if window is None:
        return ToolResult("That folder is not open.", ok=False)
    window.Quit()
    ctx.control.sleep(0.5)
    return ToolResult("Closed.", ok=_find(args["folder"]) is None)
