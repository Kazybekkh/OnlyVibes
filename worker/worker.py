#!/usr/bin/env python3
"""
OnlyVibes worker — runs on each laptop of the orchestra (PROTOCOL.md §3, §5, §6).

Connects to the hub's `WS /worker`, announces which agent CLIs this laptop
has, and executes the tasks the hub assigns: it prepares a per-task git
workspace, runs the agent headless and fully automatic inside it, streams the
normalized transcript (batched every ~250 ms), then commits / rebases / pushes /
opens and merges the PR (or just commits, in local mode). PLAN tasks turn the
product prompt into a board of build tasks (`plan_result`).

Run:
    cd worker
    uv run worker.py
    # against the team hub over Tailscale:
    SERVER_URL=ws://100.x.x.x:8000/worker uv run worker.py
    # demo/test without any agent CLI:
    AGENTS=mock MOCK_SPEED=5 SERVER_URL=ws://localhost:8765/worker uv run worker.py

Env vars:
    SERVER_URL        hub websocket (default ws://localhost:8000/worker). A URL ending in
                      /ws (the reporter endpoint) or with no path is rewritten to /worker.
    WORKER_ID         worker id (default: hostname, lowercased, without ".local")
    MAX_CONCURRENT    tasks run in parallel on this laptop (default 1)
    AGENTS            comma list of adapters, in preference order; overrides auto-detection
                      (claude-code, codex, vibe, cursor, mock). `mock` is only enabled this way.
    PLAN_AGENT        adapter to use for PLAN tasks if available here (default: hub's choice)
    WORKSPACES_DIR    root of task workspaces (default ~/.onlyvibes/workspaces)
    AUTO_MERGE        1 = squash-merge PRs automatically (default), 0 = leave them open for review
    OV_SHARED_SECRET  appended to SERVER_URL as ?secret=… when the hub requires it
    AGENT_TIMEOUT     max seconds per agent run (default 3600, 0 = unlimited)
    GH_BIN / GIT_BIN  gh / git executables (default "gh" / "git")
    MOCK_SPEED        speed multiplier for the mock agent (default 1)
"""
from __future__ import annotations

import asyncio
import json
import os
import platform
import random
import re
import shlex
import signal
import socket
import subprocess
import sys
import time
import traceback
import urllib.parse
from collections import deque
from dataclasses import dataclass, field
from getpass import getuser

from websockets.asyncio.client import connect
from websockets.exceptions import WebSocketException

from adapters import AgentResult, detect_agents, make_adapter, make_event, truncate
from gitops import GitError, GitOps, branch_name
from planparse import PlanError, extract_plan, normalize_plan
from prompts import render_build_prompt, render_conflict_prompt, render_plan_prompt

VERSION = "2"
OV_PY = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ov.py")
MAX_OUTBOX = 5000           # frames buffered while disconnected
EVENTS_PER_FRAME = 100
MAX_EVENTS_PER_FLUSH = 600  # beyond this a flush keeps head + tail and says how many were skipped
DROPPABLE = {"task_event", "task_usage", "task_progress"}
# never leak into agent processes: the hub secret, and markers that make a nested
# `claude` think it runs inside another Claude Code session
STRIP_ENV = {
    "OV_SHARED_SECRET", "CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT", "CLAUDE_CODE_SESSION_ID",
    "CLAUDE_CODE_CHILD_SESSION", "CLAUDE_PID", "CLAUDE_CODE_MESSAGING_SOCKET",
    "CLAUDE_CODE_MESSAGING_TOKEN", "CLAUDE_CODE_CHILD_NOTES_DIR", "CLAUDE_CODE_SESSION_ATTENDED",
    "CLAUDE_CODE_EXECPATH",
}


# ---------------------------------------------------------------- config


def tailscale_ip() -> str:
    """Best-effort Tailscale IPv4 address of this laptop (same logic as reporter.py)."""
    try:
        out = subprocess.run(["tailscale", "ip", "-4"], capture_output=True, text=True, timeout=3)
        if out.returncode == 0 and out.stdout.strip():
            return out.stdout.strip().split()[0]
    except Exception:
        pass
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("8.8.8.8", 80))
            return s.getsockname()[0]
    except Exception:
        return "127.0.0.1"


def default_worker_id() -> str:
    host = platform.node().lower()
    return host[:-len(".local")] if host.endswith(".local") else host or "worker"


