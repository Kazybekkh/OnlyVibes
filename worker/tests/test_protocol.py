"""End-to-end: a real Worker + mock agent against a stub `WS /worker` hub (and stub agent HTTP API)."""
from __future__ import annotations

import asyncio
import json
import os
import time

import pytest
from websockets.asyncio.server import serve

from conftest import free_port, git
from adapters import AgentResult
from gitops import GitOps
from worker import Config, TaskRun, Worker, normalize_server_url


class StubWS:
    """Records every frame from the worker; lets the test push frames to it."""

    def __init__(self):
        self.port = free_port()
        self.frames: asyncio.Queue = asyncio.Queue()
        self.all: list[dict] = []
        self.conns = []
        self.paths: list[str] = []

    async def handler(self, ws):
        self.conns.append(ws)
        self.paths.append(ws.request.path)
        try:
            async for raw in ws:
                msg = json.loads(raw)
                msg["_conn"] = len(self.conns)
                self.all.append(msg)
                await self.frames.put(msg)
        except Exception:
            pass

    async def __aenter__(self):
        self.server = await serve(self.handler, "127.0.0.1", self.port)
        return self

    async def __aexit__(self, *exc):
        self.server.close()
        await self.server.wait_closed()

    async def send(self, msg: dict, conn: int = -1):
        await self.conns[conn].send(json.dumps(msg))

    async def expect(self, type_: str, timeout: float = 30.0) -> dict:
        end = time.monotonic() + timeout
        while True:
            left = end - time.monotonic()
            if left <= 0:
                raise AssertionError(f"timed out waiting for {type_}; got {[m['type'] for m in self.all]}")
            msg = await asyncio.wait_for(self.frames.get(), timeout=left)
            if msg["type"] == type_:
                return msg

    def of(self, type_: str) -> list[dict]:
        return [m for m in self.all if m["type"] == type_]

    def events(self, task_id: str) -> list[dict]:
        out = []
        for m in self.of("task_event"):
            if m["task_id"] == task_id:
                out += m.get("events") or [m["event"]]
        return out


def make_cfg(port: int, tmp_path, **kw) -> Config:
    base = dict(server_url=f"ws://127.0.0.1:{port}/ws", worker_id="w-test", laptop="test-laptop", owner="tester",
                tailscale_ip="100.64.0.99", agents=["mock"], workspaces_dir=str(tmp_path / "ws"),
                flush_interval=0.05, heartbeat_interval=0.3)
    base.update(kw)
    return Config(**base)


PROJECT = {"id": "p-1", "prompt": "Build a pomodoro timer with weekly stats", "title": "Pomodoro", "repo": "",
           "base_branch": "main", "status": "active", "plan_task_id": "p-1-PLAN", "summary": ""}


def run_task(hub_url: str, key: str = "T1", kind: str = "build", repo: str = "", agent: str = "mock") -> dict:
    task = {"id": f"p-1-{key}", "key": key, "project_id": "p-1", "kind": kind, "title": "Timer engine",
            "description": "OWNS: src/timer*. Export timer_engine_ready().", "acceptance": ["tests pass"],
            "depends_on": [], "priority": 2, "agent_hint": "", "status": "claimed",
            "assignee": {"worker_id": "w-test", "laptop": "test-laptop", "owner": "tester", "agent": agent}}
    board = [{k: task[k] for k in ("id", "key", "title", "status", "assignee", "depends_on")},
             {"id": "p-1-T2", "key": "T2", "title": "Stats UI", "status": "backlog", "assignee": None,
              "depends_on": [task["id"]]}]
    return {"type": "run_task", "task": task, "project": {**PROJECT, "repo": repo}, "board": board,
            "agent": agent, "server_http": hub_url, "token": "tok-1"}


async def start_worker(cfg: Config) -> tuple[Worker, asyncio.Task]:
    w = Worker(cfg)
    return w, asyncio.create_task(w.run())


async def stop_worker(w: Worker, t: asyncio.Task):
    w.stop()
    await asyncio.wait_for(t, timeout=20)


def test_normalize_server_url():
    assert normalize_server_url("ws://100.107.184.10:8000/ws") == "ws://100.107.184.10:8000/worker"
    assert normalize_server_url("ws://h:8000") == "ws://h:8000/worker"
    assert normalize_server_url("ws://h:8000/worker", "s3cret") == "ws://h:8000/worker?secret=s3cret"
    assert normalize_server_url("wss://h/custom") == "wss://h/custom"


