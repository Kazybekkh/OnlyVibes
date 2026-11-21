#!/usr/bin/env python3
"""
OnlyVibes reporter — runs on each teammate's laptop.

Detects running AI coding agent sessions (Vibe, Claude Code, Codex, Cursor)
and Claude Code subagents with psutil + their session logs, tails the latest readable line of each agent's log file, and
streams a status message (see shared/schema.json) every INTERVAL seconds to
SERVER_URL over a websocket.

Env vars:
    SERVER_URL   websocket endpoint of the OnlyVibes server
                 (default ws://localhost:8000/ws)
    INTERVAL     seconds between reports (default 1)

Run:
    cd reporter
    uv run reporter.py
    # or against a server on the Tailnet:
    SERVER_URL=ws://100.x.x.x:8000/ws uv run reporter.py
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import signal
import platform
import socket
import subprocess
import time
from getpass import getuser

import psutil
from websockets.sync.client import connect

SERVER_URL = os.environ.get("SERVER_URL", "ws://localhost:8000/ws")
INTERVAL = float(os.environ.get("INTERVAL", "1"))

# Match on the EXECUTABLE name only (basename of cmdline[0], fallback to
# proc.name()) — never a substring of the full cmdline. Exact equality, so
# "Cursor Helper (Renderer)" etc. never match "cursor".
AGENT_EXE_NAMES: dict[str, set[str]] = {
    "vibe": {"vibe", "vibe-rs"},  # vibe-rs = Rust CLI binary the `vibe` shim execs into
    "claude-code": {"claude"},
    "codex": {"codex"},
    "cursor": {"cursor"},  # main Cursor app only; helpers have their own exe names
}

# never report our own stack
EXCLUDE_CMDLINE_SUBSTRINGS = ("onlyvibes", "reporter.py", "mock_reporter", "main.py", "uvicorn")

# candidate log dirs per agent, in PRIORITY order (first dir that yields a
# file wins). Claude Code sessions are one .jsonl per session under
# ~/.claude/projects/**, so that dir comes first.
LOG_DIRS: dict[str, list[str]] = {
    "vibe": ["~/.vibe/logs"],
    "claude-code": ["~/.claude/projects", "~/.claude/logs", "~/.config/claude/logs"],
    "codex": ["~/.codex/logs", "~/.codex/sessions"],
    "cursor": ["~/.cursor/logs", "~/.config/Cursor/logs", "~/Library/Application Support/Cursor/logs"],
}

MAX_LOG_CHARS = 300
SELF_PIDS = {os.getpid(), os.getppid()}

# pid -> psutil.Process, kept across ticks so cpu_percent(None) is meaningful
_PROCS: dict[int, psutil.Process] = {}


# `tailscale ip -4` via the PATH binary, or via the macOS app bundle when the
# CLI is not on PATH (Tailscale.app ships its own CLI at this fixed location)
TAILSCALE_CLI_CANDIDATES = [
    ["tailscale", "ip", "-4"],
    ["/Applications/Tailscale.app/Contents/MacOS/Tailscale", "ip", "-4"],
]


def tailscale_ip() -> str:
    """Best-effort Tailscale IPv4 address of this laptop."""
    for cmd in TAILSCALE_CLI_CANDIDATES:
        try:
            out = subprocess.run(cmd, capture_output=True, text=True, timeout=3)
        except (FileNotFoundError, OSError):
            continue  # not on PATH / not installed here -> try next candidate
        if out.returncode == 0 and out.stdout.strip():
            return out.stdout.strip().split()[0]
    # fallback: local IP used for outbound traffic (on a Tailnet this is the
    # Tailscale IP; otherwise it is the LAN IP — still unique per laptop)
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("8.8.8.8", 80))
            return s.getsockname()[0]
    except Exception:
        return "127.0.0.1"


def exe_name(info: dict) -> str:
    """Executable name used for agent matching.

    basename of cmdline[0] — unless that is a python/node interpreter
    (python, python3, python3.x, node), in which case skip interpreter flags
    (args starting with "-") and use basename of the first real argument,
    i.e. the script/binary being run: `node /usr/local/bin/vibe` -> "vibe",
    `python3 -u -m vibe` -> "vibe". Falls back to proc.name() if no cmdline.
    """
    cmdline = [str(c) for c in (info.get("cmdline") or []) if c]
    if not cmdline:
        return (info.get("name") or "").lower()
    base = os.path.basename(cmdline[0]).lower()
    if base == "node" or base.startswith("python"):
        for arg in cmdline[1:]:
            if arg.startswith("-"):
                continue  # skip interpreter flags (-u, -m, ...)
            return os.path.basename(arg).lower()
    return base


def detect_agents() -> list[dict]:
    """One entry per agent SESSION (plus its running subagents).

    - CLI agents (claude, codex, vibe, cursor-agent): one entry per terminal
      session (process). Wrapper/child processes of the same CLI collapse into
      their topmost matched ancestor.
    - Desktop apps (Claude.app, Cursor.app, Vibe.app, Codex app-server): one
      entry per app (Electron helpers never match), plus Claude/Codex sessions
      whose log was written in the last ACTIVE_WINDOW_S seconds.
    - Claude Code subagents (Task/Agent tool calls still waiting for a result,
      or files under <session>/subagents/) become their own entries with
      parent=<session entry name>, only while the session log is fresh.

    Names are unique and stable between ticks: "<agent> · <project>" (with
    " #2" etc. for duplicates); subagents are "<parent> › <type>".
    """
    now = time.time()
    procs = scan_processes()
    sessions: list[dict] = []
    for fn in (claude_sessions, codex_sessions, cursor_sessions, vibe_sessions):
        try:
            sessions += fn(procs, now)
        except Exception as e:  # one broken source must never kill the reporter
            print(f"[reporter] {fn.__name__} failed: {e}")
    return name_entries(sessions)


# ------------------------------------------------------------ process scan
ACTIVE_WINDOW_S = float(os.environ.get("ACTIVE_WINDOW_S", "120"))
APP_RECENT_S = 30 * 60  # an open app only shows its latest title if used in the last 30 min


def classify(info: dict) -> tuple[str, str] | None:
    """(agent, kind) for a process, kind = "cli" | "app"; None if not an agent."""
    cmd = [str(c) for c in (info.get("cmdline") or []) if c]
    exe = exe_name(info)
    joined = " ".join(cmd).lower()
    first = cmd[0].lower() if cmd else ""
    app = ".app/contents/macos/" in first
    if exe == "claude":
        return ("claude-code", "app" if app else "cli")
    if exe == "codex":
        return ("codex", "app" if "app-server" in cmd else "cli")
    if exe in ("vibe", "vibe-rs"):
        return ("vibe", "app" if app else "cli")
    if exe == "cursor-agent" or (exe in ("agent", "index.js") and "cursor-agent" in joined):
        return ("cursor", "cli")
    if exe == "cursor":
        return ("cursor", "app")
    return None


def scan_processes() -> list[dict]:
    found: dict[int, dict] = {}
    for proc in psutil.process_iter(["pid", "ppid", "name", "cmdline", "create_time"]):
        info = proc.info
        pid = info["pid"]
        if pid in SELF_PIDS:
            continue
        cmdline = " ".join(info["cmdline"] or []).lower()
        if any(s in cmdline for s in EXCLUDE_CMDLINE_SUBSTRINGS):
            continue
        c = classify(info)
        if not c:
            continue
        p = _PROCS.setdefault(pid, proc)
        try:
            cpu = round(p.cpu_percent(None), 1)
        except Exception:
            cpu = 0.0
        try:
            cwd = p.cwd()
        except Exception:
            cwd = ""
        found[pid] = {"agent": c[0], "kind": c[1], "pid": pid, "ppid": info.get("ppid") or 0,
                      "cpu": cpu, "cwd": cwd, "ctime": info.get("create_time") or 0.0}
    for pid in [k for k in _PROCS if k not in found]:
        _PROCS.pop(pid, None)
    # collapse wrapper -> child chains of the same CLI agent (e.g. node shim + binary)
    out = []
    for r in found.values():
        parent = found.get(r["ppid"])
        if r["kind"] == "cli" and parent and parent["agent"] == r["agent"] and parent["kind"] == "cli":
            continue
        out.append(r)
    out.sort(key=lambda r: (r["ctime"], r["pid"]))
    return out


def _mtime(path: str) -> float:
    try:
        return os.path.getmtime(path)
    except OSError:
        return 0.0


def short(text: str, n: int = 60) -> str:
    text = " ".join(str(text or "").split())
    if len(text) <= n:
        return text
    cut = text[: n - 1].rsplit(" ", 1)[0]
    return (cut or text[: n - 1]) + "…"


def _entry(agent: str, kind: str, label: str, task: str, *, pid=0, cpu=0.0, cwd="",
           last_log="", sort=0.0, subs=None, ctime=0.0, log="") -> dict:
    return {"agent": agent, "kind": kind, "label": label, "task": task, "pid": pid, "cpu": cpu,
            "cwd": cwd, "last_log": last_log, "sort": sort, "subs": subs or [], "ctime": ctime, "log": log}


def name_entries(sessions: list[dict]) -> list[dict]:
    """Assign unique, tick-stable names and flatten subagents into entries."""
    order = {"app": 0, "cli": 1, "log": 2}
    sessions.sort(key=lambda e: (e["agent"], order.get(e["kind"], 3), e["sort"]))
    used: set[str] = set()

    def unique(base: str) -> str:
        name, n = base, 2
        while name in used:
            name, n = f"{base} #{n}", n + 1
        used.add(name)
        return name

    out = []
    control: dict[str, dict] = {}
    for e in sessions:
        base = e["agent"] if e["kind"] == "app" or not e["label"] else f"{e['agent']} · {short(e['label'], 28)}"
        name = unique(base)
        info = {"agent": e["agent"], "kind": "app" if e["kind"] == "app" else "session", "pid": int(e["pid"] or 0),
                "ctime": e["ctime"], "cwd": e["cwd"] or "", "log": e["log"] or ""}
        control[name] = info
        out.append({"name": name, "kind": "app" if e["kind"] == "app" else "session",
                    "pid": int(e["pid"] or 0), "cpu": float(e["cpu"] or 0.0), "cwd": e["cwd"] or "",
                    "last_log": (e["last_log"] or "")[:MAX_LOG_CHARS], "task": short(e["task"]),
                    "summary": ""})
        for sub_label, sub_task in e["subs"]:
            sub_name = unique(f"{name} › {short(sub_label, 24) or 'subagent'}")
            control[sub_name] = {**info, "kind": "subagent", "parent": name}
            out.append({"name": sub_name,
                        "kind": "subagent", "parent": name, "pid": 0, "cpu": 0.0, "cwd": "",
                        "last_log": "", "task": short(sub_task), "summary": ""})
    _CONTROL.clear()
    _CONTROL.update(control)
    return out


# ------------------------------------------------------------- Claude Code
CLAUDE_PROJECTS = os.path.expanduser("~/.claude/projects")
SUBAGENT_TOOLS = {"Task", "Agent"}


class ClaudeLog:
    """Incrementally parsed Claude Code session .jsonl (only new bytes each tick)."""

    def __init__(self, path: str):
        self.path = path
        self.offset = 0
        self.partial = b""
        self.custom = self.ai_title = self.summary = self.first_prompt = ""
        self.cwd = self.last_log = self.side_prompt = ""
        self.pending: dict[str, tuple[str, str]] = {}  # tool_use_id -> (type, description)

    def title(self) -> str:
        if not self.custom:
            try:  # newer Claude Code keeps renames next to the log
                with open(self.path[:-6] + "/custom-title.json") as fh:
                    self.custom = str(json.load(fh).get("customTitle") or "")
            except Exception:
                pass
        return self.custom or self.ai_title or self.summary or self.first_prompt

    def update(self) -> "ClaudeLog":
        try:
            size = os.path.getsize(self.path)
        except OSError:
            return self
        if size < self.offset:  # truncated/rewritten: start over
            self.__init__(self.path)
        if size == self.offset:
            return self
        with open(self.path, "rb") as fh:
            fh.seek(self.offset)
            data = self.partial + fh.read(size - self.offset)
        self.offset = size
        lines = data.split(b"\n")
        self.partial = lines.pop()
        for raw in lines:
            if raw.strip():
                self._line(raw.decode("utf-8", "ignore"))
        return self

    def _line(self, ln: str) -> None:
        try:
            obj = json.loads(ln)
        except json.JSONDecodeError:
            return
        if not isinstance(obj, dict):
            return
        t = obj.get("type")
        if t == "custom-title" and obj.get("customTitle"):
            self.custom = str(obj["customTitle"])
        elif t == "ai-title" and obj.get("aiTitle"):
            self.ai_title = str(obj["aiTitle"])
        elif t == "summary" and obj.get("summary"):
            self.summary = str(obj["summary"])
        if obj.get("cwd") and not obj.get("isSidechain"):
            self.cwd = str(obj["cwd"])
        msg = obj.get("message")
        if not isinstance(msg, dict):
            return
        content = msg.get("content")
        if isinstance(content, list):
            for b in content:
                if not isinstance(b, dict):
                    continue
                if b.get("type") == "tool_use" and b.get("name") in SUBAGENT_TOOLS:
                    inp = b.get("input") if isinstance(b.get("input"), dict) else {}
                    desc = inp.get("description") or (str(inp.get("prompt") or "").strip().splitlines() or [""])[0]
                    self.pending[str(b.get("id"))] = (str(inp.get("subagent_type") or "subagent"), str(desc))
                elif b.get("type") == "tool_result" and b.get("tool_use_id"):
                    self.pending.pop(str(b["tool_use_id"]), None)
        if obj.get("isSidechain"):
            if t == "user" and not self.side_prompt and isinstance(content, str) and content.strip():
                self.side_prompt = content.strip().splitlines()[0]  # subagent's brief
            return
        if t in ("user", "assistant"):
            if t == "user" and not self.first_prompt:
                c = content
                if isinstance(c, list):
                    c = " ".join(x.get("text", "") for x in c if isinstance(x, dict) and x.get("type") == "text")
                if isinstance(c, str) and c.strip() and not c.lstrip().startswith("<"):
                    self.first_prompt = c.strip().splitlines()[0]
            text = readable_from_jsonl_line(ln)
            if text and text != ln:
                self.last_log = text.strip().splitlines()[0] if text.strip() else ""


_CLAUDE_LOGS: dict[str, ClaudeLog] = {}


def claude_log(path: str) -> ClaudeLog:
    log = _CLAUDE_LOGS.get(path)
    if log is None:
        log = _CLAUDE_LOGS[path] = ClaudeLog(path)
    return log.update()


def claude_project_dir(cwd: str) -> str:
    return os.path.join(CLAUDE_PROJECTS, re.sub(r"[^A-Za-z0-9]", "-", cwd))


def _jsonls(d: str) -> list[str]:
    try:
        files = [os.path.join(d, f) for f in os.listdir(d) if f.endswith(".jsonl")]
    except OSError:
        return []
    return sorted(files, key=_mtime, reverse=True)


def claude_subagents(path: str, log: ClaudeLog, now: float) -> list[tuple[str, str]]:
    """Running subagents of one session, only while its logs are fresh."""
    sub_dir = os.path.join(path[:-6], "subagents")
    sub_files = [f for f in _jsonls(sub_dir) if now - _mtime(f) < ACTIVE_WINDOW_S]
    fresh = now - _mtime(path) < ACTIVE_WINDOW_S or bool(sub_files)
    if not fresh:
        return []
    if log.pending:
        return list(log.pending.values())
    subs = []
    for f in sub_files:  # subagent transcripts without a matching pending call
        sl = claude_log(f)
        subs.append(("subagent", sl.ai_title or sl.side_prompt or sl.first_prompt))
    return subs


_CLAUDE_PID_LOG: dict[tuple[int, float], str] = {}  # (pid, start time) -> its session log (sticky)


def pick_claude_log(r: dict, used: set[str]) -> str | None:
    """Session log for a claude process: the first log created after the process
    started (its own session), else the newest unclaimed log in that project."""
    if not r["cwd"]:
        return None
    files = [f for f in _jsonls(claude_project_dir(r["cwd"])) if f not in used]
    sticky = set(_CLAUDE_PID_LOG.values())
    after = sorted((f for f in files if _birth(f) >= r["ctime"] - 5 and f not in sticky), key=_birth)
    return after[0] if after else (files[0] if files else None)


def claude_sessions(procs: list[dict], now: float) -> list[dict]:
    out: list[dict] = []
    used: set[str] = set()
    live = {(r["pid"], r["ctime"]) for r in procs}
    for k in [k for k in _CLAUDE_PID_LOG if k not in live]:
        del _CLAUDE_PID_LOG[k]
    for r in (r for r in procs if r["agent"] == "claude-code" and r["kind"] == "cli"):
        path = _CLAUDE_PID_LOG.get((r["pid"], r["ctime"]))
        if not path or path in used or not os.path.exists(path):
            path = pick_claude_log(r, used)
            if path:
                _CLAUDE_PID_LOG[(r["pid"], r["ctime"])] = path
        log = None
        if path:
            used.add(path)
            log = claude_log(path)
        out.append(_entry("claude-code", "cli", project_name(r["cwd"]),
                          (log.title() if log else "") or project_name(r["cwd"]),
                          pid=r["pid"], cpu=r["cpu"], cwd=r["cwd"], last_log=log.last_log if log else "",
                          sort=r["ctime"], subs=claude_subagents(path, log, now) if log else [],
                          ctime=r["ctime"], log=path or ""))
    # sessions with a fresh log but no matching CLI process (Claude desktop app, ssh, ...)
    newest, newest_m = None, 0.0
    try:
        dirs = [os.path.join(CLAUDE_PROJECTS, d) for d in os.listdir(CLAUDE_PROJECTS)]
    except OSError:
        dirs = []
    for d in dirs:
        for f in _jsonls(d)[:3]:
            m = _mtime(f)
            if m > newest_m:
                newest, newest_m = f, m
            if f in used or now - m >= ACTIVE_WINDOW_S:
                continue
            used.add(f)
            log = claude_log(f)
            out.append(_entry("claude-code", "log", project_name(log.cwd) or short(log.title(), 24),
                              log.title() or project_name(log.cwd), cwd=log.cwd, last_log=log.last_log,
                              sort=_birth(f), subs=claude_subagents(f, log, now), log=f))
    apps = [r for r in procs if r["agent"] == "claude-code" and r["kind"] == "app"]
    if apps and not out:
        task = claude_log(newest).title() if newest and now - newest_m < APP_RECENT_S else "idle"
        out.append(_entry("claude-code", "app", "", task, pid=apps[0]["pid"], cpu=apps[0]["cpu"]))
    return out


def _birth(path: str) -> float:
    try:
        st = os.stat(path)
        return getattr(st, "st_birthtime", st.st_ctime)
    except OSError:
        return 0.0


# ------------------------------------------------------------------- Codex
CODEX_HOME = os.path.expanduser("~/.codex")
_codex_index: tuple[float, dict[str, tuple[str, str]]] = (0.0, {})


def codex_threads() -> dict[str, tuple[str, str]]:
    """thread id -> (thread_name, updated_at) from ~/.codex/session_index.jsonl."""
    global _codex_index
    path = os.path.join(CODEX_HOME, "session_index.jsonl")
    m = _mtime(path)
    if m and m != _codex_index[0]:
        idx = {}
        try:
            with open(path, errors="ignore") as fh:
                for ln in fh:
                    try:
                        o = json.loads(ln)
                    except json.JSONDecodeError:
                        continue
                    if isinstance(o, dict) and o.get("id"):
                        idx[str(o["id"])] = (str(o.get("thread_name") or ""), str(o.get("updated_at") or ""))
        except OSError:
            pass
        _codex_index = (m, idx)
    return _codex_index[1]


def codex_active_rollouts(now: float) -> list[tuple[float, str]]:
    """(mtime, thread_id) of rollout files written in the active window (today/yesterday dirs)."""
    out = []
    for day in (now, now - 86400):
        d = os.path.join(CODEX_HOME, "sessions", time.strftime("%Y/%m/%d", time.localtime(day)))
        try:
            names = os.listdir(d)
        except OSError:
            continue
        for f in names:
            mt = re.search(r"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})\.jsonl", f)
            m = _mtime(os.path.join(d, f))
            if mt and now - m < ACTIVE_WINDOW_S:
                out.append((m, mt.group(1)))
    return sorted(out, reverse=True)


def codex_sessions(procs: list[dict], now: float) -> list[dict]:
    idx = codex_threads()
    active = codex_active_rollouts(now)
    out = []
    for r in (r for r in procs if r["agent"] == "codex" and r["kind"] == "cli"):
        title = ""
        if active:
            title = idx.get(active.pop(0)[1], ("", ""))[0]
        out.append(_entry("codex", "cli", project_name(r["cwd"]), title or project_name(r["cwd"]),
                          pid=r["pid"], cpu=r["cpu"], cwd=r["cwd"], last_log=title, sort=r["ctime"],
                          ctime=r["ctime"]))
    for m, tid in active:  # threads driven by the Codex app / IDE extension
        title = idx.get(tid, ("", ""))[0]
        out.append(_entry("codex", "log", short(title, 24) or "thread", title, last_log=title, sort=m))
    apps = [r for r in procs if r["agent"] == "codex" and r["kind"] == "app"]
    if apps and not out:
        newest = max(idx.values(), key=lambda v: v[1], default=("", ""))
        recent = _mtime(os.path.join(CODEX_HOME, "session_index.jsonl"))
        task = newest[0] if newest[0] and now - recent < APP_RECENT_S else "idle"
        out.append(_entry("codex", "app", "", task, pid=apps[0]["pid"], cpu=apps[0]["cpu"]))
    return out


# ------------------------------------------------------------------ Cursor
CURSOR_CHATS = os.path.expanduser("~/.cursor/chats")


def cursor_chat_title(cwd: str) -> str:
    """Newest cursor-agent chat title for a working dir (~/.cursor/chats/<md5(cwd)>/*/meta.json)."""
    if not cwd:
        return ""
    d = os.path.join(CURSOR_CHATS, hashlib.md5(cwd.encode()).hexdigest())
    best, best_t = "", -1.0
    try:
        chats = os.listdir(d)
    except OSError:
        return ""
    for c in chats:
        try:
            with open(os.path.join(d, c, "meta.json")) as fh:
                meta = json.load(fh)
        except Exception:
            continue
        t = float(meta.get("updatedAtMs") or 0)
        if meta.get("title") and t > best_t:
            best, best_t = str(meta["title"]), t
    return best


def cursor_sessions(procs: list[dict], now: float) -> list[dict]:
    out = []
    for r in (r for r in procs if r["agent"] == "cursor" and r["kind"] == "cli"):
        out.append(_entry("cursor", "cli", project_name(r["cwd"]),
                          cursor_chat_title(r["cwd"]) or project_name(r["cwd"]),
                          pid=r["pid"], cpu=r["cpu"], cwd=r["cwd"], sort=r["ctime"],
                          ctime=r["ctime"]))
    apps = [r for r in procs if r["agent"] == "cursor" and r["kind"] == "app"]
    if apps:
        a = max(apps, key=lambda r: r["cpu"])
        out.append(_entry("cursor", "app", "", "", pid=a["pid"], cpu=a["cpu"], last_log=tail_log("cursor")))
    return out


# -------------------------------------------------------------------- Vibe
VIBE_DESKTOP_STORE = os.path.expanduser("~/Library/Application Support/le-chat-desktop/local-synced-store.json")
_vibe_prompt: tuple[float, str] = (0.0, "")


def vibe_app_task() -> str:
    """Latest prompt typed into Vibe desktop's code composer (newest is last)."""
    global _vibe_prompt
    m = _mtime(VIBE_DESKTOP_STORE)
    if m and m != _vibe_prompt[0]:
        text = ""
        try:
            with open(VIBE_DESKTOP_STORE) as fh:
                vals = json.load(fh).get("values", {})
            for p in reversed(vals.get("composer.recent-prompts", {}).get("code", {}).get("prompts", [])):
                t = " ".join(b.get("content", "") for b in p if isinstance(b, dict) and b.get("type") == "text").strip()
                if len(t.split()) >= 3:
                    text = t.splitlines()[0]
                    break
        except Exception:
            pass
        _vibe_prompt = (m, text)
    return _vibe_prompt[1]


def vibe_sessions(procs: list[dict], now: float) -> list[dict]:
    out = []
    for r in (r for r in procs if r["agent"] == "vibe" and r["kind"] == "cli"):
        out.append(_entry("vibe", "cli", project_name(r["cwd"]), project_name(r["cwd"]),
                          pid=r["pid"], cpu=r["cpu"], cwd=r["cwd"], sort=r["ctime"],
                          ctime=r["ctime"]))
    apps = [r for r in procs if r["agent"] == "vibe" and r["kind"] == "app"]
    if apps:
        a = max(apps, key=lambda r: r["cpu"])
        out.append(_entry("vibe", "app", "", vibe_app_task(), pid=a["pid"], cpu=a["cpu"]))
    return out


def _newest_log_file(d: str) -> str | None:
    """Most recently modified log file under dir d (recursive), or None."""
    best_file, best_mtime = None, 0.0
    for root, _dirs, files in os.walk(d):
        for f in files:
            if not f.endswith((".log", ".jsonl", ".txt")):
                continue
            path = os.path.join(root, f)
            try:
                m = os.path.getmtime(path)
            except OSError:
                continue
            if m > best_mtime:
                best_file, best_mtime = path, m
    return best_file


def readable_from_jsonl_line(line: str) -> str:
    """Reduce one Claude Code session .jsonl line to readable text.

    Prefers message content blocks (assistant text, tool_use name +
    command/file), then common top-level text fields; falls back to raw.
    """
    try:
        obj = json.loads(line)
    except json.JSONDecodeError:
        return line
    if not isinstance(obj, dict):
        return line
    msg = obj.get("message")
    if isinstance(msg, dict):
        content = msg.get("content")
        if isinstance(content, str) and content.strip():
            return content.strip()
        if isinstance(content, list):
            parts = []
            for block in content:
                if not isinstance(block, dict):
                    continue
                if block.get("type") == "text" and block.get("text"):
                    parts.append(str(block["text"]).strip())
                elif block.get("type") == "tool_use":
                    tool = block.get("name", "tool")
                    inp = block.get("input") if isinstance(block.get("input"), dict) else {}
                    detail = inp.get("command") or inp.get("file_path") or inp.get("description") or ""
                    parts.append(f"[{tool}] {detail}".strip())
                elif block.get("type") == "tool_result":
                    c = block.get("content")
                    if isinstance(c, str) and c.strip():
                        parts.append(c.strip())
                    elif isinstance(c, list):
                        for cb in c:
                            if isinstance(cb, dict) and cb.get("type") == "text" and cb.get("text"):
                                parts.append(str(cb["text"]).strip())
            if parts:
                return " | ".join(parts)
    for key in ("summary", "text", "command", "tool_name", "name"):
        v = obj.get(key)
        if isinstance(v, str) and v.strip():
            return v.strip()
    return line


def tail_log(agent: str) -> str:
    """Latest readable line of the agent's most recently modified log file.

    Log dirs are tried in priority order. .jsonl files (Claude Code
    sessions under ~/.claude/projects/**) are parsed as JSON and reduced
    to a readable text field instead of sending raw JSON.
    """
    for d in LOG_DIRS.get(agent, []):
        d = os.path.expanduser(d)
        if not os.path.isdir(d):
            continue
        path = _newest_log_file(d)
        if not path:
            continue
        try:
            with open(path, "r", errors="ignore") as fh:
                lines = [ln.strip() for ln in fh if ln.strip()]
        except OSError:
            continue
        if not lines:
            continue
        last = lines[-1]
        if path.endswith(".jsonl"):
            last = readable_from_jsonl_line(last)
        return last[:MAX_LOG_CHARS]
    return ""


def project_name(cwd: str) -> str:
    """Fallback task: the project folder the agent runs in (e.g. "OnlyVibes")."""
    if not cwd or cwd in ("/", os.path.expanduser("~")):
        return ""
    return os.path.basename(cwd.rstrip("/"))


# ------------------------------------------------------- remote control (AR)
# The hub forwards {"type":"control","id","agent":<card name>,"action","text"} down this
# reporter's /ws socket. Only a fixed whitelist runs, only on agent processes this
# reporter itself detected, resolved by card name HERE (pids never leave the laptop
# as control targets). No shell, no command strings from the network.
_CONTROL: dict[str, dict] = {}  # card name -> {agent, kind, pid, ctime, cwd, log}
CLAUDE_BIN_CANDIDATES = ["~/.local/bin/claude", "/opt/homebrew/bin/claude", "/usr/local/bin/claude"]
CONTROL_LOG = "/tmp/onlyvibes-control.log"


def _live_agent_proc(info: dict) -> psutil.Process | None:
    """The card's process if it is still the same agent process (guards against pid reuse)."""
    pid = info.get("pid") or 0
    if not pid:
        return None
    try:
        p = psutil.Process(pid)
        if info.get("ctime") and abs(p.create_time() - info["ctime"]) > 1:
            return None
        c = classify({"pid": pid, "name": p.name(), "cmdline": p.cmdline()})
    except (psutil.Error, OSError):
        return None
    return p if c and c[0] == info["agent"] and c[1] == "cli" else None


def handle_control(msg: dict) -> dict:
    agent = str(msg.get("agent") or "")
    action = str(msg.get("action") or "")
    text = " ".join(str(msg.get("text") or "").split())[:500]
    info = _CONTROL.get(agent)
    if info is None:
        return {"ok": False, "message": f"{agent or 'that agent'} is not running on this laptop any more"}
    if info["kind"] == "app":
        return {"ok": False, "message": "desktop apps are view-only from AR; control them in the app"}
    proc = _live_agent_proc(info)
    if action == "stop":
        if proc is None:
            return {"ok": False, "message": "no running terminal process for this session"}
        os.kill(proc.pid, signal.SIGINT)  # = Ctrl-C in its terminal
        what = "its parent session" if info["kind"] == "subagent" else agent
        print(f"[reporter] control: SIGINT -> {agent}")
        return {"ok": True, "message": f"sent Ctrl-C to {what}"}
    if action in ("resume", "say"):
        if info["agent"] != "claude-code" or not info.get("log"):
            return {"ok": False, "message": f"{action} is only supported for Claude Code sessions (type in its terminal)"}
        if proc is not None:
            return {"ok": False, "message": "this session is open in a terminal: type there, or Stop it first and then Resume here"}
        claude = shutil.which("claude") or next(
            (os.path.expanduser(c) for c in CLAUDE_BIN_CANDIDATES if os.path.isfile(os.path.expanduser(c))), None)
        if not claude:
            return {"ok": False, "message": "claude CLI not found on this laptop"}
        session_id = os.path.basename(info["log"])[:-6]
        if not re.fullmatch(r"[0-9a-fA-F-]{8,64}", session_id):
            return {"ok": False, "message": "unrecognised session id"}
        prompt = text or "continue"
        cwd = info["cwd"] if info["cwd"] and os.path.isdir(info["cwd"]) else os.path.expanduser("~")
        with open(CONTROL_LOG, "ab") as out:
            subprocess.Popen([claude, "-p", "--resume", session_id, prompt], cwd=cwd, stdin=subprocess.DEVNULL,
                             stdout=out, stderr=subprocess.STDOUT, start_new_session=True)
        print(f"[reporter] control: resumed {agent} headless")
        return {"ok": True, "message": f"resumed {agent} headless with: {prompt[:60]}"}
    return {"ok": False, "message": f"unknown action {action!r}"}


def _drain_controls(ws, seconds: float) -> None:
    """Wait up to `seconds` for control frames from the hub and answer each one."""
    deadline = time.time() + seconds
    while (remaining := deadline - time.time()) > 0:
        try:
            raw = ws.recv(timeout=remaining)
        except TimeoutError:
            return
        try:
            msg = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            continue
        if not isinstance(msg, dict) or msg.get("type") != "control":
            continue
        try:
            res = handle_control(msg)
        except Exception as e:  # never let a control request kill the reporter
            res = {"ok": False, "message": f"control failed: {e}"}
        ws.send(json.dumps({"type": "control_result", "id": msg.get("id"), **res}))


def build_status() -> dict:
    """One message conforming to shared/schema.json."""
    return {
        "laptop": platform.node(),
        "owner": getuser(),
        "tailscale_ip": tailscale_ip(),
        "ts": time.time(),
        "agents": detect_agents(),
    }


def main() -> None:
    print(f"[reporter] laptop={platform.node()} owner={getuser()} ip={tailscale_ip()}")
    print(f"[reporter] streaming to {SERVER_URL} every {INTERVAL}s")
    while True:
        try:
            with connect(SERVER_URL) as ws:
                while True:
                    status = build_status()
                    ws.send(json.dumps(status))
                    names = ", ".join(a["name"] for a in status["agents"]) or "-"
                    print(f"[reporter] sent {len(status['agents'])} agent(s): {names}")
                    _drain_controls(ws, INTERVAL)
        except Exception as e:
            print(f"[reporter] connection error: {e} — retrying in 2s")
            time.sleep(2)


if __name__ == "__main__":
    main()