def normalize_server_url(url: str, secret: str = "") -> str:
    """ws://host:8000[/ws] -> ws://host:8000/worker, plus ?secret=… if configured."""
    parts = urllib.parse.urlsplit(url.strip())
    path = parts.path
    if path in ("", "/", "/ws", "/ws/"):
        path = "/worker"
    query = urllib.parse.parse_qsl(parts.query)
    if secret and not any(k == "secret" for k, _ in query):
        query.append(("secret", secret))
    return urllib.parse.urlunsplit((parts.scheme or "ws", parts.netloc, path, urllib.parse.urlencode(query), ""))


def http_base_from_ws(url: str) -> str:
    parts = urllib.parse.urlsplit(url)
    scheme = "https" if parts.scheme == "wss" else "http"
    return f"{scheme}://{parts.netloc}"


@dataclass
class Config:
    server_url: str = "ws://localhost:8000/worker"
    worker_id: str = field(default_factory=default_worker_id)
    laptop: str = field(default_factory=platform.node)
    owner: str = field(default_factory=getuser)
    tailscale_ip: str = "127.0.0.1"
    agents: list[str] = field(default_factory=list)
    max_concurrent: int = 1
    workspaces_dir: str = os.path.expanduser("~/.onlyvibes/workspaces")
    auto_merge: bool = True
    shared_secret: str = ""
    plan_agent: str = ""
    agent_timeout: float = 3600.0
    flush_interval: float = 0.25
    heartbeat_interval: float = 5.0
    label: str = ""  # log prefix

    @classmethod
    def from_env(cls, env: dict | None = None) -> "Config":
        env = os.environ if env is None else env
        return cls(
            server_url=env.get("SERVER_URL", "ws://localhost:8000/worker"),
            worker_id=env.get("WORKER_ID") or default_worker_id(),
            tailscale_ip=tailscale_ip(),
            agents=detect_agents(env.get("AGENTS", "")),
            max_concurrent=max(1, int(env.get("MAX_CONCURRENT", "1") or 1)),
            workspaces_dir=os.path.expanduser(env.get("WORKSPACES_DIR") or "~/.onlyvibes/workspaces"),
            auto_merge=(env.get("AUTO_MERGE", "1").strip().lower() not in ("0", "false", "no", "off")),
            shared_secret=env.get("OV_SHARED_SECRET", ""),
            plan_agent=env.get("PLAN_AGENT", "").strip(),
            agent_timeout=float(env.get("AGENT_TIMEOUT", "3600") or 0),
        )

    @property
    def ws_url(self) -> str:
        return normalize_server_url(self.server_url, self.shared_secret)


def _safe(name: str) -> str:
    """A path component that can't escape the workspaces dir."""
    return re.sub(r"[^A-Za-z0-9._-]+", "_", str(name)).strip("._") or "x"


def write_ov_shim(bin_dir: str) -> str:
    """Executable `ov` that runs ov.py with this interpreter (ov is stdlib-only)."""
    os.makedirs(bin_dir, exist_ok=True)
    path = os.path.join(bin_dir, "ov")
    with open(path, "w") as fh:
        fh.write(f"#!/bin/sh\nexec {shlex.quote(sys.executable)} {shlex.quote(OV_PY)} \"$@\"\n")
    os.chmod(path, 0o755)
    return path


def readme_for(project: dict) -> str:
    title = project.get("title") or "OnlyVibes project"
    return (f"# {title}\n\nBuilt by an OnlyVibes orchestra of AI coding agents.\n\n"
            f"## Product prompt\n\n{project.get('prompt', '').strip()}\n")


def plan_markdown(project: dict, plan: dict) -> str:
    lines = [f"# Plan: {plan.get('title') or project.get('title') or 'OnlyVibes project'}", "",
             plan.get("summary") or "", "", "## Tasks", ""]
    for t in plan["tasks"]:
        deps = f" (after {', '.join(t['depends_on'])})" if t["depends_on"] else ""
        lines.append(f"### {t['ref']} · {t['title']}{deps} — P{t['priority']}")
        lines.append("")
        lines.append(t["description"] or "")
        if t["acceptance"]:
            lines.append("")
            lines += [f"- [ ] {a}" for a in t["acceptance"]]
        lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------- one task