def test_build_task_local_mode(stub_hub, tmp_path, monkeypatch):
    monkeypatch.setenv("MOCK_SPEED", "40")
    hub = stub_hub()

    async def scenario():
        async with StubWS() as srv:
            w, wt = await start_worker(make_cfg(srv.port, tmp_path))
            hello = await srv.expect("hello")
            assert srv.paths[0] == "/worker"  # /ws rewritten
            assert hello["running"] == []
            assert isinstance(hello["worker"].pop("device", {}), dict)  # optional device info (OS/model)
            assert hello["worker"] == {"id": "w-test", "laptop": "test-laptop", "owner": "tester",
                                       "tailscale_ip": "100.64.0.99", "agents": ["mock"], "max_concurrent": 1,
                                       "version": "2"}
            await srv.send({"type": "welcome", "worker_id": "w-test", "server_version": "2", "server_time": 0})
            await srv.send({"type": "some_future_type"})
            await srv.send(run_task(hub.url))
            started = await srv.expect("task_started")
            assert started["task_id"] == "p-1-T1" and started["agent"] == "mock" and started["branch"] == ""
            assert started["workspace"].endswith(os.path.join("p-1", "shared"))
            await srv.send({"type": "chat", "message": {"id": "m-000500", "project_id": "p-1", "to": "p-1-T1",
                                                        "text": "please also add a README note",
                                                        "from": {"kind": "human", "name": "carlo"}}})
            fin = await srv.expect("task_finished", timeout=60)
            await asyncio.sleep(0.5)  # let a heartbeat arrive after the task
            await stop_worker(w, wt)
            return started, fin

    started, fin = asyncio.run(scenario())
    assert fin["ok"] is True and fin["merged"] is False and fin["error"] == "" and fin["pr_url"] == ""
    assert "T1 done" in fin["result"]
    # (the stub recorded frames in order; check them with a fresh look)


def test_local_mode_leftovers_not_committed_while_another_task_shares_workspace(tmp_path, monkeypatch):
    """Seen live (claude-code + codex, MAX_CONCURRENT=2): T1's `git add -A` leftover commit swallowed
    T2's half-done files. While a sibling task runs in the shared tree, leftovers stay uncommitted."""
    w = Worker(make_cfg(free_port(), tmp_path, max_concurrent=2))
    ws = str(tmp_path / "ws" / "p-1" / "shared")

    async def fake_agent(run, prompt, workspace, env, mode="", files=None):
        with open(os.path.join(workspace, f"{run.key.lower()}.py"), "w") as fh:
            fh.write("x = 1\n")                     # forgot to commit it
        os.makedirs(os.path.join(workspace, "__pycache__"), exist_ok=True)
        open(os.path.join(workspace, "__pycache__", "x.cpython-313.pyc"), "wb").close()
        return AgentResult(0, f"{run.key} built")

    monkeypatch.setattr(w, "_run_agent", fake_agent)

    async def flow():
        t1, t2 = TaskRun(run_task("http://x", "T1"), w.cfg), TaskRun(run_task("http://x", "T2"), w.cfg)
        w.runs = {t1.id: t1, t2.id: t2}
        g = GitOps()
        await w._build(t1, g, ws, "main", True, {})          # T2 still "running": no auto-commit
        assert "t1.py" in git("status", "--porcelain", cwd=ws)
        assert any("T2 still running" in e["text"] for e in t1.pending)
        del w.runs[t1.id]
        await w._build(t2, g, ws, "main", True, {})          # last one out commits the leftovers
        return g

    asyncio.run(flow())
    assert git("status", "--porcelain", cwd=ws).strip() == ""
    tracked = git("ls-files", cwd=ws).split()
    assert "t1.py" in tracked and "t2.py" in tracked
    assert not any("__pycache__" in f for f in tracked)    # build caches are excluded locally


