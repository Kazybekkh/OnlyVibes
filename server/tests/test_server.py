"""
End-to-end tests for the hub: fake workers and browsers talk to the real app over
Starlette's TestClient (HTTP + websocket_connect). No network, no real agents.

    cd server && uv run pytest -q
"""
from __future__ import annotations

import json
import sys
import threading
import time
from pathlib import Path

import anyio
import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from config import Settings  # noqa: E402
from main import create_app  # noqa: E402
from planning import PlanError, map_plan, normalize_plan  # noqa: E402
from scheduler import WorkerSlot, schedule  # noqa: E402


# ----------------------------------------------------------------------------- helpers
def make_settings(tmp_path: Path, **overrides) -> Settings:
    web = tmp_path / "web"
    web.mkdir(exist_ok=True)
    (web / "index.html").write_text("<h1>board</h1>")
    (web / "ar.html").write_text("<h1>ar</h1>")
    (web / "board.js").write_text("console.log('board')")
    s = Settings(
        data_dir=tmp_path / "data", web_dir=web, tick_interval=0.05, save_debounce=0.02,
        frame_flush_interval=0.05, reattach_grace=0.0, restart_grace=0.0, ping_interval=30.0,
    )
    for k, v in overrides.items():
        setattr(s, k, v)
    return s


OPEN_WORKERS: list["FakeWorker"] = []


def close_client(c: TestClient) -> None:
    """Close fake workers first: an open websocket session would block TestClient shutdown."""
    for w in list(OPEN_WORKERS):
        w.close()
    c.__exit__(None, None, None)


@pytest.fixture
def make_client(tmp_path):
    """Factory: make_client(**settings_overrides) -> started TestClient (all closed at teardown)."""
    clients: list[TestClient] = []

    def factory(**overrides) -> TestClient:
        c = TestClient(create_app(make_settings(tmp_path, **overrides)))
        c.__enter__()
        clients.append(c)
        return c

    yield factory
    for c in reversed(clients):
        if c.portal is not None:
            close_client(c)


@pytest.fixture
def client(make_client):
    return make_client()


def hub_of(client):
    return client.app.state.hub


async def _recv(rx, timeout):
    with anyio.fail_after(timeout):
        return await rx.receive()


def recv_json(ws, timeout: float = 5.0) -> dict:
    """receive_json with a timeout so a missing frame fails the test instead of hanging."""
    msg = ws.portal.call(_recv, ws._send_rx, timeout)
    if msg["type"] == "websocket.close":
        raise WebSocketDisconnect(code=msg.get("code", 1000))
    return json.loads(msg.get("text") or msg["bytes"].decode())


def recv_until(ws, pred, timeout: float = 5.0) -> dict:
    deadline = time.monotonic() + timeout
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise AssertionError("expected frame never arrived")
        try:
            m = recv_json(ws, remaining)
        except TimeoutError:
            raise AssertionError("expected frame never arrived") from None
        if pred(m):
            return m


def no_frame(ws, pred, timeout: float = 0.3) -> None:
    """Assert no frame matching pred arrives within `timeout`."""
    try:
        m = recv_until(ws, pred, timeout)
    except AssertionError:
        return
    raise AssertionError(f"unexpected frame {m}")


