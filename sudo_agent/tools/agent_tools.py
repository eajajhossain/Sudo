"""Tools the supervisor uses on itself: plan, memory, asking the owner, finishing."""
from __future__ import annotations

from ..memory import KINDS, SecretRejected
from .base import ToolResult, tool


@tool("plan_update", "Record or update the working plan, shown live to the owner. Call it before acting on anything multi-step and when the plan changes.",
      {"steps": {"type": "array", "items": {"type": "object", "properties": {
          "text": {"type": "string"}, "status": {"type": "string", "enum": ["todo", "doing", "done", "failed"]}},
          "required": ["text", "status"]}}},
      ["steps"], group="supervisor", phase="PLAN")
def plan_update(args, ctx):
    ctx.memory.set_task(ctx.task_id, plan=args["steps"])
    ctx.events.emit("plan", task_id=ctx.task_id, steps=args["steps"])
    return ToolResult("Plan saved.")


@tool("ask_owner", "Ask the owner something, or hand over a step only they can do (CAPTCHA, MFA code, login, a decision). Blocks until they reply.",
      {"question": {"type": "string"}}, ["question"], group="supervisor", timeout=86400)
def ask_owner(args, ctx):
    reply = ctx.control.ask("question", {"question": args["question"]})
    return ToolResult(f"Owner replied: {reply}" if reply else "The owner gave no reply.", ok=bool(reply))


@tool("memory_remember", "Save a durable, useful fact for future tasks: a preference, project location, how an error was fixed. Never secrets.",
      {"kind": {"type": "string", "enum": sorted(KINDS)}, "title": {"type": "string"}, "content": {"type": "string"}},
      ["kind", "title", "content"], group="supervisor")
def memory_remember(args, ctx):
    try:
        row = ctx.memory.remember(args["kind"], args["title"], args["content"])
    except SecretRejected as exc:
        return ToolResult(str(exc), ok=False)
    ctx.events.emit("memory_changed")
    return ToolResult(f"Saved as {row['id']}.")


@tool("memory_search", "Search long-term memory.", {"query": {"type": "string"}}, ["query"], group="supervisor", phase="OBSERVE")
def memory_search(args, ctx):
    rows = ctx.memory.search(args["query"])
    return ToolResult("\n".join(f"{r['id']} [{r['kind']}] {r['title']}: {r['content']}" for r in rows) or "(nothing relevant)")


@tool("memory_forget", "Delete a memory that turned out to be wrong.", {"id": {"type": "string"}}, ["id"], group="supervisor")
def memory_forget(args, ctx):
    found = ctx.memory.delete_memory(args["id"])
    ctx.events.emit("memory_changed")
    return ToolResult("Deleted." if found else "No such memory.", ok=found)


@tool("history_search", "Search earlier conversations and tasks by words in the request or result. Returns ids for history_details.",
      {"query": {"type": "string"}}, ["query"], group="supervisor", phase="OBSERVE", untrusted=True)
def history_search(args, ctx):
    rows = ctx.memory.search_tasks(args["query"])
    return ToolResult("\n".join(f"{r['id']} [{r['status']}] {r['objective'][:200]} -> {r['summary'][:200]}" for r in rows) or "(nothing found)")


@tool("history_details", "Read exactly what an earlier task asked for, did and concluded.",
      {"task_id": {"type": "string"}}, ["task_id"], group="supervisor", phase="OBSERVE", untrusted=True)
def history_details(args, ctx):
    row = ctx.memory.get_task(args["task_id"])
    if row is None:
        return ToolResult("No such task.", ok=False)
    steps = "\n".join(f"{'ok ' if s['ok'] else 'ERR'} {s['tool']} {s['args']} -> {s['result'][:300]}" for s in ctx.memory.steps(args["task_id"])[-60:])
    return ToolResult(f"Request: {row['objective']}\nStatus: {row['status']} {row['verdict']}\nResult: {row['summary']}\nSteps:\n{steps or '(none)'}")


@tool("task_finish", (
    "End the task. Put the full answer for the owner in summary. status 'done' claims the objective was achieved. "
    "If you changed something, give checks that prove it. If you only looked things up, leave checks out. "
    "Check types: file_exists{path}, file_absent{path}, file_contains{path,text}, command_succeeds{command}, "
    "command_output_contains{command,text}, page_url_contains{text}, page_text_contains{text}, "
    "field_value_equals{ref,text}, window_exists{text}, window_absent{text}, process_running{text}, "
    "note{text} for anything that cannot be machine-checked (the result is then reported as unverified). "
    "Use status 'blocked' or 'failed' honestly when the objective was not achieved."),
      {"status": {"type": "string", "enum": ["done", "blocked", "failed"]}, "summary": {"type": "string"},
       "checks": {"type": "array", "items": {"type": "object", "properties": {
           "type": {"type": "string", "enum": [
               "file_exists", "file_absent", "file_contains", "command_succeeds", "command_output_contains", "page_url_contains",
               "page_text_contains", "field_value_equals", "window_exists", "window_absent", "process_running", "note"]},
           "path": {"type": "string"}, "text": {"type": "string"}, "command": {"type": "string"}, "ref": {"type": "string"}},
           "required": ["type"]}}},
      ["status", "summary"], group="verification", phase="VERIFY")
def task_finish(args, ctx):  # The agent loop intercepts this tool.
    return ToolResult("Handled by the supervisor.")