def test_build_task_frames_in_order(stub_hub, tmp_path, monkeypatch):
    monkeypatch.setenv("MOCK_SPEED", "40")
    hub = stub_hub()

    async def scenario():
        async with StubWS() as srv:
            w, wt = await start_worker(make_cfg(srv.port, tmp_path))
            await srv.expect("hello")
            await srv.send(run_task(hub.url))
            await srv.expect("task_started")
            await srv.send({"type": "chat", "message": {"id": "m-000500", "project_id": "p-1", "to": "p-1-T1",
                                                        "text": "add a README note",
                                                        "from": {"kind": "human", "name": "carlo"}}})
            await srv.expect("task_finished", timeout=60)
            await asyncio.sleep(0.4)
            await stop_worker(w, wt)
            return srv

    srv = asyncio.run(scenario())
    types = [m["type"] for m in srv.all]
    assert types.index("task_started") < types.index("task_event") < types.index("task_finished")
    last_event = max(i for i, t in enumerate(types) if t == "task_event")
    assert last_event < types.index("task_finished")
    assert "task_usage" in types and "task_progress" in types
    beats = srv.of("heartbeat")
    assert beats and any(b["running"] == ["p-1-T1"] for b in beats) and beats[-1]["running"] == []
    usage = srv.of("task_usage")[-1]["tokens"]
    assert usage["input"] > 0 and usage["output"] > 0

    events = srv.events("p-1-T1")
    kinds = {e["kind"] for e in events}
    assert {"system", "user", "thinking", "tool_call", "tool_result", "assistant"} <= kinds
    assert all("seq" not in e and e["ts"] > 0 for e in events)
    assert events[0]["kind"] == "system" and "mock on test-laptop" in events[0]["text"]
    assert any(e["kind"] == "user" and "Timer engine" in e["text"] for e in events)
    assert any(e["kind"] == "system" and "message from carlo" in e["text"] for e in events)
    batched = [m for m in srv.of("task_event") if len(m["events"]) > 1]
    assert batched, "events should be batched"

    # the mock used the agent API like a real agent, through the `ov` shim
    paths = hub.paths()
    for p in ("/api/agent/inbox", "/api/agent/board", "/api/agent/progress", "/api/agent/say"):
        assert p in paths
    says = [r["body"] for r in hub.requests if r["path"] == "/api/agent/say"]
    assert any(s["to"] == "p-1-T2" for s in says)                     # told its dependent
    assert any(s.get("reply_to") == "m-000101" and s["to"] == "p-1-T2" for s in says)  # answered a question
    assert any(s.get("reply_to") == "m-000500" for s in says)          # answered the live chat
    ws = os.path.join(str(tmp_path / "ws"), "p-1", "shared")
    assert "T1: Timer engine" in git("log", "--oneline", cwd=ws)
    assert git("status", "--porcelain", cwd=ws).strip() == ""


@pytest.mark.parametrize("hub_accepts", [False, True])
def test_plan_task(stub_hub, tmp_path, monkeypatch, hub_accepts):
    monkeypatch.setenv("MOCK_SPEED", "40")
    hub = stub_hub(accept_plan=hub_accepts)

    async def scenario():
        async with StubWS() as srv:
            w, wt = await start_worker(make_cfg(srv.port, tmp_path))
            await srv.expect("hello")
            msg = run_task(hub.url, key="PLAN", kind="plan")
            msg["task"]["title"] = "Plan the project"
            await srv.send(msg)
            await srv.expect("task_started")
            fin = await srv.expect("task_finished", timeout=60)
            await stop_worker(w, wt)
            return srv, fin

    srv, fin = asyncio.run(scenario())
    assert fin["ok"] is True
    plans = srv.of("plan_result")
    if hub_accepts:  # the agent already submitted via `ov plan` -> no duplicate plan_result
        assert plans == []
        assert "/api/agent/plan" in hub.paths()
    else:
        assert len(plans) == 1
        p = plans[0]
        assert p["task_id"] == "p-1-PLAN" and p["project_id"] == "p-1" and p["title"]
        assert 5 <= len(p["tasks"]) <= 7
        assert p["tasks"][0]["ref"] == "T1" and p["tasks"][0]["depends_on"] == []
        assert set(p["tasks"][0]) == {"ref", "title", "description", "acceptance", "depends_on", "priority", "agent_hint"}
        assert srv.all.index(plans[0]) < srv.all.index(fin)
    shared = os.path.join(str(tmp_path / "ws"), "p-1", "shared")
    assert os.path.exists(os.path.join(shared, "PLAN.md"))


def test_cancel_task_kills_run(stub_hub, tmp_path, monkeypatch):
    monkeypatch.setenv("MOCK_SPEED", "1")  # 20-60 s run
    hub = stub_hub()

    async def scenario():
        async with StubWS() as srv:
            w, wt = await start_worker(make_cfg(srv.port, tmp_path))
            await srv.expect("hello")
            await srv.send(run_task(hub.url))
            await srv.expect("task_started")
            await asyncio.sleep(0.5)
            t0 = time.monotonic()
            await srv.send({"type": "cancel_task", "task_id": "p-1-T1", "reason": "cancelled by carlo"})
            fin = await srv.expect("task_finished", timeout=10)
            elapsed = time.monotonic() - t0
            await stop_worker(w, wt)
            return fin, elapsed, srv

    fin, elapsed, srv = asyncio.run(scenario())
    assert fin["ok"] is False and fin["error"] == "cancelled: cancelled by carlo"
    assert elapsed < 5
    assert any(e["kind"] == "error" and "cancelled" in e["text"] for e in srv.events("p-1-T1"))


