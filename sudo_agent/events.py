"""Structured event stream: feeds the live UI and an on-disk execution log."""
from __future__ import annotations

import json
import threading
import time
from collections import deque
from pathlib import Path
from typing import Callable

from .policy import redact_args


class EventBus:
    def __init__(self, log_dir: Path | None = None, history: int = 400):
        self._subs: list[Callable[[dict], None]] = []
        self._lock = threading.Lock()
        self._seq = 0
        self.history: deque[dict] = deque(maxlen=history)
        self._log_dir = log_dir
        if log_dir:
            log_dir.mkdir(parents=True, exist_ok=True)

    def subscribe(self, fn: Callable[[dict], None]) -> Callable[[], None]:
        with self._lock:
            self._subs.append(fn)

        def unsubscribe() -> None:
            with self._lock:
                if fn in self._subs:
                    self._subs.remove(fn)

        return unsubscribe

    def emit(self, event_type: str, /, **data) -> dict:
        image = data.pop("image", None)
        clean = redact_args(data)
        with self._lock:
            self._seq += 1
            event = {"seq": self._seq, "ts": time.time(), "type": event_type, **clean}
            subs = list(self._subs)
            if event_type != "text_delta":
                self.history.append(event)
                self._write(event)
        outgoing = {**event, "image": image} if image else event
        for fn in subs:
            try:
                fn(outgoing)
            except Exception:
                pass
        return event

    def _write(self, event: dict) -> None:
        if not self._log_dir:
            return
        name = time.strftime("events-%Y%m%d.jsonl")
        try:
            with open(self._log_dir / name, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(event, ensure_ascii=False) + "\n")
        except OSError:
            pass
