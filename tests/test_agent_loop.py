"""Runs the real supervisor and task manager with a scripted stand-in for the model."""
import time
from pathlib import Path

from sudo_agent.agent import URGENT


def finish(status="done", summary="All done.", checks=None):
    return ("task_finish", {"status": status, "summary": summary, "checks": checks or []})


def test_plain_question_needs_no_tools(harness):
    h = harness(["Paris."])
    row = h.wait(h.manager.submit("Capital of France?"))
    assert row["status"] == "answered" and row["summary"] == "Paris."


def test_done_is_verified_only_when_checks_pass(harness, tmp_path):
    path = str(tmp_path / "r.txt")
    h = harness([
        [("plan_update", {"steps": [{"text": "write", "status": "doing"}]})],
        [("file_write", {"path": path, "content": "report"})],
        [finish(checks=[{"type": "file_contains", "path": path, "text": "report"}])],
    ])
    row = h.wait(h.manager.submit("Write a report"))
    assert (row["status"], row["verdict"]) == ("done", "verified")
    assert [s["tool"] for s in h.memory.steps(row["id"])] == ["plan_update", "file_write"]


def test_false_claim_is_rejected_and_model_must_continue(harness, tmp_path):
    path = str(tmp_path / "missing.txt")
    h = harness([
        [("file_list", {"path": str(tmp_path)})],
        [finish(checks=[{"type": "file_exists", "path": path}])],
        [("file_write", {"path": path, "content": "now real"})],
        [finish(checks=[{"type": "file_exists", "path": path}])],
    ])
    row = h.wait(h.manager.submit("Make the file"))
    assert (row["status"], row["verdict"]) == ("done", "verified")
    rejected = h.provider.calls[2][-1]["content"][0]
    assert rejected["is_error"] and "Verification failed" in rejected["content"]


def test_persistent_false_claim_ends_as_failed(harness, tmp_path):
    claim = finish(checks=[{"type": "file_exists", "path": str(tmp_path / "never")}])
    h = harness([[("file_list", {"path": str(tmp_path)})], [claim], [claim], [claim]])
    row = h.wait(h.manager.submit("x"))
    assert (row["status"], row["verdict"]) == ("failed", "failed")


def test_malformed_checks_do_not_fail_a_read_only_task(harness, tmp_path):
    h = harness([[("file_list", {"path": str(tmp_path)})],
                 [finish(checks=[{"type": "system_overview"}, {"type": "file_exists"}])]])
    row = h.wait(h.manager.submit("x"))
    assert (row["status"], row["verdict"]) == ("done", "")


def test_malformed_checks_never_count_as_verified(harness, tmp_path):
    path = str(tmp_path / "a.txt")
    bad = finish(checks=[{"type": "system_overview"}])
    h = harness([[("file_write", {"path": path, "content": "x"})], [bad], [bad]])
    assert h.wait(h.manager.submit("x"))["verdict"] == "unverified"


def test_read_only_task_has_nothing_to_verify(harness, tmp_path):
    h = harness([[("file_list", {"path": str(tmp_path)})], [finish()]])
    row = h.wait(h.manager.submit("x"))
    assert (row["status"], row["verdict"]) == ("done", "")


def test_a_change_with_only_a_note_stays_unverified(harness, tmp_path):
    path = str(tmp_path / "a.txt")
    note = finish(checks=[{"type": "note", "text": "looked right"}])
    h = harness([[("file_write", {"path": path, "content": "x"})], [note], [note]])
    assert h.wait(h.manager.submit("x"))["verdict"] == "unverified"


def test_verification_commands_must_be_read_only(harness, tmp_path):
    victim = tmp_path / "v.txt"
    victim.write_text("x")
    claim = finish(checks=[{"type": "command_succeeds", "command": f"Remove-Item '{victim}'"}])
    other = str(tmp_path / "b.txt")
    h = harness([[("file_write", {"path": other, "content": "x"})], [claim]])
    row = h.wait(h.manager.submit("x"))
    assert (row["status"], row["verdict"]) == ("done", "unverified"), "a refused check proves nothing"
    assert victim.exists()


def test_stop_ends_task_and_issues_no_further_actions(harness, tmp_path):
    marker = tmp_path / "after.txt"
    h = harness([
        [("run_command", {"command": "Start-Sleep 30", "timeout": 60})],
        [("file_write", {"path": str(marker), "content": "x"})],
    ])
    task = h.manager.submit("x")
    h.wait_event("tool_start", tool="run_command")
    time.sleep(0.8)
    started = time.monotonic()
    assert h.manager.command("STOP") == {"action": "stopped"}
    row = h.wait(task)
    assert row["status"] == "stopped" and time.monotonic() - started < 3
    assert not marker.exists() and len(h.provider.calls) == 1


def test_stop_clears_the_queue(harness):
    h = harness([[("run_command", {"command": "Start-Sleep 30", "timeout": 60})]])
    first = h.manager.submit("first")
    h.wait_event("tool_start", tool="run_command")
    queued = h.manager.submit("second")
    h.manager.stop()
    h.wait(first)
    assert h.memory.get_task(queued)["status"] == "cancelled"


def test_stop_releases_a_pending_approval(harness, tmp_path):
    target = tmp_path / "a.txt"
    target.write_text("x")
    h = harness([[("file_delete", {"path": str(target)})]])
    task = h.manager.submit("x")
    h.wait_event("prompt", kind="approval")
    h.manager.stop()
    assert h.wait(task)["status"] == "stopped" and target.exists()


