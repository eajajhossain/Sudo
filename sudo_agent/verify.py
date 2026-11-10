"""Verification: machine checks that decide whether a task may be reported as done.
The verdict comes from these checks, never from the model's own claim."""
from __future__ import annotations

from pathlib import Path

import psutil

from . import policy
from .tools.base import ToolContext
from .tools.desktop import find_window
from .tools.terminal import run_shell

VERIFIED, UNVERIFIED, FAILED = "verified", "unverified", "failed"


def _normal(text: str) -> str:
    return (text or "").replace("\r\n", "\n")


def _page(ctx: ToolContext):
    return ctx.browser.run(lambda p: (p.url, p.evaluate("() => document.body ? document.body.innerText : ''")))


def _command(check: dict, ctx: ToolContext) -> tuple[int | None, str]:
    decision = policy.check_command(check["command"], "cautious")
    if decision.level != policy.ALLOW:
        raise PermissionError("Verification commands must be read-only.")
    code, output, _ = run_shell(check["command"], "powershell", None, 30, ctx)
    return code, output


def _field(check: dict, ctx: ToolContext) -> str:
    def job(page):
        return ctx.browser.locate(page, check["ref"]).evaluate("el => 'value' in el ? String(el.value) : el.innerText")

    return ctx.browser.run(job)


def run_check(check: dict, ctx: ToolContext) -> tuple[bool | None, str]:
    """Returns (passed or None for a non-machine note, description)."""
    kind = check.get("type", "")
    text = str(check.get("text", ""))
    label = f"{kind} {text or check.get('path') or check.get('command') or ''}".strip()
    if kind == "note":
        return None, f"not machine-checked: {text}"
    if kind in ("file_exists", "file_absent", "file_contains"):
        for_check = policy.check_read(check["path"])
        if for_check.level == policy.BLOCK:
            return False, f"{label}: {for_check.reason}"
        path = Path(check["path"]).expanduser()
        if kind == "file_exists":
            return path.exists(), label
        if kind == "file_absent":
            return not path.exists(), label
        return path.is_file() and _normal(text) in _normal(path.read_text("utf-8", "replace")), label
    if kind == "command_succeeds":
        return _command(check, ctx)[0] == 0, label
    if kind == "command_output_contains":
        return text in _command(check, ctx)[1], label
    if kind == "page_url_contains":
        return text in _page(ctx)[0], label
    if kind == "page_text_contains":
        return _normal(text) in _normal(_page(ctx)[1]), label
    if kind == "field_value_equals":
        return _normal(_field(check, ctx)).rstrip("\n") == _normal(text).rstrip("\n"), label
    if kind == "window_exists":
        return find_window(text) is not None, label
    if kind == "window_absent":
        return find_window(text) is None, label
    if kind == "process_running":
        needle = text.lower()
        return any(needle in (p.info["name"] or "").lower() for p in psutil.process_iter(["name"])), label
    return None, f"not a valid check, so not run: {kind!r}"


def run_checks(checks: list[dict], ctx: ToolContext) -> tuple[str, list[dict]]:
    results = []
    for check in checks:
        try:
            passed, label = run_check(check, ctx)
        except (PermissionError, KeyError) as exc:  # A refused or malformed check is no evidence either way.
            passed, label = None, f"not run: {exc}"
        except Exception as exc:  # noqa: BLE001 - a check that errors has not passed
            passed, label = False, f"{check.get('type')}: {type(exc).__name__}: {exc}"
        results.append({"passed": passed, "label": label})
    if any(r["passed"] is False for r in results):
        return FAILED, results
    if any(r["passed"] is True for r in results):
        if any(r["passed"] is None for r in results):
            return UNVERIFIED, results
        return VERIFIED, results
    return UNVERIFIED, results
