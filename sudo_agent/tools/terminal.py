"""Terminal: run PowerShell or cmd with a timeout, live output and a hard stop."""
from __future__ import annotations

import subprocess
import threading
import time

from .. import policy
from ..control import Stopped, kill_process_tree
from .base import ToolContext, ToolResult, tool

LIMIT = 30000


def run_shell(command: str, shell: str, cwd: str | None, timeout: float, ctx: ToolContext) -> tuple[int | None, str, bool]:
    """Returns (exit code, combined output, timed out). Raises Stopped on STOP."""
    if shell == "cmd":
        # Passed as one string: Python would otherwise rewrite " as \", which cmd does not understand.
        argv = f'cmd /d /s /c "{command}"'
    else:
        prefix = "[Console]::OutputEncoding=[Text.Encoding]::UTF8; $ProgressPreference='SilentlyContinue'; "
        argv = ["powershell", "-NoProfile", "-NonInteractive", "-Command", prefix + command]
    proc = subprocess.Popen(
        argv, cwd=cwd or None, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        creationflags=subprocess.CREATE_NO_WINDOW,
    )
    ctx.control.register_process(proc.pid)
    chunks: list[str] = []

    def reader():
        for raw in iter(proc.stdout.readline, b""):
            line = raw.decode("utf-8", "replace")
            chunks.append(line)
            ctx.events.emit("terminal_output", task_id=ctx.task_id, line=line.rstrip()[:500])

    thread = threading.Thread(target=reader, daemon=True)
    thread.start()
    deadline = time.monotonic() + timeout
    timed_out = False
    try:
        while proc.poll() is None:
            if ctx.control.stop_requested:
                kill_process_tree(proc.pid)
                proc.wait(5)
                ctx.control.raise_if_stopped()
            if time.monotonic() > deadline:
                timed_out = True
                kill_process_tree(proc.pid)
                break
            time.sleep(0.05)
        thread.join(2)
    finally:
        ctx.control.unregister_process(proc.pid)
    return proc.poll(), "".join(chunks), timed_out


def _risk(args: dict, ctx: ToolContext) -> policy.Decision:
    return policy.check_command(args["command"], ctx.settings.autonomy)


@tool("run_command", "Run a command and get its output and exit code. Runs non-interactively, so anything waiting for input will time out.",
      {"command": {"type": "string"}, "shell": {"type": "string", "enum": ["powershell", "cmd"]},
       "cwd": {"type": "string"}, "timeout": {"type": "integer", "description": "Seconds, default 120, max 900"}},
      ["command"], group="terminal", risk=_risk, timeout=125, untrusted=True)
def run_command(args, ctx):
    timeout = min(args.get("timeout", 120), 900)
    try:
        code, output, timed_out = run_shell(args["command"], args.get("shell", "powershell"), args.get("cwd"), timeout, ctx)
    except (FileNotFoundError, NotADirectoryError) as exc:
        return ToolResult(f"Could not start: {exc}", ok=False)
    except Stopped:
        raise
    if len(output) > LIMIT:
        output = output[:LIMIT // 2] + f"\n[... {len(output) - LIMIT} characters cut ...]\n" + output[-LIMIT // 2:]
    if timed_out:
        return ToolResult(f"Timed out after {timeout}s and was ended.\n{output}", ok=False)
    return ToolResult(f"Exit code {code}\n{output}", ok=code == 0)
