"""Tests for continuity, learning, permissions, recovery, documents and OpenAI's Responses API."""
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from docx import Document
from openpyxl import load_workbook
from pypdf import PdfReader, PdfWriter

from conftest import Ctx
from sudo_agent.agent import PAUSE_RE, RESUME_RE, STOP_RE
from sudo_agent.config import Settings, get_key
from sudo_agent.control import TaskControl
from sudo_agent.llm import OpenAICompatibleProvider, OpenAIResponsesProvider, build_provider
from sudo_agent.tools import execute_tool


def run(c, tool_name, /, **args):
    return execute_tool(tool_name, args, c.tool)


# 1: conversation continuity ----------------------------------------------------
def test_follow_up_sees_the_previous_request_and_its_actions(harness, tmp_path):
    path = str(tmp_path / "a.txt")
    h = harness([
        [("file_write", {"path": path, "content": "x"})],
        [("task_finish", {"status": "done", "summary": "Wrote the note.", "checks": [{"type": "file_exists", "path": path}]})],
        ["Doing it again."],
    ])
    h.wait(h.manager.submit("Write a note to a.txt"))
    h.wait(h.manager.submit("now do that again"))
    opening = h.provider.calls[-1][0]["content"][0]["text"]
    assert "Owner: Write a note to a.txt" in opening and "Wrote the note." in opening
    assert "file_write" in opening and "a.txt" in opening


def test_history_tools_find_old_tasks(ctx, memory):
    task = memory.create_task("Fix the printer driver")
    memory.set_task(task, status="done", summary="Reinstalled it.")
    memory.add_step(task, "run_command", {"command": "x"}, True, "done", 1)
    assert task in run(ctx, "history_search", query="printer").text
    assert "Reinstalled it." in run(ctx, "history_details", task_id=task).text


def test_done_after_a_change_must_offer_proof_once(harness, tmp_path):
    path = str(tmp_path / "a.txt")
    h = harness([
        [("file_write", {"path": path, "content": "x"})],
        [("task_finish", {"status": "done", "summary": "Wrote it."})],
        [("task_finish", {"status": "done", "summary": "Wrote it.", "checks": [{"type": "file_exists", "path": path}]})],
    ])
    assert h.wait(h.manager.submit("x"))["verdict"] == "verified"
    assert "prove it" in h.provider.calls[2][-1]["content"][0]["content"]


def test_no_proof_demand_for_read_only_tasks(harness, tmp_path):
    h = harness([[("file_list", {"path": str(tmp_path)})], [("task_finish", {"status": "done", "summary": "Looked."})]])
    assert h.wait(h.manager.submit("x"))["verdict"] == "" and len(h.provider.calls) == 2


def test_repeating_a_successful_call_gets_a_nudge_after_the_results(harness, tmp_path):
    same = [("file_list", {"path": str(tmp_path)}), ("file_list", {"path": str(tmp_path / "..")})]
    h = harness([same, same, same, ["ok"]])
    h.wait(h.manager.submit("x"))
    blocks = h.provider.calls[3][-1]["content"]
    assert [b["type"] for b in blocks][:2] == ["tool_result", "tool_result"]
    assert blocks[-1]["type"] == "text" and "three times" in blocks[-1]["text"]


# 2: chat survives restart -------------------------------------------------------
def test_conversation_is_returned_oldest_first(memory):
    first = memory.create_task("first")
    time.sleep(0.01)
    second = memory.create_task("second")
    memory.set_task(first, status="answered", summary="one")
    assert [r["id"] for r in memory.conversation()] == [first, second]


# 3: automatic learning ----------------------------------------------------------
def test_apps_and_project_folders_are_counted_automatically(harness, tmp_path):
    from sudo_agent.tools.base import REGISTRY, Tool, ToolResult

    REGISTRY["app_open"], saved = Tool("app_open", "", {"name": {"type": "string"}}, ("name",), lambda a, c: ToolResult("ok"),
                                       "computer", 5, None, 0, "EXECUTE", False), REGISTRY["app_open"]
    try:
        h = harness([[("app_open", {"name": "notepad"})], [("app_open", {"name": "notepad"})],
                     [("run_command", {"command": "dir", "cwd": str(tmp_path)})], ["done"], ["done"]])
        h.wait(h.manager.submit("x"))
    finally:
        REGISTRY["app_open"] = saved
    rows = {m["title"]: m["content"] for m in h.memory.list_memories()}
    assert "Opened 2 times" in rows["Uses notepad"]
    assert "Ran commands 1 times" in rows[f"Works in {tmp_path}"]