def test_pause_holds_actions_until_resume(harness, tmp_path):
    marker = tmp_path / "m.txt"

    def pause_then_write(messages):
        h.manager.pause()
        return [("file_write", {"path": str(marker), "content": "x"})]

    h = harness([pause_then_write, [finish(checks=[{"type": "file_exists", "path": str(marker)}])]])
    task = h.manager.submit("x")
    time.sleep(1.0)
    assert not marker.exists() and h.memory.get_task(task)["status"] == "paused"
    assert h.manager.command("resume") == {"action": "resumed"}
    assert h.wait(task)["verdict"] == "verified"


def test_new_instruction_during_a_task_reaches_the_model(harness, tmp_path):
    def send_update(messages):
        h.manager.command("Use the other folder instead")
        return [("file_list", {"path": str(tmp_path)})]

    h = harness([send_update, ["Understood."]])
    h.wait(h.manager.submit("x"))
    blocks = h.provider.calls[1][-1]["content"]
    assert blocks[0]["type"] == "tool_result"
    assert blocks[-1] == {"type": "text", "text": "[OWNER UPDATE] Use the other folder instead"}


def test_finalize_approval_declined_is_reported_to_model(harness, tmp_path):
    target = tmp_path / "a.txt"
    target.write_text("x")
    h = harness([[("file_delete", {"path": str(target)})], ["Left it alone."]], approve=False)
    h.wait(h.manager.submit("x"))
    result = h.provider.calls[1][-1]["content"][0]
    assert result["is_error"] and "declined" in result["content"][0]["text"] and target.exists()


def test_urgent_task_preempts_and_original_resumes(harness, tmp_path):
    h = harness([
        [("run_command", {"command": "Start-Sleep 20", "timeout": 60})],
        ["Urgent handled."],
        ["Original finished."],
    ])
    first = h.manager.submit("slow job")
    h.wait_event("tool_start", tool="run_command")
    time.sleep(0.5)
    urgent = h.manager.submit("urgent job", URGENT)
    assert h.wait(urgent)["summary"] == "Urgent handled."
    row = h.wait(first)
    assert row["summary"] == "Original finished."
    assert "being resumed" in h.provider.calls[2][0]["content"][0]["text"]


def test_queue_runs_in_order(harness):
    h = harness([["one"], ["two"]])
    a, b = h.manager.submit("a"), h.manager.submit("b")
    assert h.wait(a)["summary"] == "one" and h.wait(b)["summary"] == "two"


def test_repeated_failure_warns_then_aborts(harness, tmp_path):
    same = [("file_read", {"path": str(tmp_path / "nope")})]
    h = harness([same] * 8)
    row = h.wait(h.manager.submit("x"))
    assert row["status"] == "failed" and "kept failing" in row["summary"]
    warned = [b for m in h.provider.calls[-1] if m["role"] == "user" for b in m["content"]
              if b.get("type") == "text" and "failed three times" in b["text"]]
    assert warned


def test_recovers_from_a_failed_tool_with_another_approach(harness, tmp_path):
    good = tmp_path / "ok.txt"
    h = harness([
        [("file_read", {"path": str(tmp_path / "nope.txt")})],
        [("file_write", {"path": str(good), "content": "x"})],
        [finish(checks=[{"type": "file_exists", "path": str(good)}])],
    ])
    assert h.wait(h.manager.submit("x"))["verdict"] == "verified"


def test_step_limit_ends_the_task(harness, tmp_path):
    h = harness([[("file_list", {"path": str(tmp_path)})]] * 30)
    h.settings.max_steps = 4
    row = h.wait(h.manager.submit("x"))
    assert row["status"] == "failed" and "limit" in row["summary"]


def test_crash_recovery_resumes_from_recorded_steps(harness, tmp_path):
    h = harness([["Picked up where it stopped."]])
    task = h.memory.create_task("Long job")
    h.memory.add_step(task, "file_write", {"path": "a"}, True, "Wrote a", 5)
    h.memory.set_task(task, status="running")
    assert h.memory.recover_interrupted() == [task]
    h.manager.resume_task(task)
    assert h.wait(task)["status"] == "answered"
    opening = h.provider.calls[0][0]["content"][0]["text"]
    assert "being resumed" in opening and "Wrote a" in opening


def test_memories_are_given_to_the_model_as_facts(harness):
    harness_memory = harness(["ok"])
    harness_memory.memory.remember("project", "Website repo", "Lives in D:/web")
    harness_memory.wait(harness_memory.manager.submit("Fix the website repo"))
    assert "Lives in D:/web" in harness_memory.provider.calls[0][0]["content"][0]["text"]


def test_missing_key_gives_a_clear_failure(memory):
    from sudo_agent.agent import TaskManager
    from sudo_agent.config import Settings
    from sudo_agent.events import EventBus

    manager = TaskManager(Settings(), memory, EventBus(), None)
    try:
        task = manager.submit("x")
        deadline = time.monotonic() + 5
        while memory.get_task(task)["status"] != "failed" and time.monotonic() < deadline:
            time.sleep(0.02)
        assert "No API key" in memory.get_task(task)["summary"]
    finally:
        manager.close()


def test_internal_tool_exception_does_not_kill_supervisor(harness, tmp_path):
    h = harness([[("file_read", {"path": "\0bad"})], ["Moving on."], ["second task ok"]])
    assert h.wait(h.manager.submit("a"))["status"] in ("done", "failed", "answered")
    assert h.wait(h.manager.submit("b"), timeout=5)["status"] in ("answered", "failed", "done")
    assert Path(tmp_path).exists()