def test_reconnect_reports_running_tasks(stub_hub, tmp_path, monkeypatch):
    monkeypatch.setenv("MOCK_SPEED", "8")  # 2.5-7.5 s run
    hub = stub_hub()

    async def scenario():
        async with StubWS() as srv:
            w, wt = await start_worker(make_cfg(srv.port, tmp_path))
            await srv.expect("hello")
            await srv.send(run_task(hub.url))
            await srv.expect("task_started")
            await srv.conns[0].close()  # hub drops the connection mid-task
            hello2 = await srv.expect("hello", timeout=15)
            fin = await srv.expect("task_finished", timeout=60)
            await stop_worker(w, wt)
            return hello2, fin

    hello2, fin = asyncio.run(scenario())
    assert hello2["running"] == ["p-1-T1"] and hello2["_conn"] == 2
    assert fin["ok"] is True and fin["_conn"] == 2


def test_task_finished_while_offline_is_listed_in_hello(stub_hub, tmp_path, monkeypatch):
    """A task that finished while the hub was away must be re-attached, not redone."""
    monkeypatch.setenv("MOCK_SPEED", "40")
    hub = stub_hub()
    port = free_port()  # nothing listens here at first: the worker keeps retrying

    async def scenario():
        w, wt = await start_worker(make_cfg(port, tmp_path))
        w.start_task(run_task(hub.url))  # as if run_task arrived just before the hub went down
        while w.runs:
            await asyncio.sleep(0.05)
        assert "p-1-T1" in w.in_flight() and not w.runs
        srv = StubWS()
        srv.port = port
        async with srv:
            hello = await srv.expect("hello", timeout=20)
            fin = await srv.expect("task_finished", timeout=10)
        await stop_worker(w, wt)
        return hello, fin, w

    hello, fin, w = asyncio.run(scenario())
    assert hello["running"] == ["p-1-T1"]
    assert fin["ok"] is True
    assert w.in_flight() == []


def test_shutdown_reports_running_task(stub_hub, tmp_path, monkeypatch):
    monkeypatch.setenv("MOCK_SPEED", "1")
    hub = stub_hub()

    async def scenario():
        async with StubWS() as srv:
            w, wt = await start_worker(make_cfg(srv.port, tmp_path))
            await srv.expect("hello")
            await srv.send(run_task(hub.url))
            await srv.expect("task_started")
            await asyncio.sleep(0.3)
            await stop_worker(w, wt)
            return await srv.expect("task_finished", timeout=5)

    fin = asyncio.run(scenario())
    assert fin["ok"] is False and "shutting down" in fin["error"]


def test_unavailable_agent_falls_back(stub_hub, tmp_path, monkeypatch):
    monkeypatch.setenv("MOCK_SPEED", "40")
    hub = stub_hub()

    async def scenario():
        async with StubWS() as srv:
            w, wt = await start_worker(make_cfg(srv.port, tmp_path))
            await srv.expect("hello")
            await srv.send(run_task(hub.url, agent="cursor"))
            started = await srv.expect("task_started")
            fin = await srv.expect("task_finished", timeout=60)
            await stop_worker(w, wt)
            return started, fin, srv

    started, fin, srv = asyncio.run(scenario())
    assert started["agent"] == "mock" and fin["ok"]
    assert any("'cursor' is not available" in e["text"] for e in srv.events("p-1-T1"))


def test_github_mode_build_push_pr_merge(stub_hub, fake_gh, tmp_path, monkeypatch):
    monkeypatch.setenv("MOCK_SPEED", "40")
    fake_gh.bare("team/app")
    hub = stub_hub()

    async def scenario():
        async with StubWS() as srv:
            w, wt = await start_worker(make_cfg(srv.port, tmp_path))
            await srv.expect("hello")
            await srv.send(run_task(hub.url, repo="team/app"))
            started = await srv.expect("task_started")
            status = await srv.expect("task_status", timeout=60)
            fin = await srv.expect("task_finished", timeout=60)
            await stop_worker(w, wt)
            return started, status, fin, srv

    started, status, fin, srv = asyncio.run(scenario())
    assert started["branch"] == "ov/p-1-t1-timer-engine"
    assert started["workspace"].endswith(os.path.join("p-1", "T1"))
    assert status["status"] == "review"
    assert fin["ok"] and fin["merged"] and fin["pr_url"] == "https://github.com/team/app/pull/1", fin
    assert fin["branch"] == "ov/p-1-t1-timer-engine"
    files = git("--git-dir", os.path.join(fake_gh.root, "team/app.git"), "ls-tree", "-r", "--name-only", "main")
    assert "src/t1_timer_engine.py" in files
    git_events = [e["text"] for e in srv.events("p-1-T1") if e["kind"] == "git"]
    assert any("push" in t for t in git_events) and any("pr create" in t for t in git_events)
    create = next(c for c in fake_gh.calls() if c[:2] == ["pr", "create"])
    assert create[create.index("--title") + 1] == "T1: Timer engine"
    assert "tests pass" in create[create.index("--body") + 1]


