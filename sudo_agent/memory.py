"""Persistent memory (SQLite). Working memory for a task lives on the task row and in
the agent loop; long-term memory is the owner-editable `memories` table."""
from __future__ import annotations

import json
import re
import sqlite3
import threading
import time
import uuid
from pathlib import Path

from .policy import contains_secret, redact, redact_args

KINDS = {"preference", "project", "app", "error_fix", "fact"}

_SCHEMA = """
CREATE TABLE IF NOT EXISTS memories(
  id TEXT PRIMARY KEY, kind TEXT NOT NULL, title TEXT NOT NULL, content TEXT NOT NULL,
  created REAL NOT NULL, updated REAL NOT NULL);
CREATE TABLE IF NOT EXISTS tasks(
  id TEXT PRIMARY KEY, objective TEXT NOT NULL, status TEXT NOT NULL, verdict TEXT DEFAULT '',
  summary TEXT DEFAULT '', priority INTEGER DEFAULT 1, plan TEXT DEFAULT '[]',
  created REAL NOT NULL, updated REAL NOT NULL);
CREATE TABLE IF NOT EXISTS steps(
  id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT NOT NULL, tool TEXT NOT NULL, args TEXT NOT NULL,
  ok INTEGER NOT NULL, result TEXT NOT NULL, ms INTEGER NOT NULL, ts REAL NOT NULL);
CREATE INDEX IF NOT EXISTS steps_task ON steps(task_id, id);
"""


class SecretRejected(ValueError):
    pass


