"""Files: read, write, edit, search, organize. Overwrites are backed up; deletes go to the Recycle Bin."""
from __future__ import annotations

import fnmatch
import hashlib
import os
import re
import shutil
import time
from collections import defaultdict
from pathlib import Path

from .. import policy
from ..config import data_dir
from .base import ToolContext, ToolResult, tool

_SKIP_DIRS = {".git", "node_modules", ".venv", "__pycache__", ".next", "dist", "build"}


def _risk_read(args, ctx):
    return policy.check_read(args["path"])


def _risk_write(args, ctx):
    decision = policy.check_write(args["path"])
    if decision.level != policy.ALLOW:
        return decision
    if Path(args["path"]).exists() and args.get("mode", "create") == "overwrite":
        return policy.confirm(f"This overwrites {args['path']}. A backup is kept.")
    return policy.OK


def _risk_edit(args, ctx):
    return policy.check_write(args["path"])


def _risk_transfer(args, ctx):
    source = policy.check_read(args["src"])
    if source.level != policy.ALLOW:
        return source
    return policy.check_write(args["dst"])


def _risk_delete(args, ctx):
    decision = policy.check_write(args["path"])
    if decision.level == policy.BLOCK:
        return decision
    return policy.confirm(f"This moves {args['path']} to the Recycle Bin.")


def backup(path: Path) -> Path:
    folder = data_dir() / "backups" / time.strftime("%Y%m%d-%H%M%S")
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / path.name
    shutil.copy2(path, target)
    return target


def recycle(path: Path) -> None:
    from win32com.shell import shell, shellcon

    code, aborted = shell.SHFileOperation(
        (0, shellcon.FO_DELETE, str(path.resolve()), None,
         shellcon.FOF_ALLOWUNDO | shellcon.FOF_NOCONFIRMATION | shellcon.FOF_SILENT | shellcon.FOF_NOERRORUI, None, None)
    )
    if code != 0 or aborted:
        raise OSError(f"Windows could not recycle the file (code {code}).")


@tool("file_read", "Read a text file. Use offset and limit (in lines) for large ones.",
      {"path": {"type": "string"}, "offset": {"type": "integer"}, "limit": {"type": "integer"}},
      ["path"], group="files", risk=_risk_read, phase="OBSERVE", retries=1, untrusted=True)
def file_read(args, ctx):
    path = Path(args["path"]).expanduser()
    if not path.is_file():
        return ToolResult(f"No such file: {path}", ok=False)
    head = path.open("rb").read(4096)
    if b"\0" in head:
        return ToolResult(f"{path} is a binary file ({path.stat().st_size} bytes).", ok=False)
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    start = args.get("offset", 0)
    chunk = lines[start:start + args.get("limit", 2000)]
    text = "\n".join(chunk)[:60000]
    more = f"\n[{len(lines) - start - len(chunk)} more lines]" if start + len(chunk) < len(lines) else ""
    return ToolResult(text + more)


@tool("file_write", "Create a text file. mode: create (fails if it exists), overwrite (backs up first), append.",
      {"path": {"type": "string"}, "content": {"type": "string"},
       "mode": {"type": "string", "enum": ["create", "overwrite", "append"]}},
      ["path", "content"], group="files", risk=_risk_write)
def file_write(args, ctx):
    path = Path(args["path"]).expanduser()
    mode = args.get("mode", "create")
    if path.exists() and mode == "create":
        return ToolResult(f"{path} already exists. Use mode overwrite or append.", ok=False)
    path.parent.mkdir(parents=True, exist_ok=True)
    note = ""
    if path.exists() and mode == "overwrite":
        note = f" Backup: {backup(path)}"
    with open(path, "a" if mode == "append" else "w", encoding="utf-8", newline="") as fh:
        fh.write(args["content"])
    return ToolResult(f"Wrote {path} ({path.stat().st_size} bytes).{note}")


@tool("file_edit", "Replace an exact piece of text in a file. It must occur exactly once unless replace_all is true. Backs up first.",
      {"path": {"type": "string"}, "old": {"type": "string"}, "new": {"type": "string"}, "replace_all": {"type": "boolean"}},
      ["path", "old", "new"], group="files", risk=_risk_edit)
def file_edit(args, ctx):
    path = Path(args["path"]).expanduser()
    if not path.is_file():
        return ToolResult(f"No such file: {path}", ok=False)
    with open(path, encoding="utf-8", newline="") as fh:
        text = fh.read()
    count = text.count(args["old"])
    if count == 0:
        return ToolResult("The text to replace was not found.", ok=False)
    if count > 1 and not args.get("replace_all"):
        return ToolResult(f"The text occurs {count} times. Give more context or set replace_all.", ok=False)
    saved = backup(path)
    with open(path, "w", encoding="utf-8", newline="") as fh:
        fh.write(text.replace(args["old"], args["new"]))
    return ToolResult(f"Edited {path} ({count} replacement(s)). Backup: {saved}")


@tool("file_list", "List a folder.", {"path": {"type": "string"}}, ["path"], group="files", phase="OBSERVE", retries=1, untrusted=True)
def file_list(args, ctx):
    path = Path(args["path"]).expanduser()
    if not path.is_dir():
        return ToolResult(f"No such folder: {path}", ok=False)
    rows = []
    for entry in sorted(path.iterdir(), key=lambda e: (e.is_file(), e.name.lower()))[:500]:
        try:
            rows.append(f"{'file' if entry.is_file() else 'dir '} {entry.stat().st_size:>12} {entry.name}")
        except OSError:
            rows.append(f"?    {entry.name}")
    return ToolResult("\n".join(rows) or "(empty)")


