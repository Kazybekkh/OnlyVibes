"""
Hub state + persistence.

All mutation happens through hub.Hub while holding its single asyncio lock; this module
only owns the data structures and their (de)serialisation.

On disk (OV_DATA_DIR, default server/data/):
    state.json               projects, tasks, workers, messages, per-task meta (tokens, ...)
    events/<task_id>.json    transcript events of one task (last 3000)

Writes are atomic (write to *.tmp, fsync, rename) and only dirty event files are rewritten,
so a busy transcript does not force re-serialising the whole board every second.
"""
from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path

from models import MAX_EVENTS_PER_TASK, task_number

MAX_MESSAGES = 5000
STATE_VERSION = 2


def _atomic_write(path: Path, text: str) -> None:
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(text)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)


def _safe_name(task_id: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]", "_", task_id)


class Store:
    def __init__(self, data_dir: Path) -> None:
        self.data_dir = Path(data_dir)
        self.projects: dict[str, dict] = {}
        self.tasks: dict[str, dict] = {}
        self.workers: dict[str, dict] = {}
        self.messages: list[dict] = []
        self.msg_counter = 0
        # transcript events per task + next seq
        self.events: dict[str, list[dict]] = {}
        self.event_seq: dict[str, int] = {}
        # per-task server-side bookkeeping that is not part of the Task object:
        # token, agent, claimed_at, hold_until, tokens_base
        self.meta: dict[str, dict] = {}
        self.token_index: dict[str, str] = {}  # bearer token -> task id
        # persistence bookkeeping
        self.dirty = False
        self.dirty_events: set[str] = set()

    # ----------------------------------------------------------------- queries
    def project_tasks(self, project_id: str) -> list[dict]:
        """Tasks of a project: PLAN first, then T1, T2, ... in number order."""
        tasks = [t for t in self.tasks.values() if t["project_id"] == project_id]
        return sorted(tasks, key=lambda t: (t["kind"] != "plan", task_number(t["id"]), t["id"]))

    def all_tasks(self) -> list[dict]:
        out: list[dict] = []
        for pid in sorted(self.projects, key=lambda p: self.projects[p]["created_at"]):
            out.extend(self.project_tasks(pid))
        return out

    def meta_of(self, task_id: str) -> dict:
        return self.meta.setdefault(task_id, {})

    # ------------------------------------------------------------------ tokens
    def set_token(self, task_id: str, token: str) -> None:
        self.revoke_token(task_id)
        self.meta_of(task_id)["token"] = token
        self.token_index[token] = task_id

    def revoke_token(self, task_id: str) -> None:
        old = self.meta_of(task_id).pop("token", None)
        if old:
            self.token_index.pop(old, None)

    # ------------------------------------------------------------------ events
    def append_event(self, task_id: str, event: dict) -> dict:
        seq = self.event_seq.get(task_id, 0) + 1
        self.event_seq[task_id] = seq
        stored = {"seq": seq, **event}
        events = self.events.setdefault(task_id, [])
        events.append(stored)
        if len(events) > MAX_EVENTS_PER_TASK:
            del events[: len(events) - MAX_EVENTS_PER_TASK]
        self.dirty_events.add(task_id)
        return stored

    # ---------------------------------------------------------------- messages
    def append_message(self, message: dict) -> None:
        self.messages.append(message)
        if len(self.messages) > MAX_MESSAGES:
            del self.messages[: len(self.messages) - MAX_MESSAGES]

    # ------------------------------------------------------------- persistence
    def serialize(self) -> tuple[str, dict[str, str]]:
        """Snapshot dirty state as JSON text (call with the hub lock held)."""
        state = {
            "version": STATE_VERSION,
            "saved_at": time.time(),
            "projects": list(self.projects.values()),
            "tasks": list(self.tasks.values()),
            "workers": list(self.workers.values()),
            "messages": self.messages,
            "msg_counter": self.msg_counter,
            "meta": self.meta,
        }
        events = {tid: json.dumps(self.events.get(tid, []), ensure_ascii=False) for tid in self.dirty_events}
        self.dirty = False
        self.dirty_events.clear()
        return json.dumps(state, ensure_ascii=False), events

    def write(self, payload: tuple[str, dict[str, str]]) -> None:
        """Blocking write of a serialize() result (run it in a thread)."""
        state_text, events = payload
        self.data_dir.mkdir(parents=True, exist_ok=True)
        if events:
            ev_dir = self.data_dir / "events"
            ev_dir.mkdir(exist_ok=True)
            for tid, text in events.items():
                _atomic_write(ev_dir / f"{_safe_name(tid)}.json", text)
        _atomic_write(self.data_dir / "state.json", state_text)

    def load(self) -> bool:
        """Load state.json (+ event files). Returns False when starting fresh."""
        path = self.data_dir / "state.json"
        if not path.exists():
            return False
        try:
            state = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            backup = path.with_name(f"state.json.corrupt-{int(time.time())}")
            print(f"[store] cannot read {path} ({exc}); moving it to {backup.name} and starting empty")
            path.replace(backup)
            return False
        self.projects = {p["id"]: p for p in state.get("projects", [])}
        self.tasks = {t["id"]: t for t in state.get("tasks", [])}
        self.workers = {w["id"]: w for w in state.get("workers", [])}
        self.messages = list(state.get("messages", []))
        self.msg_counter = int(state.get("msg_counter", 0))
        self.meta = {k: v for k, v in state.get("meta", {}).items() if k in self.tasks}
        self.token_index = {m["token"]: tid for tid, m in self.meta.items() if m.get("token")}
        ev_dir = self.data_dir / "events"
        for tid in self.tasks:
            f = ev_dir / f"{_safe_name(tid)}.json"
            if not f.exists():
                continue
            try:
                events = json.loads(f.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                print(f"[store] skipping unreadable transcript {f.name}")
                continue
            self.events[tid] = events
            self.event_seq[tid] = max((e.get("seq", 0) for e in events), default=0)
        return True