class TaskRun:
    """State of one assigned task on this worker."""

    def __init__(self, msg: dict, cfg: Config):
        self.task: dict = msg.get("task") or {}
        self.project: dict = msg.get("project") or {}
        self.board: list[dict] = msg.get("board") or []
        self.server_http: str = (msg.get("server_http") or http_base_from_ws(cfg.ws_url)).rstrip("/")
        self.token: str = msg.get("token") or ""
        self.requested_agent: str = msg.get("agent") or self.task.get("agent_hint") or ""
        self.agent: str = ""
        self.kind: str = self.task.get("kind") or ("plan" if self.task.get("key") == "PLAN" else "build")
        self.branch = ""
        self.pr_url = ""
        self.adapter = None
        self.usage_base = {"input": 0, "output": 0}
        self.usage_sent: dict | None = None
        self.pending: list[dict] = []
        self.cancel_reason = ""
        self.atask: asyncio.Task | None = None
        self.early_chats: list[dict] = []  # chat that arrived before the agent started

    @property
    def id(self) -> str:
        return self.task.get("id", "")

    @property
    def key(self) -> str:
        return self.task.get("key") or self.id.rsplit("-", 1)[-1]

    def emit(self, ev: dict) -> None:
        self.pending.append(ev)

    def system(self, text: str) -> None:
        self.emit(make_event("system", text))

    def git_emit(self, kind: str, text: str, data: dict | None = None) -> None:
        self.emit(make_event(kind, text, data=data))

    def total_usage(self) -> dict:
        cur = self.adapter.usage if self.adapter else {"input": 0, "output": 0}
        return {"input": self.usage_base["input"] + int(cur.get("input", 0)),
                "output": self.usage_base["output"] + int(cur.get("output", 0))}

    def outcome(self, ok: bool, result: str = "", error: str = "", merged: bool = False) -> dict:
        return {"ok": ok, "result": truncate(result, 4000), "error": truncate(error, 2000),
                "branch": self.branch, "pr_url": self.pr_url, "merged": merged}


# ---------------------------------------------------------------- worker