def test_github_mode_plan_bootstraps_empty_repo(stub_hub, fake_gh, tmp_path, monkeypatch):
    monkeypatch.setenv("MOCK_SPEED", "40")
    fake_gh.bare("team/fresh", seed=False)
    hub = stub_hub(accept_plan=False)

    async def scenario():
        async with StubWS() as srv:
            w, wt = await start_worker(make_cfg(srv.port, tmp_path))
            await srv.expect("hello")
            await srv.send(run_task(hub.url, key="PLAN", kind="plan", repo="team/fresh"))
            plan = await srv.expect("plan_result", timeout=60)
            fin = await srv.expect("task_finished", timeout=30)
            await stop_worker(w, wt)
            return plan, fin

    plan, fin = asyncio.run(scenario())
    assert fin["ok"] and 5 <= len(plan["tasks"]) <= 7
    assert "pomodoro timer" in fake_gh.show("team/fresh", "main", "README.md")
    assert "# Plan:" in fake_gh.show("team/fresh", "main", "PLAN.md")


class ConflictAgent:
    """Test adapter: edits README.md while a 'teammate' merges a conflicting README edit into main;
    on the conflict re-run it resolves by keeping both lines (like the mock agent does)."""

    @staticmethod
    def build(bare: str):
        import subprocess
        import tempfile

        from adapters import Adapter, AgentResult, make_event
        from mock_agent import MockAdapter

        class _Conflicty(Adapter):
            name = "conflicty"

            async def run(self, prompt, cwd, env, emit, ctx=None):
                if ctx.get("mode") == "resolve":
                    assert ctx["files"] == ["README.md"]
                    return await MockAdapter().run(prompt, cwd, env, emit, ctx)
                tmp = tempfile.mkdtemp()
                for args, where in ((["clone", "-q", bare, tmp], None),
                                    (["commit", "-q", "-am", "teammate edit"], tmp),
                                    (["push", "-q", "origin", "main"], tmp)):
                    if args[0] == "commit":
                        open(os.path.join(tmp, "README.md"), "w").write("# App\n\nteammate line\n")
                    subprocess.run(["git", *args], cwd=where, check=True)
                open(os.path.join(cwd, "README.md"), "w").write("# App\n\nmy line\n")
                subprocess.run(["git", "commit", "-q", "-am", "T1: readme"], cwd=cwd, check=True)
                emit(make_event("assistant", "edited README"))
                self.final_text = "edited README"
                return AgentResult(0, "edited README")

        return _Conflicty


def test_github_mode_conflict_is_resolved_by_agent_rerun(stub_hub, fake_gh, tmp_path, monkeypatch):
    import adapters

    monkeypatch.setenv("MOCK_SPEED", "100")
    bare = fake_gh.bare("team/app")
    monkeypatch.setitem(adapters.ADAPTERS, "conflicty", ConflictAgent.build(bare))
    hub = stub_hub()

    async def scenario():
        async with StubWS() as srv:
            w, wt = await start_worker(make_cfg(srv.port, tmp_path, agents=["conflicty"]))
            await srv.expect("hello")
            await srv.send(run_task(hub.url, repo="team/app", agent="conflicty"))
            fin = await srv.expect("task_finished", timeout=60)
            await stop_worker(w, wt)
            return fin, srv

    fin, srv = asyncio.run(scenario())
    assert fin["ok"] and fin["merged"], fin
    readme = fake_gh.show("team/app", "main", "README.md")
    assert "teammate line" in readme and "my line" in readme and "<<<<<<<" not in readme
    events = srv.events("p-1-T1")
    assert any("merge conflicts in README.md" in e["text"] for e in events)
    assert sum(1 for e in events if e["kind"] == "user") == 2  # build prompt + conflict prompt
    assert any(e["kind"] == "user" and "resolve" in e["text"] for e in events)