class Memory:
    def __init__(self, path: Path):
        self._db = sqlite3.connect(path, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.executescript(_SCHEMA)
        columns = {row["name"] for row in self._db.execute("PRAGMA table_info(tasks)")}
        if "resumes" not in columns:
            self._db.execute("ALTER TABLE tasks ADD COLUMN resumes INTEGER DEFAULT 0")
            self._db.commit()
        self._lock = threading.Lock()

    def _exec(self, sql: str, params=()) -> list[dict]:
        with self._lock:
            cur = self._db.execute(sql, params)
            rows = [dict(r) for r in cur.fetchall()]
            self._db.commit()
            return rows

    # -- long-term -----------------------------------------------------------
    def remember(self, kind: str, title: str, content: str) -> dict:
        if kind not in KINDS:
            raise ValueError(f"kind must be one of {sorted(KINDS)}")
        if contains_secret(title) or contains_secret(content):
            raise SecretRejected("That looks like a password, key or card number, so it was not saved.")
        now = time.time()
        mem_id = uuid.uuid4().hex[:12]
        self._exec(
            "INSERT INTO memories VALUES(?,?,?,?,?,?)", (mem_id, kind, title, content, now, now)
        )
        return self.get_memory(mem_id)

    def get_memory(self, mem_id: str) -> dict | None:
        rows = self._exec("SELECT * FROM memories WHERE id=?", (mem_id,))
        return rows[0] if rows else None

    def list_memories(self) -> list[dict]:
        return self._exec("SELECT * FROM memories ORDER BY updated DESC")

    def update_memory(self, mem_id: str, title: str | None, content: str | None, kind: str | None) -> dict | None:
        row = self.get_memory(mem_id)
        if row is None:
            return None
        title = row["title"] if title is None else title
        content = row["content"] if content is None else content
        kind = row["kind"] if kind is None else kind
        if kind not in KINDS:
            raise ValueError(f"kind must be one of {sorted(KINDS)}")
        if contains_secret(title) or contains_secret(content):
            raise SecretRejected("That looks like a password, key or card number, so it was not saved.")
        self._exec(
            "UPDATE memories SET title=?, content=?, kind=?, updated=? WHERE id=?",
            (title, content, kind, time.time(), mem_id),
        )
        return self.get_memory(mem_id)

    def delete_memory(self, mem_id: str) -> bool:
        exists = self.get_memory(mem_id) is not None
        self._exec("DELETE FROM memories WHERE id=?", (mem_id,))
        return exists

    def search(self, query: str, limit: int = 8) -> list[dict]:
        words = {w for w in re.findall(r"\w{3,}", query.lower())}
        if not words:
            return []
        scored = []
        for row in self.list_memories():
            text = f"{row['title']} {row['content']}".lower()
            score = sum(1 for w in words if w in text)
            if score:
                scored.append((score, row["updated"], row))
        scored.sort(key=lambda s: (s[0], s[1]), reverse=True)
        return [s[2] for s in scored[:limit]]

    # -- learned automatically; the owner can inspect, edit or delete these ----
    def bump_usage(self, kind: str, title: str, verb: str) -> None:
        if contains_secret(title):
            return
        rows = self._exec("SELECT * FROM memories WHERE kind=? AND title=?", (kind, title))
        today = time.strftime("%Y-%m-%d")
        if not rows:
            self.remember(kind, title, f"{verb} 1 times, last on {today}")
            return
        found = re.search(r"(\d+) times", rows[0]["content"])
        count = int(found.group(1)) + 1 if found else 1
        self.update_memory(rows[0]["id"], None, f"{verb} {count} times, last on {today}", None)

    def top_usage(self, kind: str, limit: int = 8) -> list[dict]:
        rows = self._exec("SELECT * FROM memories WHERE kind=?", (kind,))

        def count(row: dict) -> int:
            found = re.search(r"(\d+) times", row["content"])
            return int(found.group(1)) if found else 0

        return sorted(rows, key=count, reverse=True)[:limit]

    def record_fix(self, title: str, content: str) -> None:
        """Save or refresh what fixed an error. Skipped if it would store a secret."""
        rows = self._exec("SELECT id FROM memories WHERE kind='error_fix' AND title=?", (title,))
        try:
            if rows:
                self.update_memory(rows[0]["id"], None, content, None)
            else:
                self.remember("error_fix", title, content)
        except SecretRejected:
            pass

    # -- conversation --------------------------------------------------------
    def conversation(self, limit: int = 40) -> list[dict]:
        rows = self._exec(
            "SELECT id,objective,status,verdict,summary,created FROM tasks WHERE status != 'cancelled' "
            "ORDER BY created DESC LIMIT ?",
            (limit,),
        )
        return list(reversed(rows))

    def search_tasks(self, query: str, limit: int = 10) -> list[dict]:
        words = {w for w in re.findall(r"\w{3,}", query.lower())}
        scored = []
        for row in self.list_tasks(500):
            text = f"{row['objective']} {row['summary']}".lower()
            score = sum(1 for w in words if w in text)
            if score:
                scored.append((score, row["created"], row))
        scored.sort(key=lambda s: (s[0], s[1]), reverse=True)
        return [s[2] for s in scored[:limit]]

    # -- tasks ---------------------------------------------------------------
    def create_task(self, objective: str, priority: int = 1) -> str:
        task_id = uuid.uuid4().hex[:12]
        now = time.time()
        self._exec(
            "INSERT INTO tasks(id,objective,status,priority,created,updated) VALUES(?,?,?,?,?,?)",
            (task_id, redact(objective), "queued", priority, now, now),
        )
        return task_id

    def set_task(self, task_id: str, **fields) -> None:
        allowed = {"status", "verdict", "summary", "plan"}
        parts, values = [], []
        for key, value in fields.items():
            if key not in allowed:
                raise ValueError(key)
            parts.append(f"{key}=?")
            values.append(json.dumps(value) if key == "plan" else redact(value))
        parts.append("updated=?")
        values.extend([time.time(), task_id])
        self._exec(f"UPDATE tasks SET {','.join(parts)} WHERE id=?", values)

    def get_task(self, task_id: str) -> dict | None:
        rows = self._exec("SELECT * FROM tasks WHERE id=?", (task_id,))
        if not rows:
            return None
        rows[0]["plan"] = json.loads(rows[0]["plan"] or "[]")
        return rows[0]

    def list_tasks(self, limit: int = 100) -> list[dict]:
        return self._exec(
            "SELECT id,objective,status,verdict,summary,created,updated FROM tasks ORDER BY created DESC LIMIT ?",
            (limit,),
        )

    def recent_finished(self, limit: int = 5) -> list[dict]:
        return self._exec(
            "SELECT id,objective,status,summary FROM tasks WHERE status NOT IN ('queued','running','paused') "
            "ORDER BY updated DESC LIMIT ?",
            (limit,),
        )

    def delete_task(self, task_id: str) -> None:
        self._exec("DELETE FROM steps WHERE task_id=?", (task_id,))
        self._exec("DELETE FROM tasks WHERE id=?", (task_id,))

    def add_step(self, task_id: str, tool: str, args: dict, ok: bool, result: str, ms: int) -> None:
        self._exec(
            "INSERT INTO steps(task_id,tool,args,ok,result,ms,ts) VALUES(?,?,?,?,?,?,?)",
            (task_id, tool, json.dumps(redact_args(args), ensure_ascii=False)[:4000], int(ok),
             redact(result)[:4000], ms, time.time()),
        )

    def steps(self, task_id: str) -> list[dict]:
        rows = self._exec("SELECT * FROM steps WHERE task_id=? ORDER BY id", (task_id,))
        for row in rows:
            row["args"] = json.loads(row["args"])
            row["ok"] = bool(row["ok"])
        return rows

    def resumable_tasks(self, max_age_hours: float = 6.0, max_resumes: int = 2) -> list[str]:
        """Call before recover_interrupted. The resume cap prevents a crash loop."""
        cutoff = time.time() - max_age_hours * 3600
        rows = self._exec(
            "SELECT id FROM tasks WHERE status IN ('running','paused','queued') AND updated >= ? AND resumes < ? ORDER BY created",
            (cutoff, max_resumes),
        )
        return [r["id"] for r in rows]

    def note_resume(self, task_id: str) -> None:
        self._exec("UPDATE tasks SET resumes = resumes + 1 WHERE id=?", (task_id,))

    def recover_interrupted(self) -> list[str]:
        """After a crash or restart, anything still marked active was interrupted."""
        rows = self._exec("SELECT id FROM tasks WHERE status IN ('running','paused','queued')")
        for row in rows:
            self.set_task(row["id"], status="interrupted", summary="SUDO closed before this task finished.")
        return [r["id"] for r in rows]
