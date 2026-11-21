"""POST /api/control: orchestra task cards (cancel/retry/message) and reporter forwarding."""
from __future__ import annotations

import threading
import time

from test_server import FakeWorker, api, client, make_client, task_of  # noqa: F401  (fixtures)


def test_control_task_card(client):
    project = client.post("/api/projects", json={"prompt": "control me"}).json()
    w = FakeWorker(client, "w1")
    run = w.expect("run_task")
    tid = run.get("task_id") or run["task"]["id"]
    lap = next(lap for lap in client.get("/api/state").json()["laptops"] if lap["laptop"] == "w1")
    card = lap["agents"][0]
    assert card["kind"] == "task" and card["task_id"] == tid

    status, body = api(client, "POST", "/api/control", json={"task_id": tid, "action": "say", "text": "wait for me"})
    assert status == 200 and body["ok"]
    assert any(m["to"] == tid and m["text"] == "wait for me" for m in client.get("/api/state").json()["messages"])

    status, body = api(client, "POST", "/api/control", json={"task_id": tid, "action": "stop"})
    assert status == 200 and body["ok"]
    w.expect("cancel_task", task_id=tid)
    assert task_of(client, tid)["status"] == "cancelled"

    status, body = api(client, "POST", "/api/control", json={"task_id": tid, "action": "resume"})
    assert status == 200 and body["ok"] and task_of(client, tid)["status"] in ("todo", "claimed")

    assert api(client, "POST", "/api/control", json={"task_id": tid, "action": "rm -rf"})[0] == 400
    assert project["id"]
    w.close()


def test_control_forwarded_to_reporter(client):
    with client.websocket_connect("/ws") as rep:
        rep.send_json({"laptop": "kay-mbp", "owner": "kay", "tailscale_ip": "100.0.0.7", "ts": time.time(),
                       "agents": [{"name": "codex · demo", "pid": 1, "cpu": 0, "cwd": "/", "last_log": "",
                                   "summary": "", "kind": "session"}]})
        seen = {}

        def answer():
            msg = rep.receive_json()
            seen.update(msg)
            rep.send_json({"type": "control_result", "id": msg["id"], "ok": True, "message": "sent Ctrl-C"})

        th = threading.Thread(target=answer)
        th.start()
        status, body = api(client, "POST", "/api/control",
                           json={"laptop": "100.0.0.7", "agent": "codex · demo", "action": "stop"})
        th.join(5)
        assert status == 200 and body == {"ok": True, "message": "sent Ctrl-C"}
        assert seen["type"] == "control" and seen["agent"] == "codex · demo" and seen["action"] == "stop"
        assert "pid" not in seen
    assert api(client, "POST", "/api/control", json={"laptop": "nope", "agent": "x", "action": "stop"})[0] == 404


def test_control_secret_locks_mutations_but_not_viewing(make_client):
    """OV_CONTROL_SECRET set: /api/control + task cancel/retry need ?key= or X-OV-Key;
    viewing (/api/state, /ar) stays open."""
    c = make_client(control_secret="s3cret")
    # viewing stays open, no key needed
    assert c.get("/api/state").status_code == 200
    assert c.get("/ar").status_code == 200
    # control endpoints locked without the key
    assert c.post("/api/control", json={"action": "stop"}).status_code == 403
    assert c.post("/api/tasks/t1/cancel").status_code == 403
    assert c.post("/api/tasks/t1/retry").status_code == 403
    # wrong key (query or header) still 403
    assert c.post("/api/control?key=nope", json={"action": "stop"}).status_code == 403
    assert c.post("/api/control", json={"action": "stop"}, headers={"X-OV-Key": "nope"}).status_code == 403
    # correct key via query param passes auth (404: no such laptop reporter)
    r = c.post("/api/control?key=s3cret", json={"action": "stop", "laptop": "x", "agent": "y"})
    assert r.status_code == 404
    # correct key via header passes auth (400: bad action)
    r = c.post("/api/control", json={"action": "bogus"}, headers={"X-OV-Key": "s3cret"})
    assert r.status_code == 400
    # cancel/retry with the key are not 403
    assert c.post("/api/tasks/t1/cancel?key=s3cret").status_code != 403
    assert c.post("/api/tasks/t1/retry", headers={"X-OV-Key": "s3cret"}).status_code != 403


def test_control_open_when_no_secret(make_client):
    """Without OV_CONTROL_SECRET nothing is locked (backwards compatible)."""
    c = make_client()
    assert c.post("/api/control", json={"action": "stop", "laptop": "x", "agent": "y"}).status_code == 404


def test_focus_forwarded_and_locked(make_client):
    """'focus' goes to the reporter like the other actions and needs the key; task cards have no window."""
    c = make_client(control_secret="s3cret")
    body = {"laptop": "100.0.0.8", "agent": "cursor", "action": "focus"}
    assert c.post("/api/control", json=body).status_code == 403
    with c.websocket_connect("/ws") as rep:
        rep.send_json({"laptop": "kay-mbp", "owner": "kay", "tailscale_ip": "100.0.0.8", "ts": time.time(),
                       "agents": [{"name": "cursor", "pid": 1, "cpu": 0, "cwd": "", "last_log": "", "summary": "",
                                   "kind": "app"}]})
        seen = {}

        def answer():
            msg = rep.receive_json()
            seen.update(msg)
            rep.send_json({"type": "control_result", "id": msg["id"], "ok": True, "message": "focused Cursor"})

        th = threading.Thread(target=answer)
        th.start()
        r = c.post("/api/control?key=s3cret", json=body)
        th.join(5)
        assert r.status_code == 200 and r.json() == {"ok": True, "message": "focused Cursor"}
        assert seen["action"] == "focus" and seen["agent"] == "cursor"
    r = c.post("/api/control", json={"task_id": "t-1", "action": "focus"}, headers={"X-OV-Key": "s3cret"})
    assert r.status_code == 200 and r.json()["ok"] is False