class Worker:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.runs: dict[str, TaskRun] = {}
        self.outbox: deque[dict] = deque()
        self._wake = asyncio.Event()
        self._stopping = asyncio.Event()
        self._unknown: set[str] = set()
        self.ws = None
        self.connected = asyncio.Event()  # handy for tests / fleet
        self._stop_at = 0.0

    def log(self, msg: str) -> None:
        print(f"[worker {self.cfg.label or self.cfg.worker_id}] {msg}", flush=True)

    # ------------------------------------------------------------ outbound

    def send(self, msg: dict) -> None:
        """Queue a frame for the hub (kept across reconnects; oldest event frames dropped if huge)."""
        self.outbox.append(msg)
        if len(self.outbox) > MAX_OUTBOX:
            for i, m in enumerate(self.outbox):
                if m.get("type") in DROPPABLE:
                    del self.outbox[i]
                    break
        self._wake.set()

    def in_flight(self) -> list[str]:
        """Tasks still executing here, plus finished ones whose task_finished hasn't been sent yet.

        The latter matter after a reconnect / hub restart: listing them in hello lets the hub
        re-attach the task, so the queued transcript, plan_result and task_finished are
        accepted instead of being dropped as "not assigned" (and the work redone).
        """
        ids = list(self.runs)
        for m in self.outbox:
            if m.get("type") == "task_finished" and m.get("task_id") and m["task_id"] not in ids:
                ids.append(m["task_id"])
        return ids

    def hello(self) -> dict:
        c = self.cfg
        return {"type": "hello", "running": self.in_flight(),
                "worker": {"id": c.worker_id, "laptop": c.laptop, "owner": c.owner, "tailscale_ip": c.tailscale_ip,
                           "agents": c.agents, "max_concurrent": c.max_concurrent, "version": VERSION}}

    def flush_run(self, run: TaskRun) -> None:
        if run.pending:
            events, run.pending = run.pending, []
            if len(events) > MAX_EVENTS_PER_FLUSH:
                skipped = len(events) - 500
                events = events[:100] + [make_event("system", f"… {skipped} events skipped (stream too fast)")] + events[-400:]
            for i in range(0, len(events), EVENTS_PER_FRAME):
                self.send({"type": "task_event", "task_id": run.id, "events": events[i:i + EVENTS_PER_FRAME]})
        usage = run.total_usage()
        if usage != run.usage_sent and (usage["input"] or usage["output"]):
            run.usage_sent = usage
            self.send({"type": "task_usage", "task_id": run.id, "tokens": usage})

    async def _flusher(self) -> None:
        while True:
            await asyncio.sleep(self.cfg.flush_interval)
            for run in list(self.runs.values()):
                self.flush_run(run)

    async def _sender(self, ws) -> None:
        while True:
            if not self.outbox:
                self._wake.clear()
                await self._wake.wait()
                continue
            msg = self.outbox[0]
            await ws.send(json.dumps(msg, default=str))
            if self.outbox and self.outbox[0] is msg:
                self.outbox.popleft()

    async def _heartbeat(self, ws) -> None:
        while True:
            await asyncio.sleep(self.cfg.heartbeat_interval)
            await ws.send(json.dumps({"type": "heartbeat", "running": list(self.runs)}))

    # ------------------------------------------------------------ connection

    async def run(self) -> None:
        """Connect / reconnect until stop() is called."""
        c = self.cfg
        self.log(f"laptop={c.laptop} owner={c.owner} ip={c.tailscale_ip} agents={','.join(c.agents) or '-'} "
                 f"max_concurrent={c.max_concurrent}")
        if not c.agents:
            self.log("WARNING: no agent CLI found (install claude/codex/cursor-agent/vibe or set AGENTS=mock)")
        flusher = asyncio.create_task(self._flusher())
        backoff = 1.0
        try:
            while not self._stopping.is_set():
                self.log(f"connecting to {c.ws_url.split('?')[0]}")
                try:
                    async with connect(c.ws_url, open_timeout=10, ping_interval=20, ping_timeout=20,
                                       close_timeout=3, max_size=32 * 1024 * 1024) as ws:
                        backoff = 1.0
                        await self._session(ws)
                except (OSError, WebSocketException, asyncio.TimeoutError) as e:
                    self.log(f"connection error: {type(e).__name__}: {e}")
                except Exception as e:  # never die on a surprise
                    self.log(f"unexpected session error: {type(e).__name__}: {e}")
                if self._stopping.is_set():
                    break
                delay = backoff * random.uniform(0.8, 1.2)
                self.log(f"reconnecting in {delay:.1f}s ({len(self.runs)} task(s) still running)")
                try:
                    await asyncio.wait_for(self._stopping.wait(), timeout=delay)
                except asyncio.TimeoutError:
                    pass
                backoff = min(backoff * 2, 10.0)  # well inside the hub's 30 s restart grace
        finally:
            await self._cancel_all("worker shutting down")
            flusher.cancel()
            self.log("stopped")

    async def _session(self, ws) -> None:
        self.ws = ws
        await ws.send(json.dumps(self.hello()))
        self.connected.set()
        self.log(f"connected; hello sent (running: {list(self.runs) or '-'})")
        jobs = [asyncio.create_task(self._sender(ws)), asyncio.create_task(self._heartbeat(ws)),
                asyncio.create_task(self._receiver(ws)), asyncio.create_task(self._stopping.wait())]
        try:
            done, _ = await asyncio.wait(jobs, return_when=asyncio.FIRST_COMPLETED)
            if self._stopping.is_set():
                await self._cancel_all("worker shutting down")
                await self._drain(3.0)
                return
            for job in done:
                if not job.cancelled() and job.exception():
                    raise job.exception()
        finally:
            self.connected.clear()
            self.ws = None
            for job in jobs:
                job.cancel()
            await asyncio.gather(*jobs, return_exceptions=True)

    async def _drain(self, timeout: float) -> None:
        end = time.monotonic() + timeout
        while self.outbox and time.monotonic() < end:
            await asyncio.sleep(0.05)

    async def _receiver(self, ws) -> None:
        async for raw in ws:
            try:
                msg = json.loads(raw)
                if isinstance(msg, dict):
                    self.handle(msg)
            except Exception as e:
                self.log(f"bad frame ignored: {type(e).__name__}: {e}")

    def stop(self) -> None:
        if self._stopping.is_set():
            if time.monotonic() - self._stop_at < 1.0:
                return  # the same signal delivered twice (e.g. forwarded by `uv run`)
            self.log("second stop signal: killing agents now")
            for run in list(self.runs.values()):
                proc = getattr(run.adapter, "proc", None)
                if proc and proc.returncode is None:
                    try:
                        os.killpg(proc.pid, signal.SIGKILL)
                    except (ProcessLookupError, PermissionError):
                        pass
            os._exit(130)
        self.log("stopping… (signal again to force)")
        self._stop_at = time.monotonic()
        self._stopping.set()

    async def _cancel_all(self, reason: str, timeout: float = 15.0) -> None:
        pending = []
        for run in list(self.runs.values()):
            if run.atask and not run.atask.done():
                run.cancel_reason = reason
                run.atask.cancel()
                pending.append(run.atask)
        if pending:
            await asyncio.wait(pending, timeout=timeout)

    # ------------------------------------------------------------ inbound

    def handle(self, msg: dict) -> None:
        t = msg.get("type")
        if t == "welcome":
            self.log(f"welcome: worker_id={msg.get('worker_id')} server_version={msg.get('server_version')}")
        elif t == "run_task":
            self.start_task(msg)
        elif t == "cancel_task":
            self.cancel_task(msg.get("task_id", ""), msg.get("reason") or "cancelled by the hub")
        elif t == "chat":
            self.on_chat(msg.get("message") or {})
        elif t in ("ping", "pong", "ack"):
            pass
        elif t not in self._unknown:
            self._unknown.add(t)
            self.log(f"ignoring unknown message type {t!r}")

    def pick_agent(self, run: TaskRun) -> str:
        agents = self.cfg.agents
        if run.kind == "plan" and self.cfg.plan_agent in agents:
            return self.cfg.plan_agent
        if run.requested_agent in agents:
            return run.requested_agent
        return agents[0] if agents else ""

    def start_task(self, msg: dict) -> None:
        run = TaskRun(msg, self.cfg)
        if not run.id:
            self.log("run_task without task id ignored")
            return
        if run.id in self.runs:
            self.log(f"{run.id} is already running here; duplicate run_task ignored")
            return
        if len(self.runs) >= self.cfg.max_concurrent:
            self.log(f"WARNING: {run.id} assigned while {len(self.runs)} task(s) run (max {self.cfg.max_concurrent})")
        run.agent = self.pick_agent(run)
        self.runs[run.id] = run
        self.log(f"run_task {run.id} ({run.kind}) '{run.task.get('title', '')}' with {run.agent or '-'}")
        run.atask = asyncio.create_task(self._run_safe(run), name=f"task-{run.id}")

    def cancel_task(self, task_id: str, reason: str) -> None:
        run = self.runs.get(task_id)
        if not run or not run.atask:
            self.log(f"cancel_task for unknown task {task_id} ignored")
            return
        self.log(f"cancelling {task_id}: {reason}")
        run.cancel_reason = reason
        run.atask.cancel()

    def on_chat(self, message: dict) -> None:
        to = message.get("to") or "all"
        sender = message.get("from") or {}
        for run in list(self.runs.values()):
            direct = to in (run.id, self.cfg.worker_id)
            if not (direct or (to == "all" and run.project.get("id") == message.get("project_id"))):
                continue
            if sender.get("task_id") == run.id:
                continue  # our own agent's message
            if run.adapter is None:
                run.early_chats.append(message)
            delivered = bool(run.adapter and run.adapter.live_input(message))
            if direct:
                name = sender.get("name") or sender.get("kind") or "someone"
                run.system(f"message from {name}: {truncate(message.get('text', ''), 500)}"
                           + ("" if delivered else "  (the agent sees it with `ov inbox`)"))

    # ------------------------------------------------------------ task execution

    async def _run_safe(self, run: TaskRun) -> None:
        """Run one task; whatever happens, report task_finished and never raise."""
        outcome = run.outcome(False, error="worker error")
        try:
            outcome = await self._execute(run)
        except asyncio.CancelledError:
            reason = run.cancel_reason or "cancelled"
            run.emit(make_event("error", f"cancelled: {reason}"))
            outcome = run.outcome(False, error=f"cancelled: {reason}")
        except GitError as e:
            run.emit(make_event("error", f"git step failed: {e}"))
            outcome = run.outcome(False, error=f"git: {e}")
        except Exception as e:
            tb = traceback.format_exc(limit=6)
            self.log(f"task {run.id} crashed:\n{tb}")
            run.emit(make_event("error", f"worker error: {type(e).__name__}: {e}", data={"traceback": tb[-3000:]}))
            outcome = run.outcome(False, error=f"worker error: {type(e).__name__}: {e}")
        finally:
            try:
                self.flush_run(run)
                self.send({"type": "task_finished", "task_id": run.id, **outcome})
                self.log(f"task_finished {run.id} ok={outcome['ok']} merged={outcome['merged']} "
                         f"{outcome['pr_url'] or ''} {outcome['error'] or ''}".rstrip())
            finally:
                self.runs.pop(run.id, None)

    def agent_env(self, run: TaskRun, bin_dir: str, state_dir: str) -> dict:
        env = {k: v for k, v in os.environ.items() if k not in STRIP_ENV}
        env.update({
            "OV_SERVER": run.server_http, "OV_TOKEN": run.token, "OV_TASK_ID": run.id,
            "OV_TASK_KEY": run.key, "OV_PROJECT_ID": run.project.get("id", ""),
            "OV_WORKER_ID": self.cfg.worker_id, "OV_AGENT": run.agent, "OV_STATE_DIR": state_dir,
            "PATH": bin_dir + os.pathsep + env.get("PATH", ""), "GIT_TERMINAL_PROMPT": "0",
        })
        return env

    async def _run_agent(self, run: TaskRun, prompt: str, workspace: str, env: dict,
                         mode: str = "", files: list[str] | None = None) -> AgentResult:
        if run.adapter:  # second run (conflict resolution): keep the attempt's token totals
            run.usage_base = run.total_usage()
        adapter = make_adapter(run.agent)
        run.adapter = adapter
        for message in run.early_chats:
            adapter.live_input(message)
        run.early_chats.clear()
        run.emit(make_event("user", prompt))
        ctx = {"task": run.task, "project": run.project, "board": run.board,
               "mode": mode or run.kind, "files": files or []}
        timeout = self.cfg.agent_timeout or None
        try:
            result = await asyncio.wait_for(adapter.run(prompt, workspace, env, run.emit, ctx), timeout)
        except asyncio.TimeoutError:
            result = AgentResult(124, adapter.last_text(), f"agent timed out after {timeout:.0f}s", dict(adapter.usage))
        if not result.ok:
            run.emit(make_event("error", result.error or f"{run.agent} exited with code {result.exit_code}"))
        return result

    async def _execute(self, run: TaskRun) -> dict:
        cfg, task, project = self.cfg, run.task, run.project
        if not run.agent:
            return run.outcome(False, error=f"no agent adapter available on {cfg.worker_id}")
        local = not project.get("repo")
        base = project.get("base_branch") or "main"
        proj_dir = os.path.join(cfg.workspaces_dir, _safe(project.get("id") or task.get("project_id") or "project"))
        workspace = os.path.join(proj_dir, "shared" if local else _safe(run.key))
        run.branch = "" if (local or run.kind == "plan") else branch_name(task)
        self.send({"type": "task_started", "task_id": run.id, "agent": run.agent, "branch": run.branch,
                   "workspace": workspace})
        if run.requested_agent and run.requested_agent != run.agent:
            run.system(f"requested agent {run.requested_agent!r} is not available here; using {run.agent}")
        run.system(f"{run.agent} on {cfg.laptop} (worker {cfg.worker_id}, {cfg.owner}) · "
                   f"{'local mode' if local else project['repo']} · workspace {workspace}"
                   + (f" · branch {run.branch}" if run.branch else ""))

        ov_dir = os.path.join(proj_dir, ".ov", _safe(run.key))
        bin_dir, state_dir = os.path.join(ov_dir, "bin"), os.path.join(ov_dir, "state")
        write_ov_shim(bin_dir)
        os.makedirs(state_dir, exist_ok=True)
        for stale in ("plan.json", "plan.submitted", "inbox.cursor"):
            try:
                os.unlink(os.path.join(state_dir, stale))
            except FileNotFoundError:
                pass
        env = self.agent_env(run, bin_dir, state_dir)
        g = GitOps(emit=run.git_emit)
        if run.kind == "plan":
            return await self._plan(run, g, workspace, base, local, env, state_dir)
        return await self._build(run, g, workspace, base, local, env)

    # -- PLAN

    async def _plan(self, run: TaskRun, g: GitOps, workspace: str, base: str, local: bool,
                    env: dict, state_dir: str) -> dict:
        project, task = run.project, run.task
        bootstrapped = False
        if local:
            await g.init_local(workspace, base, readme_for(project))
        else:
            await g.clone_or_fetch(project["repo"], workspace)
            if await g.remote_branch_exists(workspace, base):
                await g.checkout_base(workspace, base)
            else:
                run.system(f"{project['repo']} has no '{base}' branch yet: bootstrapping it with a README")
                await g.bootstrap_base(workspace, base, {"README.md": readme_for(project)},
                                       "Initial commit: product prompt (OnlyVibes)")
                bootstrapped = True
        files = (await g.git(workspace, "ls-files", check=False, quiet=True)).out.split()
        repo_state = {"empty": bootstrapped or files in ([], ["README.md"]), "local": local, "files": files}
        self.send({"type": "task_progress", "task_id": run.id, "progress": 5, "note": f"planning with {run.agent}"})

        result = await self._run_agent(run, render_plan_prompt(project, task, repo_state, run.board), workspace, env)
        if not result.ok:
            return run.outcome(False, result.final_text, result.error or "planner failed")

        plan, submitted = self._collect_plan(state_dir, result, run)
        if plan is None:
            return run.outcome(False, result.final_text,
                               "planner finished without a plan (no `ov plan` call and no ```json block)")
        try:
            plan = normalize_plan(plan, default_title=project.get("title", ""))
        except PlanError as e:
            if not submitted:
                return run.outcome(False, result.final_text, f"invalid plan: {e}")
            run.system(f"plan was submitted by the agent but looks odd locally: {e}")
            return run.outcome(True, "plan submitted by the agent via `ov plan`")
        run.system(f"plan: {len(plan['tasks'])} tasks — " + "; ".join(f"{t['ref']} {t['title']}" for t in plan["tasks"]))

        if local or bootstrapped:
            if bootstrapped:
                await g.checkout_base(workspace, base)  # drop anything the planner left behind
            ok = await g.commit_files_to_base(workspace, base, {"PLAN.md": plan_markdown(project, plan)},
                                              "Add OnlyVibes plan", push=not local)
            if not ok:
                run.system("could not commit PLAN.md (continuing)")
        if submitted:
            run.system("plan already submitted to the hub by the agent (`ov plan`)")
        else:
            self.send({"type": "plan_result", "task_id": run.id, "project_id": project.get("id", ""),
                       "title": plan["title"], "summary": plan["summary"], "tasks": plan["tasks"]})
        return run.outcome(True, plan["summary"] or f"{len(plan['tasks'])} tasks planned", merged=True)

    @staticmethod
    def _collect_plan(state_dir: str, result: AgentResult, run: TaskRun) -> tuple[dict | None, bool]:
        """Plan from `ov plan` (state dir) or from the final message's fenced json block."""
        submitted = os.path.exists(os.path.join(state_dir, "plan.submitted"))
        path = os.path.join(state_dir, "plan.json")
        if os.path.exists(path):
            try:
                with open(path) as fh:
                    data = json.load(fh)
                plan = data if isinstance(data, dict) else {"tasks": data}
                return plan, submitted
            except (OSError, ValueError):
                pass
        texts = run.adapter.assistant_texts if run.adapter else []
        plan = extract_plan(result.final_text) or extract_plan("\n\n".join(texts))
        return plan, False

    # -- BUILD

    async def _build(self, run: TaskRun, g: GitOps, workspace: str, base: str, local: bool, env: dict) -> dict:
        cfg, project, task = self.cfg, run.project, run.task
        if local:
            await g.init_local(workspace, base, readme_for(project))
        else:
            await g.clone_or_fetch(project["repo"], workspace)
            if not await g.remote_branch_exists(workspace, base):
                raise GitError(f"{project['repo']} has no '{base}' branch (the PLAN task bootstraps it)")
            await g.checkout_task_branch(workspace, run.branch, base)
        head_before = await g.head(workspace)
        self.send({"type": "task_progress", "task_id": run.id, "progress": 2,
                   "note": f"workspace ready, {run.agent} starting"})

        prompt = render_build_prompt(project, task, run.board, branch=run.branch, local_mode=local,
                                     agent=run.agent, worker_id=cfg.worker_id)
        result = await self._run_agent(run, prompt, workspace, env)
        if not result.ok:
            return run.outcome(False, result.final_text, result.error or f"{run.agent} failed")
        summary = result.final_text.strip() or f"{run.agent} finished without a summary."
        leftovers = f"{run.key}: {task.get('title', '')} (wip leftovers)"

        if local:
            # Local mode shares one working tree per project. While another task of the project
            # runs here, `git add -A` would sweep its half-done files into OUR commit (seen live
            # with MAX_CONCURRENT=2: codex's files ended up in claude's "wip leftovers" commit).
            sharing = [r.key for r in self.runs.values()
                       if r is not run and r.project.get("id") == project.get("id")]
            if not sharing:
                committed = await g.commit_all(workspace, leftovers)
            else:
                committed = False
                if await g.is_dirty(workspace):
                    run.system(f"not auto-committing uncommitted files: {', '.join(sharing)} still "
                               f"running in this shared workspace (local mode)")
            if not committed and await g.head(workspace) == head_before:
                summary += "\n\n(No changes were committed — verification-only task.)"
            self._final_note(run, "done (committed locally)")
            return run.outcome(True, summary, merged=False)

        if await g.commit_all(workspace, leftovers):
            run.system("committed uncommitted leftovers")
        if await g.commits_ahead(workspace, base) == 0:
            run.system("no commits on the task branch: nothing to push or merge")
            self._final_note(run, "done (no changes needed)")
            return run.outcome(True, summary + "\n\n(No changes were committed — verification-only task.)",
                               merged=True)

        self.send({"type": "task_status", "task_id": run.id, "status": "review",
                   "note": f"syncing with {base}, pushing {run.branch}, opening PR"})
        sync = await g.sync_with_base(workspace, base)
        if sync.status == "conflict":
            if not await self._resolve_conflicts(run, g, workspace, base, sync.files, env):
                return run.outcome(False, summary, f"merge conflicts with {base} could not be resolved: "
                                                   f"{', '.join(sync.files)}")
        await g.push_branch(workspace, run.branch)
        run.pr_url = await g.create_pr(workspace, base, run.branch, f"{run.key}: {task.get('title', '')}",
                                       self._pr_body(run, summary))
        run.system(f"PR opened: {run.pr_url}")
        if not cfg.auto_merge:
            self._final_note(run, f"PR open for review: {run.pr_url}")
            return run.outcome(True, summary + "\n\nPR open for review (AUTO_MERGE=0).", merged=False)

        merged = await g.merge_pr(workspace, run.pr_url)
        if not merged:
            run.system(f"merge failed; re-syncing with {base} and retrying once")
            sync = await g.sync_with_base(workspace, base)
            # base usually moved because a teammate merged meanwhile; if that conflicts again,
            # the agent gets one more resolve run (it aborts the merge itself if it can't)
            if sync.status == "ok" or await self._resolve_conflicts(run, g, workspace, base, sync.files, env):
                await g.push_branch(workspace, run.branch)
                merged = await g.merge_pr(workspace, run.pr_url)
        if merged:
            run.system(f"merged into {base}")
            self._final_note(run, f"merged into {base}: {run.pr_url}")
        else:
            summary += "\n\nAuto-merge failed: the PR is left open for a human."
            self._final_note(run, f"auto-merge failed, PR open: {run.pr_url}")
        return run.outcome(True, summary, merged=merged)

    def _final_note(self, run: TaskRun, note: str) -> None:
        self.send({"type": "task_progress", "task_id": run.id, "progress": 100, "note": note})

    async def _resolve_conflicts(self, run: TaskRun, g: GitOps, workspace: str, base: str,
                                 files: list[str], env: dict) -> bool:
        run.system(f"merge conflicts in {', '.join(files)}: asking {run.agent} to resolve them")
        prompt = render_conflict_prompt(run.project, run.task, files, base=base, branch=run.branch)
        result = await self._run_agent(run, prompt, workspace, env, mode="resolve", files=files)
        if result.ok and await g.conclude_merge(workspace, f"{run.key}: merge {base} and resolve conflicts"):
            run.system("conflicts resolved")
            return True
        await g.abort_merge(workspace)
        return False

    def _pr_body(self, run: TaskRun, summary: str) -> str:
        acc = "\n".join(f"- [ ] {a}" for a in run.task.get("acceptance") or []) or "- (none)"
        c = self.cfg
        return (f"{truncate(summary, 3000)}\n\n## Acceptance criteria\n{acc}\n\n---\n"
                f"OnlyVibes task `{run.id}` · agent `{run.agent}` on `{c.laptop}` "
                f"(worker `{c.worker_id}`, {c.owner})\n")


# ---------------------------------------------------------------- entry


async def run_workers(workers: list[Worker]) -> None:
    """Run workers until SIGINT/SIGTERM."""
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, lambda: [w.stop() for w in workers])
        except (NotImplementedError, RuntimeError):
            pass
    await asyncio.gather(*(w.run() for w in workers))


def main() -> None:
    cfg = Config.from_env()
    asyncio.run(run_workers([Worker(cfg)]))


if __name__ == "__main__":
    main()