def wait_for(fn, timeout: float = 5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        v = fn()
        if v:
            return v
        time.sleep(0.02)
    raise AssertionError("condition not met in time")


def tick(client, offset: float) -> None:
    hub = hub_of(client)

    async def _tick():
        async with hub.lock:
            hub.tick(time.time() + offset)

    client.portal.call(_tick)


class FakeWorker:
    def __init__(self, client, wid, agents=("mock",), max_concurrent=1, running=(), path="/worker"):
        self.id = wid
        self._cm = client.websocket_connect(path)
        self.ws = self._cm.__enter__()
        self.send(type="hello", running=list(running), worker={
            "id": wid, "laptop": wid, "owner": "tester", "tailscale_ip": f"100.0.0.{len(wid)}",
            "agents": list(agents), "max_concurrent": max_concurrent, "version": "2"})
        OPEN_WORKERS.append(self)
        self.welcome = self.expect("welcome")

    def send(self, **msg):
        self.ws.send_json(msg)

    def expect(self, type_, timeout=5.0, **match):
        return recv_until(self.ws, lambda m: m.get("type") == type_ and all(
            (m.get(k) if k != "task_id" else m.get("task_id") or m.get("task", {}).get("id")) == v
            for k, v in match.items()), timeout)

    def close(self):
        if self in OPEN_WORKERS:
            OPEN_WORKERS.remove(self)
            self._cm.__exit__(None, None, None)


def api(client, method, url, **kw):
    r = client.request(method, url, **kw)
    return r.status_code, r.json()


def task_of(client, tid):
    return client.get(f"/api/tasks/{tid}").json()["task"]


PLAN_ITEMS = [
    {"ref": "T1", "title": "Scaffold", "description": "repo skeleton", "acceptance": ["builds"], "priority": 1},
    {"ref": "T2", "title": "API", "depends_on": ["T1"], "priority": 2},
    {"ref": "T3", "title": "UI", "depends_on": ["T1", "T3", "T99"], "priority": 2},
    {"ref": "T4", "title": "Docs", "depends_on": ["T5"], "priority": 4},
    {"ref": "T5", "title": "Polish", "depends_on": ["T4"], "priority": 3},
]


def plan_project(client, worker, repo=""):
    """Create a project, let `worker` plan it with PLAN_ITEMS; returns (project, plan run_task)."""
    project = client.post("/api/projects", json={"prompt": "Build a pomodoro web app with stats", "repo": repo}).json()
    run = worker.expect("run_task", task_id=project["plan_task_id"])
    worker.send(type="task_started", task_id=run["task"]["id"], agent=run["agent"], branch="", workspace="/tmp/x")
    worker.send(type="plan_result", task_id=run["task"]["id"], project_id=project["id"],
                title="Pomodoro app", summary="five tasks", tasks=PLAN_ITEMS)
    worker.send(type="task_finished", task_id=run["task"]["id"], ok=True, result="planned", error="",
                branch="", pr_url="", merged=False)
    wait_for(lambda: task_of(client, run["task"]["id"])["status"] == "done")
    return project, run


# ----------------------------------------------------------------------------- pure units
def test_planning_validation_and_mapping():
    with pytest.raises(PlanError):
        normalize_plan([])
    with pytest.raises(PlanError):
        normalize_plan([{"title": "  "}])
    with pytest.raises(PlanError):
        normalize_plan([{"title": f"t{i}"} for i in range(21)])
    items = normalize_plan(PLAN_ITEMS)
    planned, dropped = map_plan("p-1", items, 1, {})
    deps = {p.key: p.depends_on for p in planned}
    assert deps == {"T1": [], "T2": ["p-1-T1"], "T3": ["p-1-T1"], "T4": ["p-1-T5"], "T5": []}
    assert any("(self)" in d for d in dropped)
    assert any("(unknown)" in d for d in dropped)
    assert any("(cycle)" in d for d in dropped)
    # numbering continues after existing tasks; existing ids/keys are valid deps
    existing = {"p-1-T1": {"project_id": "p-1", "key": "T1"}}
    planned, _ = map_plan("p-1", normalize_plan([{"title": "x", "depends_on": ["p-1-T1"]}]), 2, existing)
    assert planned[0].task_id == "p-1-T2" and planned[0].depends_on == ["p-1-T1"]


def _t(tid, prio=2, created=0.0, kind="build", hint="", deps=(), assignee=None, status="todo"):
    return {"id": tid, "key": tid.split("-")[-1], "kind": kind, "priority": prio, "created_at": created,
            "agent_hint": hint, "depends_on": list(deps), "assignee": assignee, "status": status}


def test_scheduler_policy():
    tasks = {t["id"]: t for t in [
        _t("p-T10", prio=2), _t("p-T2", prio=2), _t("p-T3", prio=1), _t("p-PLAN", prio=0, kind="plan"),
    ]}
    a = schedule(tasks, [WorkerSlot("w1", ["mock"], 4, 0)], 0)
    assert [x.task_id for x in a] == ["p-PLAN", "p-T3", "p-T2", "p-T10"]  # plan, priority, natural key
    # agent_hint honoured when someone has the adapter, ignored when nobody does
    tasks = {t["id"]: t for t in [_t("p-T1", hint="codex"), _t("p-T2", hint="cursor", created=1)]}
    a = schedule(tasks, [WorkerSlot("w1", ["mock"], 1, 0), WorkerSlot("w2", ["claude-code", "codex"], 1, 0)], 0)
    got = {x.task_id: (x.worker_id, x.agent) for x in a}
    assert got["p-T1"] == ("w2", "codex")
    assert got["p-T2"] == ("w1", "mock")
    # locality wins a priority tie
    tasks = {t["id"]: t for t in [
        _t("p-T1", status="done", assignee={"worker_id": "w2"}),
        _t("p-T2", created=0), _t("p-T3", created=1, deps=["p-T1"]),
    ]}
    a = schedule(tasks, [WorkerSlot("w2", ["mock"], 1, 0)], 0)
    assert a[0].task_id == "p-T3"
    # holds delay re-assignment
    assert schedule({"p-T2": _t("p-T2")}, [WorkerSlot("w1", ["mock"], 1, 0)], 5, {"p-T2": 10}) == []


# ----------------------------------------------------------------------------- flows
def test_plan_dispatch_events_and_plan_result(client):
    with client.websocket_connect("/board") as board:
        snap = recv_json(board)
        assert snap["type"] == "snapshot" and snap["projects"] == [] and "workers" in snap

        status, project = api(client, "POST", "/api/projects",
                              json={"prompt": "Build a pomodoro web app with stats please", "repo": ""})
        assert status == 200 and project["status"] == "planning"
        assert project["title"] == "Build a pomodoro web app with"
        assert project["id"].startswith("p-") and len(project["id"]) == 6
        recv_until(board, lambda m: m["type"] == "project" and m["project"]["id"] == project["id"])

        w = FakeWorker(client, "endermite")
        assert w.welcome["worker_id"] == "endermite" and w.welcome["server_version"] == "2"
        run = w.expect("run_task")
        plan_id = project["plan_task_id"]
        assert run["task"]["id"] == plan_id and run["task"]["kind"] == "plan"
        assert run["task"]["status"] == "claimed" and run["task"]["attempts"] == 1
        assert run["agent"] == "mock" and run["token"] and run["server_http"] == "http://testserver"
        assert run["project"]["id"] == project["id"] and run["board"][0]["key"] == "PLAN"
        recv_until(board, lambda m: m["type"] == "task" and m["task"]["status"] == "claimed")
        recv_until(board, lambda m: m["type"] == "worker" and m["worker"]["status"] == "busy")

        w.send(type="task_started", task_id=plan_id, agent="mock", branch="", workspace="/tmp/ws")
        recv_until(board, lambda m: m["type"] == "task" and m["task"]["status"] == "in_progress")
        w.send(type="task_event", task_id=plan_id, event={"ts": 1.0, "kind": "assistant", "text": "x" * 5000})
        w.send(type="task_event", task_id=plan_id, events=[
            {"kind": "tool_call", "text": "Bash: ls\nmore", "tool": "Bash", "data": {"cmd": "ls"}},
            {"kind": "thinking", "text": "hmm", "data": {"big": "y" * 9000}},
        ])
        ev = recv_until(board, lambda m: m["type"] == "event" and m["event"]["kind"] == "thinking")
        assert ev["task_id"] == plan_id
        w.send(type="task_progress", task_id=plan_id, progress=140, note="reading repo")
        w.send(type="task_usage", task_id=plan_id, tokens={"input": 10, "output": 5})
        w.send(type="bogus_type")  # ignored

        detail = wait_for(lambda: (d := client.get(f"/api/tasks/{plan_id}").json())["task"]["tokens"]["input"] == 10 and d)
        events, task = detail["events"], detail["task"]
        seqs = [e["seq"] for e in events]
        assert seqs == list(range(1, len(events) + 1))
        long = next(e for e in events if e["kind"] == "assistant")
        assert len(long["text"]) == 4000 and long["text"].endswith("…") and long["ts"] == 1.0
        assert next(e for e in events if e["kind"] == "thinking")["data"] == {"truncated": True}
        assert task["event_count"] == len(events) and task["last_event"] == "hmm"
        assert task["progress"] == 100 and task["progress_note"] == "reading repo"
        assert "events" not in task

        w.send(type="plan_result", task_id=plan_id, project_id=project["id"],
               title="Pomodoro app", summary="five tasks", tasks=PLAN_ITEMS)
        recv_until(board, lambda m: m["type"] == "project" and m["project"]["status"] == "active")
        tasks = {t["key"]: t for t in client.get(f"/api/projects/{project['id']}").json()["tasks"]}
        assert tasks["PLAN"]["status"] == "done"
        assert {k: tasks[k]["status"] for k in ("T1", "T2", "T3", "T4", "T5")} == {
            "T1": "todo", "T2": "backlog", "T3": "backlog", "T4": "backlog", "T5": "todo"}
        assert tasks["T3"]["depends_on"] == [f"{project['id']}-T1"]
        assert tasks["T4"]["depends_on"] == [f"{project['id']}-T5"] and tasks["T5"]["depends_on"] == []
        assert tasks["T1"]["acceptance"] == ["builds"] and tasks["T1"]["priority"] == 1
        proj = client.get(f"/api/projects/{project['id']}").json()["project"]
        assert proj["title"] == "Pomodoro app" and proj["summary"] == "five tasks"
        # duplicate plan (e.g. via ov plan, then worker) is ignored
        w.send(type="plan_result", task_id=plan_id, project_id=project["id"], title="x", summary="", tasks=PLAN_ITEMS)

        # the planner exits -> slot frees -> highest-priority todo build task (T1) is next
        w.send(type="task_finished", task_id=plan_id, ok=True, result="planned", error="", branch="", pr_url="", merged=False)
        run2 = w.expect("run_task")
        assert run2["task"]["key"] == "T1" and run2["token"] != run["token"]
        assert len(client.get(f"/api/projects/{project['id']}").json()["tasks"]) == 6
        w.close()


def test_invalid_plan_result_retries_then_fails(make_client):
    client = make_client(max_attempts=2)
    project = client.post("/api/projects", json={"prompt": "do it"}).json()
    w = FakeWorker(client, "w1")
    run = w.expect("run_task")
    w.send(type="plan_result", task_id=run["task"]["id"], project_id=project["id"], title="", summary="", tasks=[])
    w.send(type="task_finished", task_id=run["task"]["id"], ok=True, result="", error="", branch="", pr_url="", merged=False)
    run = w.expect("run_task")  # retried (attempt 2)
    assert run["task"]["attempts"] == 2
    events = client.get(f"/api/tasks/{run['task']['id']}").json()["events"]
    assert any("plan rejected" in e["text"] for e in events)
    assert any("invalid plan" in e["text"] and e["kind"] == "error" for e in events)
    w.send(type="task_finished", task_id=run["task"]["id"], ok=True, result="", error="", branch="", pr_url="", merged=False)
    plan = wait_for(lambda: (t := task_of(client, run["task"]["id"]))["status"] == "failed" and t)
    assert "without submitting a plan" in plan["error"]
    assert client.get(f"/api/projects/{project['id']}").json()["project"]["status"] == "failed"
    w.close()


def test_build_task_finish_paths(make_client):
    client = make_client(max_attempts=2)
    w = FakeWorker(client, "w1")
    project, _ = plan_project(client, w, repo="acme/pomodoro")
    pid = project["id"]

    # T1: ok + merged -> done, dependents leave backlog, system message posted
    run = w.expect("run_task", task_id=f"{pid}-T1")
    w.send(type="task_started", task_id=run["task"]["id"], agent="mock", branch="ov/t1", workspace="")
    w.send(type="task_status", task_id=run["task"]["id"], status="review", note="pushing")
    wait_for(lambda: task_of(client, run["task"]["id"])["status"] == "review")
    w.send(type="task_finished", task_id=run["task"]["id"], ok=True, result="did it", error="",
           branch="ov/t1", pr_url="https://github.com/acme/pomodoro/pull/1", merged=True)
    t1 = wait_for(lambda: (t := task_of(client, f"{pid}-T1"))["status"] == "done" and t)
    assert t1["pr_url"].endswith("/pull/1") and t1["result"] == "did it" and t1["finished_at"]
    assert task_of(client, f"{pid}-T2")["status"] in ("todo", "claimed")
    msgs = client.get("/api/state").json()["messages"]
    assert any(m["from"]["kind"] == "system" and "T1" in m["text"] for m in msgs)

    # next (T2, priority 2 + natural order) : ok, not merged -> review; human marks done
    run = w.expect("run_task")
    tid = run["task"]["id"]
    assert run["task"]["key"] == "T2"
    w.send(type="task_started", task_id=tid, agent="mock", branch="b", workspace="")
    w.send(type="task_finished", task_id=tid, ok=True, result="pr open", error="", branch="b",
           pr_url="https://github.com/x/y/pull/2", merged=False)
    wait_for(lambda: task_of(client, tid)["status"] == "review")
    status, body = api(client, "POST", f"/api/tasks/{tid}/done")
    assert status == 200 and body["status"] == "done"

    # T3: fails -> todo (retry), fails again -> failed (max_attempts=2); human retry resets
    run = w.expect("run_task")
    tid = run["task"]["id"]
    assert run["task"]["key"] == "T3"
    w.send(type="task_finished", task_id=tid, ok=False, result="", error="tests failed", branch="", pr_url="", merged=False)
    run = w.expect("run_task", task_id=tid)
    assert run["task"]["attempts"] == 2
    w.send(type="task_finished", task_id=tid, ok=False, result="", error="still failing", branch="", pr_url="", merged=False)
    t3 = wait_for(lambda: (t := task_of(client, tid))["status"] == "failed" and t)
    assert t3["error"] == "still failing" and t3["attempts"] == 2
    run5 = w.expect("run_task")
    assert run5["task"]["key"] == "T5"
    status, body = api(client, "POST", f"/api/tasks/{tid}/retry")
    assert status == 200 and body["attempts"] == 0 and body["error"] == "" and body["status"] == "todo"
    w.send(type="task_finished", task_id=run5["task"]["id"], ok=True, result="", error="", branch="",
           pr_url="", merged=True)
    run = w.expect("run_task", task_id=tid)  # T3 (prio 2) beats the freshly promoted T4 (prio 4)
    assert run["task"]["attempts"] == 1
    w.close()


def test_local_mode_done_and_project_done(client):
    w = FakeWorker(client, "w1", max_concurrent=5)
    project, _ = plan_project(client, w, repo="")
    pid = project["id"]
    seen = set()
    for _ in range(20):
        tasks = client.get(f"/api/projects/{pid}").json()["tasks"]
        if all(t["status"] == "done" for t in tasks):
            break
        run = w.expect("run_task")
        tid = run["task"]["id"]
        seen.add(run["task"]["key"])
        w.send(type="task_started", task_id=tid, agent="mock", branch="", workspace="")
        w.send(type="task_finished", task_id=tid, ok=True, result="ok", error="", branch="", pr_url="", merged=False)
        wait_for(lambda: task_of(client, tid)["status"] == "done")
    assert seen == {"T1", "T2", "T3", "T4", "T5"}
    wait_for(lambda: client.get(f"/api/projects/{pid}").json()["project"]["status"] == "done")
    w.close()


def test_worker_disconnect_requeues_and_reconnect_is_cancelled(client):
    project = client.post("/api/projects", json={"prompt": "x y z"}).json()
    plan_id = project["plan_task_id"]
    a = FakeWorker(client, "alpha")
    a.expect("run_task", task_id=plan_id)
    a.send(type="task_started", task_id=plan_id, agent="mock", branch="", workspace="")
    b = FakeWorker(client, "bravo")
    a.close()
    run = b.expect("run_task", task_id=plan_id)
    assert run["task"]["attempts"] == 2 and run["task"]["assignee"]["worker_id"] == "bravo"
    events = client.get(f"/api/tasks/{plan_id}").json()["events"]
    assert any("alpha disconnected" in e["text"] for e in events)
    workers = {x["id"]: x for x in client.get("/api/workers").json()}
    assert workers["alpha"]["status"] == "offline" and workers["bravo"]["status"] == "busy"
    # alpha comes back still claiming the task: it now belongs to bravo -> cancel_task
    a2 = FakeWorker(client, "alpha", running=[plan_id])
    cancel = a2.expect("cancel_task")
    assert cancel["task_id"] == plan_id and "bravo" in cancel["reason"]
    a2.close()
    b.close()


def test_reconnect_reattaches_within_grace(make_client):
    client = make_client(reattach_grace=30.0)
    project = client.post("/api/projects", json={"prompt": "x y z"}).json()
    plan_id = project["plan_task_id"]
    a = FakeWorker(client, "alpha")
    token = a.expect("run_task")["token"]
    a.send(type="task_started", task_id=plan_id, agent="mock", branch="", workspace="")
    a.close()
    wait_for(lambda: task_of(client, plan_id)["status"] == "todo")
    b = FakeWorker(client, "bravo")
    no_frame(b.ws, lambda m: m.get("type") == "run_task")  # held for alpha
    a2 = FakeWorker(client, "alpha", running=[plan_id])
    t = wait_for(lambda: (t := task_of(client, plan_id))["status"] == "in_progress" and t)
    assert t["assignee"]["worker_id"] == "alpha" and t["attempts"] == 1
    # the agent's token survived the reconnect
    r = client.get("/api/agent/board", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200
    a2.close()
    b.close()


def test_claim_timeout(make_client):
    client = make_client(offline_after=1000.0)
    project = client.post("/api/projects", json={"prompt": "x y z"}).json()
    plan_id = project["plan_task_id"]
    w = FakeWorker(client, "w1")
    w.expect("run_task", task_id=plan_id)
    # claimed for > 30 s without task_started -> cancel_task + back to todo (+ short settle hold)
    tick(client, 31)
    assert w.expect("cancel_task")["task_id"] == plan_id
    assert task_of(client, plan_id)["status"] == "todo"
    tick(client, 40)
    run = w.expect("run_task", task_id=plan_id)
    assert run["task"]["attempts"] == 2
    # a late message about attempt 1 is ignored once the worker echoes `attempt`
    w.send(type="task_finished", task_id=plan_id, attempt=1, ok=False, error="killed", result="",
           branch="", pr_url="", merged=False)
    w.send(type="task_started", task_id=plan_id, attempt=2, agent="mock", branch="", workspace="")
    t = wait_for(lambda: (t := task_of(client, plan_id))["status"] == "in_progress" and t)
    assert t["attempts"] == 2 and t["error"] == ""
    w.close()


def test_offline_detection(make_client):
    client = make_client(max_attempts=1)
    project = client.post("/api/projects", json={"prompt": "x y z"}).json()
    plan_id = project["plan_task_id"]
    w = FakeWorker(client, "w1")
    w.expect("run_task", task_id=plan_id)
    w.send(type="task_started", task_id=plan_id, agent="mock", branch="", workspace="")
    wait_for(lambda: task_of(client, plan_id)["status"] == "in_progress")
    # silent for > 15 s -> offline: socket closed, task requeued -> failed (attempts exhausted)
    tick(client, 16)
    with pytest.raises(WebSocketDisconnect):
        recv_until(w.ws, lambda m: False)
    t = task_of(client, plan_id)
    assert t["status"] == "failed" and "went offline" in t["error"]
    assert client.get("/api/workers").json()[0]["status"] == "offline"
    assert client.get(f"/api/projects/{project['id']}").json()["project"]["status"] == "failed"
    w.close()


def test_agent_api(client):
    w = FakeWorker(client, "w1", max_concurrent=2)
    project, _ = plan_project(client, w)
    pid = project["id"]
    run1 = w.expect("run_task")
    run2 = w.expect("run_task")
    tok1, tok2 = run1["token"], run2["token"]
    t1, t2 = run1["task"]["id"], run2["task"]["id"]
    for t in (t1, t2):
        w.send(type="task_started", task_id=t, agent="mock", branch="", workspace="")
    h1 = {"Authorization": f"Bearer {tok1}"}
    h2 = {"Authorization": f"Bearer {tok2}"}

    for method, url in [("POST", "/api/agent/say"), ("GET", "/api/agent/inbox"), ("POST", "/api/agent/progress"),
                        ("GET", "/api/agent/board"), ("POST", "/api/agent/plan")]:
        status, body = api(client, method, url, json={} if method == "POST" else None)
        assert status == 401 and body["error"]["code"] == "unauthorized"
    status, _ = api(client, "GET", "/api/agent/board", headers={"Authorization": "Bearer nope"})
    assert status == 401

    with client.websocket_connect("/board") as board:
        recv_json(board)
        status, msg = api(client, "POST", "/api/agent/say", headers=h1, json={"text": "exposing useTimer()"})
        assert status == 200 and msg["id"].startswith("m-") and msg["to"] == "all"
        assert msg["from"] == {"kind": "agent", "task_id": t1, "worker_id": "w1", "agent": "mock",
                               "name": f"{run1['task']['key']} · mock@w1"}
        frame = recv_until(board, lambda m: m["type"] == "message")
        assert frame["message"]["id"] == msg["id"]
    chat = w.expect("chat")  # the worker runs tasks of this project -> forwarded
    assert chat["message"]["id"] == msg["id"]

    # inbox: own messages excluded; others' visible; cursor advances
    status, inbox = api(client, "GET", "/api/agent/inbox", headers=h1)
    assert status == 200 and all(m["from"].get("task_id") != t1 for m in inbox["messages"])
    status, inbox2 = api(client, "GET", "/api/agent/inbox", headers=h2)
    assert msg["id"] in [m["id"] for m in inbox2["messages"]]
    cursor = inbox2["cursor"]
    status, empty = api(client, "GET", f"/api/agent/inbox?since={cursor}", headers=h2)
    assert empty["messages"] == [] and empty["cursor"] == cursor
    # direct message to t2 is visible to t2 only
    api(client, "POST", "/api/agent/say", headers=h1, json={"text": "psst", "to": t2})
    assert [m["text"] for m in api(client, "GET", f"/api/agent/inbox?since={cursor}", headers=h2)[1]["messages"]] == ["psst"]

    # long-poll wakes up on a new human message
    cursor = api(client, "GET", "/api/agent/inbox", headers=h2)[1]["cursor"]
    result = {}

    def poll():
        t0 = time.monotonic()
        result["r"] = client.get(f"/api/agent/inbox?since={cursor}&wait=10", headers=h2).json()
        result["dt"] = time.monotonic() - t0

    th = threading.Thread(target=poll)
    th.start()
    time.sleep(0.3)
    status, hmsg = api(client, "POST", "/api/messages", json={"project_id": pid, "text": "hi agents", "name": "carlo"})
    assert status == 200 and hmsg["from"] == {"kind": "human", "name": "carlo"}
    th.join(5)
    assert [m["text"] for m in result["r"]["messages"]] == ["hi agents"] and result["dt"] < 5
    # wait with nothing new returns empty after the wait
    t0 = time.monotonic()
    r = client.get(f"/api/agent/inbox?since={result['r']['cursor']}&wait=0.3", headers=h2).json()
    assert r["messages"] == [] and 0.25 < time.monotonic() - t0 < 3

    # progress + board
    status, task = api(client, "POST", "/api/agent/progress", headers=h1, json={"progress": 40, "note": "half way"})
    assert status == 200 and task["progress"] == 40 and task["progress_note"] == "half way"
    status, board = api(client, "GET", "/api/agent/board", headers=h1)
    assert board["project"]["id"] == pid and len(board["tasks"]) == 6
    assert {"status", "assignee", "progress_note", "branch"} <= set(board["tasks"][0])

    # plan only for the PLAN task
    status, body = api(client, "POST", "/api/agent/plan", headers=h1, json={"tasks": PLAN_ITEMS})
    assert status == 403 and body["error"]["code"] == "not_planner"
    w.close()


def test_agent_plan_endpoint(client):
    project = client.post("/api/projects", json={"prompt": "make a game"}).json()
    w = FakeWorker(client, "w1")
    run = w.expect("run_task", task_id=project["plan_task_id"])
    h = {"Authorization": f"Bearer {run['token']}"}
    status, body = api(client, "POST", "/api/agent/plan", headers=h, json={"title": "Game", "tasks": [{"title": ""}]})
    assert status == 400 and body["error"]["code"] == "invalid_plan"
    status, body = api(client, "POST", "/api/agent/plan", headers=h,
                       json={"title": "Game", "summary": "s", "tasks": [{"ref": "T1", "title": "Engine"}, {"title": "Art", "depends_on": ["T1"]}]})
    assert status == 200 and [t["key"] for t in body["tasks"]] == ["T1", "T2"]
    assert body["project"]["title"] == "Game" and body["project"]["status"] == "active"
    assert task_of(client, project["plan_task_id"])["status"] == "done"
    status, body = api(client, "POST", "/api/agent/plan", headers=h, json={"tasks": [{"title": "again"}]})
    assert status == 409
    # the planner can still talk after its plan was accepted
    assert api(client, "POST", "/api/agent/say", headers=h, json={"text": "plan is up"})[0] == 200
    # task_finished after `ov plan` just frees the slot; the next task is dispatched
    w.send(type="task_finished", task_id=project["plan_task_id"], ok=True, result="", error="", branch="", pr_url="", merged=False)
    assert w.expect("run_task")["task"]["key"] == "T1"
    w.close()


def test_human_endpoints(client):
    project = client.post("/api/projects", json={"prompt": "p", "repo": "https://github.com/acme/demo.git"}).json()
    pid = project["id"]
    assert project["repo"] == "acme/demo" and project["base_branch"] == "main"
    assert api(client, "POST", "/api/projects", json={"prompt": ""})[1]["error"]["code"] == "bad_request"
    assert api(client, "POST", "/api/projects", json={"prompt": "x", "repo": "not a repo"})[0] == 400
    assert api(client, "POST", "/api/projects", content=b"[1,2]")[1]["error"]["code"] == "bad_json"
    assert api(client, "GET", "/api/projects/p-none")[1]["error"]["code"] == "project_not_found"
    assert api(client, "GET", "/api/tasks/nope")[1] == {"error": {"code": "task_not_found", "message": "no task 'nope'"}}

    status, t1 = api(client, "POST", f"/api/projects/{pid}/tasks", json={"title": "First", "priority": 9})
    assert status == 200 and t1["key"] == "T1" and t1["status"] == "todo" and t1["priority"] == 4
    status, t2 = api(client, "POST", f"/api/projects/{pid}/tasks", json={"title": "Second", "depends_on": ["T1"]})
    assert t2["status"] == "backlog" and t2["depends_on"] == [t1["id"]]
    assert api(client, "POST", f"/api/projects/{pid}/tasks", json={"title": "x", "depends_on": ["T42"]})[0] == 400
    assert api(client, "POST", f"/api/projects/{pid}/tasks", json={"title": " "})[0] == 400

    # cancel a dependency -> dependents promoted (cancelled counts as satisfied)
    status, body = api(client, "POST", f"/api/tasks/{t1['id']}/cancel")
    assert body["status"] == "cancelled"
    assert task_of(client, t2["id"])["status"] == "todo"
    # cancel a running task -> cancel_task to its worker
    w = FakeWorker(client, "w1")
    run = w.expect("run_task", task_id=project["plan_task_id"])
    api(client, "POST", f"/api/tasks/{run['task']['id']}/cancel")
    assert w.expect("cancel_task")["task_id"] == run["task"]["id"]
    assert client.get(f"/api/projects/{pid}").json()["project"]["status"] in ("active", "failed")
    # the freed slot picks up T2
    w.expect("run_task", task_id=t2["id"])
    status, body = api(client, "POST", f"/api/tasks/{t2['id']}/done")
    assert body["status"] == "done"
    assert w.expect("cancel_task")["task_id"] == t2["id"]
    assert api(client, "POST", f"/api/tasks/{t2['id']}/cancel")[0] == 409
    assert client.get(f"/api/projects/{pid}").json()["project"]["status"] == "done"
    # retry of the cancelled plan task is fine; retry of an applied plan is refused
    assert api(client, "POST", f"/api/tasks/{project['plan_task_id']}/retry")[1]["status"] == "todo"

    status, m = api(client, "POST", "/api/messages", json={"project_id": pid, "text": "hello", "to": "all"})
    assert status == 200 and m["from"]["name"] == "human"
    assert api(client, "POST", "/api/messages", json={"project_id": pid, "text": ""})[0] == 400
    assert api(client, "POST", "/api/messages", json={"project_id": "p-zzzz", "text": "x"})[0] == 404
    state = client.get("/api/state").json()
    assert {"projects", "tasks", "workers", "messages", "laptops"} <= set(state)
    assert all("events" not in t for t in state["tasks"])
    assert [p["id"] for p in client.get("/api/projects").json()] == [pid]
    w.close()


def test_retry_of_applied_plan_is_refused(client):
    w = FakeWorker(client, "w1")
    project, _ = plan_project(client, w)
    status, body = api(client, "POST", f"/api/tasks/{project['plan_task_id']}/retry")
    assert status == 409 and body["error"]["code"] == "already_planned"
    w.close()


def test_board_ping_and_reconnect_snapshot(make_client):
    client = make_client(ping_interval=0.1)
    client.post("/api/projects", json={"prompt": "abc"})
    with client.websocket_connect("/board") as b1, client.websocket_connect("/board") as b2:
        s1 = recv_json(b1)
        assert s1["type"] == "snapshot" and len(s1["projects"]) == 1 and len(s1["tasks"]) == 1
        recv_json(b2)
        assert recv_until(b1, lambda m: m["type"] == "ping")["ts"] > 0
        # one board going away does not disturb the other
    with client.websocket_connect("/board") as b3:
        assert recv_json(b3)["type"] == "snapshot"
        client.post("/api/projects", json={"prompt": "def"})
        recv_until(b3, lambda m: m["type"] == "project")


def test_persistence_and_restart_reattach(make_client, tmp_path):
    client = make_client(reattach_grace=30.0)
    project = client.post("/api/projects", json={"prompt": "persist me"}).json()
    w = FakeWorker(client, "w1")
    run = w.expect("run_task")
    tid = run["task"]["id"]
    w.send(type="task_started", task_id=tid, agent="mock", branch="", workspace="")
    w.send(type="task_event", task_id=tid, event={"kind": "assistant", "text": "working"})
    wait_for(lambda: task_of(client, tid)["event_count"] >= 3)
    client.post("/api/messages", json={"project_id": project["id"], "text": "note"})
    hub = hub_of(client)

    async def snapshot_while_running():  # what a crash mid-run leaves on disk
        async with hub.lock:
            return hub.store.serialize()

    payload = client.portal.call(snapshot_while_running)
    close_client(client)
    data_dir = tmp_path / "data"
    hub.store.write(payload)
    assert not list(data_dir.glob("*.tmp"))
    assert json.loads((data_dir / "state.json").read_text())["tasks"][0]["status"] == "in_progress"

    client = make_client(reattach_grace=30.0)
    t = task_of(client, tid)
    assert t["status"] == "todo" and t["attempts"] == 1 and t["assignee"] is None
    events = client.get(f"/api/tasks/{tid}").json()["events"]
    assert any(e["text"] == "working" for e in events) and "hub restarted" in events[-1]["text"]
    assert [e["seq"] for e in events] == list(range(1, len(events) + 1))
    assert client.get("/api/state").json()["messages"][-1]["text"] == "note"
    assert client.get("/api/workers").json()[0]["status"] == "offline"
    # the worker re-hellos still running it -> re-attached, same token still valid
    w = FakeWorker(client, "w1", running=[tid])
    t = wait_for(lambda: (t := task_of(client, tid))["status"] == "in_progress" and t)
    assert t["assignee"]["worker_id"] == "w1" and t["attempts"] == 1
    assert client.get("/api/agent/board", headers={"Authorization": f"Bearer {run['token']}"}).status_code == 200
    w.close()


def test_restart_keeps_task_for_its_worker(make_client, tmp_path):
    """Graceful shutdown requeues on disconnect; after the restart the old worker (slow to
    reconnect) must still get its task back, and its queued finish must be accepted."""
    client = make_client(reattach_grace=0.2, restart_grace=30.0)
    client.post("/api/projects", json={"prompt": "restart me"})
    w = FakeWorker(client, "w1")
    tid = w.expect("run_task")["task"]["id"]
    w.send(type="task_started", task_id=tid, agent="mock", branch="", workspace="")
    wait_for(lambda: task_of(client, tid)["status"] == "in_progress")
    w.close()  # what uvicorn's shutdown does to every worker socket before the lifespan ends
    wait_for(lambda: task_of(client, tid)["status"] == "todo")
    hub = hub_of(client)

    async def snapshot():
        async with hub.lock:
            return hub.store.serialize()

    payload = client.portal.call(snapshot)
    close_client(client)
    hub.store.write(payload)

    client = make_client(reattach_grace=0.2, restart_grace=30.0)
    other = FakeWorker(client, "w2")  # an idle worker that reconnects first
    time.sleep(0.6)  # longer than reattach_grace
    assert task_of(client, tid)["status"] == "todo"
    w = FakeWorker(client, "w1", running=[tid])  # task finished while the hub was down
    wait_for(lambda: task_of(client, tid)["status"] == "in_progress")
    assert task_of(client, tid)["assignee"]["worker_id"] == "w1"
    w.send(type="task_finished", task_id=tid, ok=False, error="planner crashed", result="", branch="",
           pr_url="", merged=False)
    events = wait_for(lambda: (ev := client.get(f"/api/tasks/{tid}").json()["events"])
                      and any("attempt 1 failed: planner crashed" in e["text"] for e in ev) and ev)
    assert not any("not assigned" in e["text"] for e in events)
    other.close()
    w.close()


def test_shared_secret(make_client):
    client = make_client(shared_secret="s3cret")
    with pytest.raises(WebSocketDisconnect):
        FakeWorker(client, "w1")
    w = FakeWorker(client, "w1", path="/worker?secret=s3cret")
    assert w.welcome["worker_id"] == "w1"
    w.close()


def test_public_url_overrides_server_http(make_client):
    client = make_client(public_url="http://100.1.2.3:8000")
    client.post("/api/projects", json={"prompt": "abc"})
    w = FakeWorker(client, "w1")
    assert w.expect("run_task")["server_http"] == "http://100.1.2.3:8000"
    w.close()


def test_static_and_legacy(client):
    assert client.get("/").text == "<h1>board</h1>"
    assert client.get("/ar").text == "<h1>ar</h1>"
    assert client.get("/ar.html").text == "<h1>ar</h1>"
    assert "board" in client.get("/board.js").text
    status, body = api(client, "GET", "/api/does-not-exist")
    assert status == 404 and body["error"]["code"] == "not_found"
    # no MISTRAL_API_KEY in tests -> task-title summarize() returns "" (ported from kazy/ar-fixes)
    assert client.post("/summarize", json={"last_log": "x" * 300}).json()["summary"] == ""

    project = client.post("/api/projects", json={"prompt": "legacy view"}).json()
    w = FakeWorker(client, "w1")
    w.expect("run_task")
    with client.websocket_connect("/ws") as rep:
        rep.send_json({"laptop": "w1-mbp", "owner": "kay", "tailscale_ip": "100.0.0.2", "ts": time.time(),
                       "agents": [{"name": "vibe", "pid": 1, "cpu": 1.0, "cwd": "/", "last_log": "editing", "summary": ""}]})
        rep.send_json({"laptop": "other", "owner": "zed", "tailscale_ip": "100.9.9.9", "ts": time.time(), "agents": []})
        with client.websocket_connect("/view") as view:
            def merged(m):
                laps = {lap["tailscale_ip"]: lap for lap in m["laptops"]}
                return m["type"] == "state" and "100.0.0.2" in laps and len(laps["100.0.0.2"]["agents"]) == 2 and "100.9.9.9" in laps
            snap = recv_until(view, merged)
            lap = next(lap for lap in snap["laptops"] if lap["tailscale_ip"] == "100.0.0.2")
            assert lap["laptop"] == "w1-mbp"  # merged into the reporter entry
            synth = lap["agents"][1]
            assert synth["name"] == "mock" and synth["pid"] == 0
            assert synth["summary"] == "PLAN · Plan: legacy view — 0%"
    laptops = client.get("/api/state").json()["laptops"]
    assert any(lap["tailscale_ip"] == "100.0.0.2" for lap in laptops)
    assert project["id"]
    w.close()