def test_verified_task_with_an_error_saves_the_fix(harness, tmp_path):
    good = str(tmp_path / "ok.txt")
    h = harness([
        [("run_command", {"command": "python -c \"import nope_mod\""})],
        [("file_write", {"path": good, "content": "x"})],
        [("task_finish", {"status": "done", "summary": "Worked around it.", "checks": [{"type": "file_exists", "path": good}]})],
    ])
    h.wait(h.manager.submit("Make it work"))
    fixes = [m for m in h.memory.list_memories() if m["kind"] == "error_fix"]
    assert len(fixes) == 1 and "nope_mod" in fixes[0]["title"] and "file_write" in fixes[0]["content"]


def test_no_fix_saved_when_owner_only_declined(harness, tmp_path):
    good = tmp_path / "ok.txt"
    good.write_text("x")
    h = harness([
        [("file_delete", {"path": str(good)})],
        [("task_finish", {"status": "done", "summary": "Left it.", "checks": [{"type": "file_exists", "path": str(good)}]})],
    ], approve=False)
    h.wait(h.manager.submit("x"))
    assert not [m for m in h.memory.list_memories() if m["kind"] == "error_fix"]


# 4: documents -------------------------------------------------------------------
def test_word_create_read_edit_across_formatting_runs(ctx, tmp_path):
    path = tmp_path / "a.docx"
    assert run(ctx, "document_create", path=str(path), paragraphs=["Hello world", "Second"]).ok
    assert not run(ctx, "document_create", path=str(path), paragraphs=["x"]).ok
    document = Document(str(path))
    paragraph = document.add_paragraph("")
    paragraph.add_run("split ")
    paragraph.add_run("across").bold = True
    paragraph.add_run(" runs")
    document.save(str(path))
    assert "split across runs" in run(ctx, "document_read", path=str(path)).text
    assert run(ctx, "document_edit", path=str(path), replacements=[{"find": "split across", "replace": "joined"}]).ok
    text = "\n".join(p.text for p in Document(str(path)).paragraphs)
    assert "joined runs" in text and "split" not in text


def test_word_edit_saves_nothing_if_text_missing(ctx, tmp_path):
    path = tmp_path / "a.docx"
    run(ctx, "document_create", path=str(path), paragraphs=["keep"])
    result = run(ctx, "document_edit", path=str(path), replacements=[{"find": "keep", "replace": "x"}, {"find": "absent", "replace": "y"}])
    assert not result.ok and Document(str(path)).paragraphs[0].text == "keep"


def test_excel_create_read_edit_keeps_formulas(ctx, tmp_path):
    path = tmp_path / "a.xlsx"
    assert run(ctx, "document_create", path=str(path), sheets={"Data": [["a", "b"], [1, "=A2+1"]]}).ok
    assert "Data" in run(ctx, "document_read", path=str(path)).text
    assert run(ctx, "document_edit", path=str(path), cells=[{"sheet": "Data", "cell": "A2", "value": 9}]).ok
    sheet = load_workbook(str(path))["Data"]
    assert sheet["A2"].value == 9 and sheet["B2"].value == "=A2+1"


def _blank_pdf(path, pages):
    writer = PdfWriter()
    for _ in range(pages):
        writer.add_blank_page(200, 200)
    with open(path, "wb") as fh:
        writer.write(fh)


def test_pdf_assemble_and_scanned_message(ctx, tmp_path):
    a, b = tmp_path / "a.pdf", tmp_path / "b.pdf"
    _blank_pdf(a, 3)
    _blank_pdf(b, 2)
    out = tmp_path / "c.pdf"
    assert run(ctx, "pdf_assemble", inputs=[{"path": str(a), "pages": "1,3"}, {"path": str(b)}], output=str(out)).ok
    assert len(PdfReader(str(out)).pages) == 4
    scanned = run(ctx, "document_read", path=str(a))
    assert not scanned.ok and "scanned" in scanned.text


def test_document_tools_refuse_credential_files(ctx, tmp_path):
    secret = tmp_path / "keys.pem"
    secret.write_text("x")
    assert "Blocked" in run(ctx, "pdf_assemble", inputs=[{"path": str(secret)}], output=str(tmp_path / "o.pdf")).text


