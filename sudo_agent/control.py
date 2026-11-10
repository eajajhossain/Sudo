"""Task control: stop, pause, preempt, live instructions and blocking owner prompts.

Every action path calls checkpoint(), so a STOP takes effect before the next action.
"""
from __future__ import annotations

import threading
import uuid
from collections import deque
from typing import Any, Callable

import psutil


class Stopped(Exception):
    """The owner stopped the task."""


class Preempted(Stopped):
    """A more urgent task took over; this one goes back in the queue."""


def kill_process_tree(pid: int) -> None:
    try:
        parent = psutil.Process(pid)
    except psutil.NoSuchProcess:
        return
    for proc in parent.children(recursive=True) + [parent]:
        try:
            proc.kill()
        except psutil.Error:
            pass


class TaskControl:
    def __init__(self, on_prompt: Callable[[dict], None] | None = None):
        self._stop = threading.Event()
        self._preempt = threading.Event()
        self._running = threading.Event()
        self._running.set()
        self._lock = threading.Lock()
        self._procs: set[int] = set()
        self._prompts: dict[str, dict] = {}
        self._on_prompt = on_prompt or (lambda prompt: None)
        self.inbox: deque[str] = deque()
        self.stop_reason = ""
        self.session_secrets: set[str] = set()

    # -- state ---------------------------------------------------------------
    @property
    def stop_requested(self) -> bool:
        return self._stop.is_set() or self._preempt.is_set()

    @property
    def paused(self) -> bool:
        return not self._running.is_set()

    def stop(self, reason: str = "owner") -> None:
        self.stop_reason = reason
        self._stop.set()
        self._release()

    def preempt(self) -> None:
        self._preempt.set()
        self._release()

    def _release(self) -> None:
        self._running.set()
        with self._lock:
            procs = list(self._procs)
            prompts = list(self._prompts.values())
        for pid in procs:
            kill_process_tree(pid)
        for prompt in prompts:
            prompt["event"].set()

    def pause(self) -> None:
        self._running.clear()

    def resume(self) -> None:
        self._running.set()

    # -- checks --------------------------------------------------------------
    def raise_if_stopped(self) -> None:
        if self._preempt.is_set():
            raise Preempted()
        if self._stop.is_set():
            raise Stopped(self.stop_reason)

    def checkpoint(self) -> None:
        """Called before each action. Blocks while paused, raises when stopped."""
        self.raise_if_stopped()
        while not self._running.wait(0.1):
            self.raise_if_stopped()
        self.raise_if_stopped()

    def sleep(self, seconds: float) -> None:
        if self._stop.wait(seconds) or self._preempt.is_set():
            self.raise_if_stopped()

    # -- child processes -----------------------------------------------------
    def register_process(self, pid: int) -> None:
        with self._lock:
            self._procs.add(pid)
        if self.stop_requested:
            kill_process_tree(pid)

    def unregister_process(self, pid: int) -> None:
        with self._lock:
            self._procs.discard(pid)

    # -- owner prompts (approval / secret / question) ------------------------
    def ask(self, kind: str, payload: dict) -> Any:
        """Block until the owner answers. The answer is never logged here."""
        prompt_id = uuid.uuid4().hex
        event = threading.Event()
        record = {"id": prompt_id, "kind": kind, "payload": payload, "event": event, "value": None}
        with self._lock:
            self._prompts[prompt_id] = record
        self._on_prompt({"id": prompt_id, "kind": kind, "payload": payload})
        try:
            while not event.wait(0.1):
                self.raise_if_stopped()
            self.raise_if_stopped()
            return record["value"]
        finally:
            with self._lock:
                self._prompts.pop(prompt_id, None)

    def answer(self, prompt_id: str, value: Any) -> bool:
        with self._lock:
            record = self._prompts.get(prompt_id)
        if record is None:
            return False
        record["value"] = value
        record["event"].set()
        return True

    def pending_prompts(self) -> list[dict]:
        with self._lock:
            return [
                {"id": p["id"], "kind": p["kind"], "payload": p["payload"]}
                for p in self._prompts.values()
            ]
