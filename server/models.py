"""
Entity constants and small pure helpers shared by the hub modules.

Field names and status strings follow shared/PROTOCOL.md exactly (§1, §2).
Nothing in here touches I/O or the event loop, so it is trivially testable.
"""
from __future__ import annotations

import re
import secrets
import time
from typing import Any

SERVER_VERSION = "2"

# §2 task statuses
TASK_STATUSES = ("backlog", "todo", "claimed", "in_progress", "review", "done", "failed", "cancelled")
ACTIVE_STATUSES = ("claimed", "in_progress", "review")  # may still be executing on a worker
SATISFIED_DEP_STATUSES = ("done", "cancelled")           # dependency counts as met

# §1 transcript event kinds
EVENT_KINDS = ("system", "user", "assistant", "thinking", "tool_call", "tool_result", "error", "git")

MAX_EVENT_TEXT = 4000      # chars, truncated with "…"
MAX_EVENT_DATA = 8 * 1024  # bytes of JSON; bigger payloads are replaced by a marker
MAX_EVENTS_PER_TASK = 3000
MAX_MESSAGE_TEXT = 4000
LAST_EVENT_CHARS = 200     # task.last_event is a one-line preview


class ApiError(Exception):
    """Raised by hub operations; rendered as {"error": {"code", "message"}}."""

    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message


def now() -> float:
    return time.time()


def new_token() -> str:
    return secrets.token_urlsafe(24)


def truncate(text: Any, limit: int = MAX_EVENT_TEXT) -> str:
    """str(text) cut to `limit` chars, ending with "…" when shortened."""
    s = "" if text is None else str(text)
    return s if len(s) <= limit else s[: limit - 1] + "…"


def one_line(text: Any, limit: int = LAST_EVENT_CHARS) -> str:
    """First non-empty line of `text`, whitespace-collapsed and truncated."""
    for line in ("" if text is None else str(text)).splitlines():
        line = " ".join(line.split())
        if line:
            return truncate(line, limit)
    return ""


def natural_key(key: str) -> tuple:
    """Sort "T2" before "T10"."""
    parts = re.split(r"(\d+)", key or "")
    return tuple(int(p) if p.isdigit() else p for p in parts)


def clamp_int(value: Any, lo: int, hi: int, default: int) -> int:
    try:
        v = int(float(value))
    except (TypeError, ValueError):
        return default
    return max(lo, min(hi, v))


def str_list(value: Any) -> list[str]:
    """Accept a list (or a single string) and return non-empty stripped strings."""
    if value is None:
        return []
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, (list, tuple)):
        return []
    return [str(v).strip() for v in value if v is not None and str(v).strip()]


def default_title(prompt: str) -> str:
    """Server default project title: first 6 words of the prompt."""
    words = prompt.split()
    title = " ".join(words[:6])
    return truncate(title, 120) or "Untitled project"


_REPO_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")


def normalize_repo(repo: Any) -> str:
    """
    "owner/name" (also accepts github URLs / git@ remotes and strips ".git").
    Returns "" for local mode; raises ApiError for garbage.
    """
    r = str(repo or "").strip()
    if not r:
        return ""
    r = re.sub(r"^(https?://)?(www\.)?github\.com[/:]", "", r)
    r = re.sub(r"^git@github\.com:", "", r)
    r = r.rstrip("/")
    if r.endswith(".git"):
        r = r[:-4]
    if not _REPO_RE.match(r):
        raise ApiError(400, "bad_repo", f"repo must look like 'owner/name', got {repo!r}")
    return r


def message_num(message_id: Any) -> int:
    """'m-000042' -> 42; anything unparsable -> 0."""
    m = re.match(r"^m-(\d+)$", str(message_id or "").strip())
    return int(m.group(1)) if m else 0


def message_id(n: int) -> str:
    return f"m-{n:06d}"


def task_number(task_id: str) -> int:
    """'p-3f9a-T12' -> 12 (0 for the PLAN task or anything else)."""
    m = re.search(r"-T(\d+)$", task_id or "")
    return int(m.group(1)) if m else 0


def new_task(
    *,
    task_id: str,
    key: str,
    project_id: str,
    kind: str,
    title: str,
    description: str = "",
    acceptance: list[str] | None = None,
    depends_on: list[str] | None = None,
    priority: int = 2,
    agent_hint: str = "",
    status: str = "todo",
    ts: float | None = None,
) -> dict:
    """A Task object with every §1 field present."""
    ts = now() if ts is None else ts
    return {
        "id": task_id,
        "key": key,
        "project_id": project_id,
        "kind": kind,
        "title": title,
        "description": description,
        "acceptance": list(acceptance or []),
        "depends_on": list(depends_on or []),
        "priority": priority,
        "agent_hint": agent_hint,
        "status": status,
        "assignee": None,
        "progress": 0,
        "progress_note": "",
        "last_event": "",
        "branch": "",
        "pr_url": "",
        "result": "",
        "error": "",
        "attempts": 0,
        "created_at": ts,
        "started_at": None,
        "finished_at": None,
        "updated_at": ts,
        "tokens": {"input": 0, "output": 0},
        "event_count": 0,
    }


def board_entry(task: dict) -> dict:
    """The compact per-task view sent in run_task.board (§3)."""
    return {k: task[k] for k in ("id", "key", "title", "status", "assignee", "depends_on")}


def agent_board_entry(task: dict) -> dict:
    """Compact view for GET /api/agent/board (§4)."""
    keys = ("id", "key", "kind", "title", "status", "assignee", "depends_on", "priority",
            "progress", "progress_note", "branch", "pr_url")
    return {k: task[k] for k in keys}