# 6: natural stop ----------------------------------------------------------------
@pytest.mark.parametrize("text", ["STOP", "stop it", "please stop", "Stop now", "no stop", "wait, stop", "cancel everything",
                                  "stop the task", "abort", "don't do that", "stop working", "ok stop please", "bas", "ruko", "band karo"])
def test_stop_phrases(text):
    assert STOP_RE.match(text)


@pytest.mark.parametrize("text", ["stop the docker container", "cancel my 3pm meeting", "end the sentence with a period",
                                  "how do I stop a python thread", "stop spotify and open chrome"])
def test_real_tasks_mentioning_stop_are_not_a_stop(text):
    assert not STOP_RE.match(text)


def test_pause_and_resume_phrases():
    assert PAUSE_RE.match("wait a second") and PAUSE_RE.match("please hold on") and not PAUSE_RE.match("wait for the build")
    assert RESUME_RE.match("go ahead") and RESUME_RE.match("please continue") and not RESUME_RE.match("continue the report in Word")


def test_please_stop_halts_a_running_task(harness):
    h = harness([[("run_command", {"command": "Start-Sleep 30", "timeout": 60})]])
    task = h.manager.submit("x")
    h.wait_event("tool_start", tool="run_command")
    assert h.manager.command("Please stop now!") == {"action": "stopped"}
    assert h.wait(task, timeout=4)["status"] == "stopped"


# 7: recovery view ---------------------------------------------------------------
def test_failed_screen_action_comes_back_with_a_recovery_view(harness):
    from sudo_agent.tools.base import REGISTRY, Tool, ToolResult

    REGISTRY["desktop_click"], saved = Tool("desktop_click", "", {}, (), lambda a, c: ToolResult("Click missed.", ok=False),
                                            "computer", 5, None, 0, "EXECUTE", False), REGISTRY["desktop_click"]
    try:
        h = harness([[("desktop_click", {})], ["Saw it."]])
        h.settings.vision = False
        h.wait(h.manager.submit("x"))
    finally:
        REGISTRY["desktop_click"] = saved
    content = h.provider.calls[1][-1]["content"][0]["content"]
    assert "AUTOMATIC RECOVERY VIEW" in content[1]["text"] and "window_list" in content[2]["text"]


def test_no_recovery_view_after_a_decline(harness, tmp_path):
    from sudo_agent.tools.base import REGISTRY, Tool, ToolResult
    from sudo_agent import policy

    REGISTRY["desktop_click"], saved = Tool("desktop_click", "", {}, (), lambda a, c: ToolResult("x"), "computer", 5,
                                            lambda a, c: policy.confirm("final"), 0, "EXECUTE", False), REGISTRY["desktop_click"]
    try:
        h = harness([[("desktop_click", {})], ["ok"]], approve=False)
        h.wait(h.manager.submit("x"))
    finally:
        REGISTRY["desktop_click"] = saved
    assert len(h.provider.calls[1][-1]["content"][0]["content"]) == 1


# 8: crash recovery --------------------------------------------------------------
def test_interrupted_task_resumes_by_itself(harness):
    h = harness([["Carried on."]])
    task = h.memory.create_task("Long job")
    h.memory.set_task(task, status="running")
    resumable = h.memory.resumable_tasks()
    h.memory.recover_interrupted()
    h.manager.schedule_auto_resume(resumable, delay=0.1)
    assert h.wait(task)["status"] == "answered"


def test_stop_cancels_the_pending_auto_resume(harness):
    h = harness([["Should not run."]])
    task = h.memory.create_task("Long job")
    h.memory.set_task(task, status="running")
    resumable = h.memory.resumable_tasks()
    h.memory.recover_interrupted()
    h.manager.schedule_auto_resume(resumable, delay=0.6)
    h.manager.command("stop")
    time.sleep(1.0)
    assert h.memory.get_task(task)["status"] == "interrupted" and not h.provider.calls


def test_auto_resume_can_be_turned_off(harness):
    h = harness([["no"]])
    h.settings.auto_resume = False
    task = h.memory.create_task("x")
    h.memory.set_task(task, status="running")
    h.manager.schedule_auto_resume(h.memory.resumable_tasks(), delay=0.05)
    time.sleep(0.3)
    assert not h.provider.calls


def test_crash_loop_is_capped_and_old_tasks_stay_put(memory):
    task = memory.create_task("x")
    memory.set_task(task, status="running")
    assert memory.resumable_tasks() == [task]
    memory.note_resume(task)
    memory.note_resume(task)
    assert memory.resumable_tasks() == []
    other = memory.create_task("y")
    memory.set_task(other, status="running")
    assert memory.resumable_tasks(max_age_hours=-1) == []