@tool("file_search", "Find files under a folder by name pattern, and optionally by a regex in their contents.",
      {"root": {"type": "string"}, "pattern": {"type": "string", "description": "For example *.py"},
       "contains": {"type": "string"}, "limit": {"type": "integer"}},
      ["root"], group="files", phase="OBSERVE", timeout=90, untrusted=True)
def file_search(args, ctx):
    root = Path(args["root"]).expanduser()
    if not root.is_dir():
        return ToolResult(f"No such folder: {root}", ok=False)
    pattern, limit = args.get("pattern", "*"), args.get("limit", 200)
    regex = re.compile(args["contains"]) if args.get("contains") else None
    hits: list[str] = []
    for folder, dirs, names in os.walk(root):
        ctx.control.checkpoint()
        dirs[:] = [d for d in dirs if d not in _SKIP_DIRS]
        for name in names:
            if not fnmatch.fnmatch(name.lower(), pattern.lower()):
                continue
            full = os.path.join(folder, name)
            if policy.check_read(full).level == policy.BLOCK:
                continue
            if regex:
                try:
                    if os.path.getsize(full) > 2_000_000:
                        continue
                    with open(full, encoding="utf-8", errors="ignore") as fh:
                        if not regex.search(fh.read()):
                            continue
                except OSError:
                    continue
            hits.append(full)
            if len(hits) >= limit:
                return ToolResult("\n".join(hits) + "\n[limit reached]")
    return ToolResult("\n".join(hits) or "(nothing found)")


@tool("file_move", "Move or rename a file or folder. Refuses to overwrite.",
      {"src": {"type": "string"}, "dst": {"type": "string"}}, ["src", "dst"], group="files", risk=_risk_transfer)
def file_move(args, ctx):
    src, dst = Path(args["src"]).expanduser(), Path(args["dst"]).expanduser()
    if not src.exists():
        return ToolResult(f"No such path: {src}", ok=False)
    if dst.is_dir():
        dst = dst / src.name
    if dst.exists():
        return ToolResult(f"{dst} already exists.", ok=False)
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(src), str(dst))
    return ToolResult(f"Moved to {dst}.", ok=dst.exists() and not src.exists())


@tool("file_copy", "Copy a file or folder. Refuses to overwrite.",
      {"src": {"type": "string"}, "dst": {"type": "string"}}, ["src", "dst"], group="files", risk=_risk_transfer, timeout=300)
def file_copy(args, ctx):
    src, dst = Path(args["src"]).expanduser(), Path(args["dst"]).expanduser()
    if not src.exists():
        return ToolResult(f"No such path: {src}", ok=False)
    if dst.is_dir():
        dst = dst / src.name
    if dst.exists():
        return ToolResult(f"{dst} already exists.", ok=False)
    dst.parent.mkdir(parents=True, exist_ok=True)
    if src.is_dir():
        shutil.copytree(src, dst)
    else:
        shutil.copy2(src, dst)
    return ToolResult(f"Copied to {dst}.", ok=dst.exists())


@tool("file_mkdir", "Create a folder.", {"path": {"type": "string"}}, ["path"], group="files",
      risk=lambda a, c: policy.check_write(a["path"]))
def file_mkdir(args, ctx):
    path = Path(args["path"]).expanduser()
    path.mkdir(parents=True, exist_ok=True)
    return ToolResult(f"Folder ready: {path}", ok=path.is_dir())


@tool("file_delete", "Move a file or folder to the Recycle Bin. Needs the owner's approval.",
      {"path": {"type": "string"}}, ["path"], group="files", risk=_risk_delete)
def file_delete(args, ctx):
    path = Path(args["path"]).expanduser()
    if not path.exists():
        return ToolResult(f"No such path: {path}", ok=False)
    recycle(path)
    return ToolResult(f"Moved {path} to the Recycle Bin.", ok=not path.exists())


@tool("file_find_duplicates", "Find identical files under a folder by size and hash. Changes nothing.",
      {"root": {"type": "string"}}, ["root"], group="files", phase="OBSERVE", timeout=300, untrusted=True)
def file_find_duplicates(args, ctx):
    root = Path(args["root"]).expanduser()
    if not root.is_dir():
        return ToolResult(f"No such folder: {root}", ok=False)
    by_size: dict[int, list[str]] = defaultdict(list)
    for folder, dirs, names in os.walk(root):
        ctx.control.checkpoint()
        dirs[:] = [d for d in dirs if d not in _SKIP_DIRS]
        for name in names:
            full = os.path.join(folder, name)
            try:
                size = os.path.getsize(full)
            except OSError:
                continue
            if size:
                by_size[size].append(full)
    by_hash: dict[str, list[str]] = defaultdict(list)
    for size, paths in by_size.items():
        if len(paths) < 2:
            continue
        for full in paths:
            ctx.control.checkpoint()
            digest = hashlib.blake2b()
            try:
                with open(full, "rb") as fh:
                    for block in iter(lambda: fh.read(1 << 20), b""):
                        digest.update(block)
            except OSError:
                continue
            by_hash[digest.hexdigest()].append(full)
    groups = [p for p in by_hash.values() if len(p) > 1]
    if not groups:
        return ToolResult("No duplicates found.")
    return ToolResult("\n\n".join("\n".join(group) for group in groups[:100]))
