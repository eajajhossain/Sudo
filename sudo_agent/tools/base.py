"""Tool registry and the single gate every action passes through:
validate -> policy -> owner approval -> run with timeout and stop -> redact -> record."""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from .. import policy
from ..control import Stopped, TaskControl
from ..events import EventBus
from ..memory import Memory


@dataclass
class ToolResult:
    text: str
    ok: bool = True
    images: list[tuple[str, str]] = field(default_factory=list)  # (media_type, base64)


@dataclass
class ToolContext:
    control: TaskControl
    events: EventBus
    memory: Memory
    settings: Any
    browser: Any
    task_id: str = ""
    allow_private_fetch: bool = False
    bridge: Any = None


@dataclass
class Tool:
    name: str
    description: str
    properties: dict
    required: tuple
    fn: Callable[[dict, ToolContext], ToolResult]
    group: str
    timeout: float
    risk: Callable[[dict, ToolContext], policy.Decision] | None
    retries: int
    phase: str
    untrusted: bool

    def schema(self) -> dict:
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": {
                "type": "object",
                "properties": self.properties,
                "required": list(self.required),
            },
        }


REGISTRY: dict[str, Tool] = {}


def tool(name, description, properties=None, required=(), group="general", timeout=30.0,
         risk=None, retries=0, phase="EXECUTE", untrusted=False):
    def deco(fn):
        REGISTRY[name] = Tool(name, description, properties or {}, tuple(required), fn, group,
                              timeout, risk, retries, phase, untrusted)
        return fn

    return deco


_TYPES = {"string": str, "integer": int, "number": (int, float), "boolean": bool,
          "array": list, "object": dict}


def validate(t: Tool, args: Any) -> str | None:
    if not isinstance(args, dict):
        return "Tool input must be an object."
    for key in t.required:
        if key not in args:
            return f"Missing required field: {key}"
    for key, value in args.items():
        spec = t.properties.get(key)
        if spec is None:
            return f"Unknown field: {key}"
        expected = _TYPES.get(spec.get("type"))
        if expected and not isinstance(value, expected):
            return f"Field {key} must be of type {spec['type']}"
        if expected is int and isinstance(value, bool):
            return f"Field {key} must be of type integer"
        if "enum" in spec and value not in spec["enum"]:
            return f"Field {key} must be one of {spec['enum']}"
    return None


def wrap_untrusted(name: str, text: str) -> str:
    safe = text.replace("</untrusted_content", "<\\/untrusted_content")
    return f'<untrusted_content source="{name}">\n{safe}\n</untrusted_content>'


def _describe_approval(t: Tool, args: dict, reason: str, secrets: set[str]) -> dict:
    return {"tool": t.name, "group": t.group, "args": policy.redact_args(args, secrets), "reason": reason}


def _run_guarded(t: Tool, args: dict, ctx: ToolContext) -> ToolResult:
    """Run one attempt on a worker thread so a hung call cannot hang the agent."""
    box: dict[str, Any] = {}

    def target():
        try:
            box["result"] = t.fn(args, ctx)
        except BaseException as exc:  # noqa: BLE001 - reported to the caller below
            box["error"] = exc

    thread = threading.Thread(target=target, daemon=True, name=f"tool-{t.name}")
    thread.start()
    deadline = time.monotonic() + t_timeout(t, args)
    while thread.is_alive():
        thread.join(0.05)
        if ctx.control.stop_requested:
            thread.join(1.0)
            ctx.control.raise_if_stopped()
        if time.monotonic() > deadline:
            return ToolResult(f"Timed out after {t_timeout(t, args):.0f}s.", ok=False)
    if "error" in box:
        raise box["error"]
    return box["result"]


def t_timeout(t: Tool, args: dict) -> float:
    custom = args.get("timeout")
    if isinstance(custom, (int, float)) and not isinstance(custom, bool):
        return min(float(custom), 900.0) + 5.0
    return t.timeout


# Tools that would show the owner's whole screen or window titles. Unavailable while focus mode is on.
FOCUS_HIDDEN = {"desktop_screenshot", "window_list", "ui_elements", "ui_read_focused"}


def execute_tool(name: str, args: dict, ctx: ToolContext) -> ToolResult:
    """The only way any action runs, for the agent loop and for tests alike."""
    started = time.monotonic()
    t = REGISTRY.get(name)
    if t is None:
        return ToolResult(f"Unknown tool: {name}", ok=False)
    problem = validate(t, args)
    if problem:
        return ToolResult(problem, ok=False)
    if name in FOCUS_HIDDEN and getattr(ctx.bridge, "focus_area", ""):
        return ToolResult("Focus mode is on: the owner allowed you to see only one part of one page, so looking at the whole "
                          "screen or the window list is not allowed. Work with my_snapshot, my_read and the other my_* tools.", ok=False)
    ctx.control.checkpoint()
    secrets = ctx.control.session_secrets
    shown_args = policy.redact_args(args, secrets)
    ctx.events.emit("tool_start", task_id=ctx.task_id, tool=name, group=t.group, phase=t.phase, args=shown_args)

    try:
        decision = t.risk(args, ctx) if t.risk else policy.OK
    except Stopped:
        raise
    except Exception as exc:  # A failed safety check must fail closed.
        decision = policy.confirm(f"SUDO could not assess this action ({exc}).")

    level = getattr(ctx.settings, "permissions", {}).get(t.group, "auto")
    if decision.level != policy.BLOCK:
        if level == "off":
            decision = policy.block(f"The owner turned off {t.group} actions in Settings.")
        elif level == "ask" and decision.level == policy.ALLOW and t.phase == "EXECUTE":
            decision = policy.confirm(f"The owner asked to approve every {t.group} action.")

    if decision.level == policy.BLOCK:
        result = ToolResult(f"Blocked by safety policy: {decision.reason}", ok=False)
    elif decision.level == policy.CONFIRM and not ctx.control.ask(
        "approval", _describe_approval(t, args, decision.reason, secrets)
    ):
        result = ToolResult(
            "The owner declined this action. Do not retry it; continue another way or finish and report.",
            ok=False,
        )
    else:
        result = _attempt(t, args, ctx)

    result.text = policy.redact(result.text, secrets)
    ms = int((time.monotonic() - started) * 1000)
    if ctx.task_id:
        ctx.memory.add_step(ctx.task_id, name, args, result.ok, result.text, ms)
    ctx.events.emit("tool_end", task_id=ctx.task_id, tool=name, ok=result.ok, ms=ms, result=result.text[:600])
    for media_type, data in result.images[-1:]:
        ctx.events.emit("screenshot", task_id=ctx.task_id, tool=name, image=f"data:{media_type};base64,{data}")
    if t.untrusted:
        result.text = wrap_untrusted(name, result.text)
    return result


def _attempt(t: Tool, args: dict, ctx: ToolContext) -> ToolResult:
    last = ToolResult("Tool did not run.", ok=False)
    for attempt in range(t.retries + 1):
        if attempt:
            ctx.control.sleep(0.5 * 2 ** (attempt - 1))
        ctx.control.checkpoint()
        try:
            last = _run_guarded(t, args, ctx)
        except Stopped:
            raise
        except Exception as exc:  # noqa: BLE001 - the model needs the real error to recover
            last = ToolResult(f"{type(exc).__name__}: {exc}", ok=False)
        if last.ok:
            return last
    return last