def test_old_database_is_upgraded(tmp_path):
    import sqlite3

    from sudo_agent.memory import Memory

    path = tmp_path / "old.db"
    db = sqlite3.connect(path)
    db.execute("CREATE TABLE tasks(id TEXT PRIMARY KEY, objective TEXT NOT NULL, status TEXT NOT NULL, verdict TEXT DEFAULT '', summary TEXT DEFAULT '', priority INTEGER DEFAULT 1, plan TEXT DEFAULT '[]', created REAL NOT NULL, updated REAL NOT NULL)")
    db.commit()
    db.close()
    memory = Memory(path)
    assert memory.get_task(memory.create_task("x"))["objective"] == "x"


# 10: permissions ----------------------------------------------------------------
def test_ask_level_asks_before_changes_but_not_for_reading(memory, tmp_path):
    ctx = Ctx(memory, approve=False)
    ctx.settings.permissions["files"] = "ask"
    assert run(ctx, "file_list", path=str(tmp_path)).ok and not ctx.prompts
    assert not run(ctx, "file_write", path=str(tmp_path / "a"), content="x").ok
    assert ctx.prompts and not (tmp_path / "a").exists()


def test_off_level_refuses_everything_in_that_group(memory, tmp_path):
    ctx = Ctx(memory)
    ctx.settings.permissions["files"] = "off"
    for result in (run(ctx, "file_list", path=str(tmp_path)), run(ctx, "file_write", path=str(tmp_path / "a"), content="x")):
        assert not result.ok and "turned off" in result.text
    assert not ctx.prompts


def test_auto_permission_cannot_unblock_a_blocked_command(memory):
    ctx = Ctx(memory)
    ctx.settings.permissions["terminal"] = "auto"
    assert "Blocked" in run(ctx, "run_command", command="netsh advfirewall set allprofiles state off").text


def test_settings_validation_of_new_fields():
    settings = Settings()
    settings.update({"permissions": {**settings.permissions, "files": "ask"}})
    with pytest.raises(ValueError):
        settings.update({"permissions": {"files": "ask"}})
    with pytest.raises(ValueError):
        settings.update({"browser": "netscape"})


def test_switching_provider_changes_the_default_model():
    settings = Settings()
    settings.update({"provider": "openai"})
    assert settings.model == "gpt-6.1-terra"
    settings.update({"provider": "anthropic"})
    assert settings.model == "claude-opus-5-5"


def test_switching_provider_keeps_a_custom_model():
    settings = Settings(model="my-model")
    settings.update({"provider": "openai"})
    assert settings.model == "my-model"


# OpenAI Responses API -----------------------------------------------------------
TOOLS = [{"name": "file_read", "description": "d", "input_schema": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}}]
MESSAGES = [{"role": "user", "content": [{"type": "text", "text": "go"}]}]
RESPONSE = {
    "id": "resp_1", "object": "response", "created_at": 1, "model": "gpt-6.1-terra", "status": "completed", "parallel_tool_calls": True,
    "tool_choice": "auto", "tools": [], "error": None, "incomplete_details": None, "instructions": None, "metadata": {}, "temperature": None, "top_p": None,
    "output": [
        {"id": "rs_1", "type": "reasoning", "summary": [], "encrypted_content": "ENC"},
        {"id": "msg_1", "type": "message", "role": "assistant", "status": "completed", "content": [{"type": "output_text", "text": "Reading", "annotations": []}]},
        {"id": "fc_1", "type": "function_call", "call_id": "call_9", "name": "file_read", "arguments": "{\"path\": \"a.txt\"}", "status": "completed"},
    ],
}


