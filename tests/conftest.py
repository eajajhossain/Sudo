import time

import pytest

from sudo_agent import config
from sudo_agent.agent import TaskManager
from sudo_agent.config import Settings
from sudo_agent.control import TaskControl
from sudo_agent.events import EventBus
from sudo_agent.llm import ScriptedProvider
from sudo_agent.memory import Memory
from sudo_agent.tools import ToolContext


TEST_CRED_PREFIX = "SUDO-TEST/"


@pytest.fixture(autouse=True)
def isolated_data(tmp_path, monkeypatch):
    """Tests must never touch the owner's real data or the keys they saved in SUDO."""
    monkeypatch.setenv("SUDO_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setattr(config, "CRED_PREFIX", TEST_CRED_PREFIX)
    for name in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    yield
    for name in ("openai", "anthropic", "extension-pairing"):
        config.delete_key(name)


def test_guard_tests_use_a_separate_credential_area():
    assert config.CRED_PREFIX == TEST_CRED_PREFIX != "SUDO/"


@pytest.fixture
def memory(tmp_path):
    return Memory(tmp_path / "t.db")


class Ctx:
    """A tool context whose approvals answer themselves and are recorded."""

    def __init__(self, memory, browser=None, approve=True):
        self.approve = approve
        self.prompts = []
        self.control = TaskControl(on_prompt=self._on_prompt)
        self.events = EventBus()
        self.memory = memory
        self.settings = Settings()
        self.tool = ToolContext(self.control, self.events, memory, self.settings, browser)

    def _on_prompt(self, prompt):
        self.prompts.append(prompt)
        self.control.answer(prompt["id"], self.approve)


@pytest.fixture
def ctx(memory):
    return Ctx(memory)


class Harness:
    def __init__(self, memory, turns, approve=None):
        self.memory = memory
        self.provider = turns if isinstance(turns, ScriptedProvider) else ScriptedProvider(turns)
        self.events = EventBus()
        self.seen = []
        self.events.subscribe(self.seen.append)
        self.settings = Settings(max_steps=20)
        self.manager = TaskManager(self.settings, memory, self.events, None, lambda s: self.provider)
        if approve is not None:
            self.events.subscribe(self._auto(approve))

    def _auto(self, value):
        def handler(event):
            if event["type"] == "prompt":
                self.manager.answer_prompt(event["id"], value)

        return handler

    def wait(self, task_id, timeout=20, statuses=("answered", "done", "failed", "blocked", "stopped")):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            row = self.memory.get_task(task_id)
            if row["status"] in statuses:
                return row
            time.sleep(0.02)
        raise AssertionError(f"task still {self.memory.get_task(task_id)['status']}")

    def wait_event(self, event_type, timeout=20, **match):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            for event in list(self.seen):
                if event["type"] == event_type and all(event.get(k) == v for k, v in match.items()):
                    return event
            time.sleep(0.02)
        raise AssertionError(f"no event {event_type} {match}")


@pytest.fixture
def harness(memory):
    created = []

    def make(turns, approve=None):
        h = Harness(memory, turns, approve)
        created.append(h)
        return h

    yield make
    for h in created:
        h.manager.close()