@pytest.fixture
def responses_endpoint():
    state = {"requests": [], "response": RESPONSE}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_POST(self):
            state["requests"].append({"path": self.path, "body": json.loads(self.rfile.read(int(self.headers["content-length"])))})
            events = [
                {"type": "response.output_text.delta", "delta": "Reading", "item_id": "msg_1", "output_index": 1, "content_index": 0, "logprobs": [], "sequence_number": 1},
                {"type": "response.completed", "response": state["response"], "sequence_number": 2},
            ]
            payload = "".join(f"event: {e['type']}\ndata: {json.dumps(e)}\n\n" for e in events).encode()
            self.send_response(200)
            self.send_header("content-type", "text/event-stream")
            self.send_header("content-length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    state["url"] = f"http://127.0.0.1:{server.server_address[1]}/v1"
    yield state
    server.shutdown()


def responses(endpoint):
    return OpenAIResponsesProvider(Settings(provider="openai", model="gpt-6.1-terra"), "k", base_url=endpoint["url"])


def test_responses_request_and_parsing(responses_endpoint):
    texts = []
    response = responses(responses_endpoint).complete("SYS", TOOLS, MESSAGES, TaskControl(), texts.append)
    request = responses_endpoint["requests"][0]
    body = request["body"]
    assert request["path"].endswith("/responses") and body["store"] is False and body["stream"] is True
    assert body["instructions"] == "SYS" and body["model"] == "gpt-6.1-terra"
    assert body["include"] == ["reasoning.encrypted_content"] and body["reasoning"] == {"effort": "high"}
    assert body["tools"][0] == {"type": "function", "name": "file_read", "description": "d", "parameters": TOOLS[0]["input_schema"], "strict": False}
    assert "temperature" not in body and "max_tokens" not in body
    assert texts == ["Reading"] and response.text == "Reading" and response.stop_reason == "tool_use"
    assert response.tool_uses == [{"type": "tool_use", "id": "call_9", "name": "file_read", "input": {"path": "a.txt"}}]
    json.dumps(response.blocks)


def test_gpt_4o_mini_is_sent_without_reasoning_settings(responses_endpoint):
    provider = OpenAIResponsesProvider(Settings(provider="openai", model="gpt-4o-mini"), "k", base_url=responses_endpoint["url"])
    response = provider.complete("SYS", TOOLS, MESSAGES, TaskControl(), lambda t: None)
    body = responses_endpoint["requests"][0]["body"]
    assert body["model"] == "gpt-4o-mini" and "reasoning" not in body and "include" not in body
    assert response.tool_uses[0]["name"] == "file_read"


def test_keeping_a_typed_model_when_choosing_openai():
    settings = Settings()
    settings.update({"provider": "openai", "model": "gpt-4o-mini"})
    assert settings.model == "gpt-4o-mini"


def test_responses_replays_reasoning_unchanged_on_the_next_turn(responses_endpoint):
    provider = responses(responses_endpoint)
    first = provider.complete("S", TOOLS, MESSAGES, TaskControl(), lambda t: None)
    history = MESSAGES + [
        {"role": "assistant", "content": first.blocks},
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "call_9", "content": [{"type": "text", "text": "file body"}]}]},
    ]
    provider.complete("S", TOOLS, history, TaskControl(), lambda t: None)
    sent = responses_endpoint["requests"][1]["body"]["input"]
    assert [i.get("type") or i.get("role") for i in sent] == ["user", "reasoning", "message", "function_call", "function_call_output"]
    assert sent[1]["encrypted_content"] == "ENC" and sent[3]["call_id"] == "call_9"
    assert sent[4] == {"type": "function_call_output", "call_id": "call_9", "output": "file body"}


def test_responses_images_follow_tool_output():
    image = {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": "AAA"}}
    out = OpenAIResponsesProvider.convert([{"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": "a", "content": [{"type": "text", "text": "seen"}, image]},
        {"type": "text", "text": "[OWNER UPDATE] change"}]}])
    assert out[0]["type"] == "function_call_output"
    kinds = {p["type"] for p in out[1]["content"]}
    assert kinds == {"input_image", "input_text"}
    assert any(p.get("image_url") == "data:image/jpeg;base64,AAA" for p in out[1]["content"])


def test_responses_content_filter_is_a_refusal(responses_endpoint):
    responses_endpoint["response"] = {**RESPONSE, "status": "incomplete", "incomplete_details": {"reason": "content_filter"}, "output": []}
    assert responses(responses_endpoint).complete("S", TOOLS, MESSAGES, TaskControl(), lambda t: None).stop_reason == "refusal"


def test_provider_routing(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "k")
    assert isinstance(build_provider(Settings(provider="openai")), OpenAIResponsesProvider)
    assert isinstance(build_provider(Settings(provider="openai", openai_base_url="https://api.openai.com/v1")), OpenAIResponsesProvider)
    assert isinstance(build_provider(Settings(provider="openai", openai_base_url="https://api.groq.com/openai/v1")), OpenAICompatibleProvider)
    assert get_key("openai") == "k"
