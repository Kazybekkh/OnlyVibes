"""The `ov` CLI against a stub of the hub's agent API."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time

from conftest import WORKER_DIR, free_port

OV = os.path.join(WORKER_DIR, "ov.py")


def ov(hub_url: str, *args, state_dir=None, token="tok-1", timeout=30) -> subprocess.CompletedProcess:
    env = {k: v for k, v in os.environ.items() if not k.startswith("OV_")}
    env.update({"OV_SERVER": hub_url, "OV_TOKEN": token, "OV_TASK_ID": "p-1-T1"})
    if state_dir:
        env["OV_STATE_DIR"] = str(state_dir)
    return subprocess.run([sys.executable, OV, *args], env=env, capture_output=True, text=True, timeout=timeout)


def test_say(stub_hub, tmp_path):
    hub = stub_hub()
    r = ov(hub.url, "say", "hello team", "--to", "p-1-T2", "--reply-to", "m-000001", state_dir=tmp_path)
    assert r.returncode == 0, r.stderr
    assert "sent m-000001 to p-1-T2" in r.stdout
    req = hub.requests[-1]
    assert req["path"] == "/api/agent/say" and req["auth"] == "Bearer tok-1"
    assert req["body"] == {"text": "hello team", "to": "p-1-T2", "reply_to": "m-000001"}


def test_say_to_own_task_goes_to_all(stub_hub, tmp_path):
    # seen live: codex answered a human with `--to <its own task id>`
    hub = stub_hub()
    r = ov(hub.url, "say", "done!", "--to", "p-1-T1", "--reply-to", "m-000102", state_dir=tmp_path)
    assert r.returncode == 0, r.stderr
    assert hub.requests[-1]["body"] == {"text": "done!", "to": "all", "reply_to": "m-000102"}
    assert "own task id" in r.stderr


def test_inbox_keeps_cursor(stub_hub, tmp_path):
    hub = stub_hub()
    r = ov(hub.url, "inbox", state_dir=tmp_path)
    assert r.returncode == 0, r.stderr
    assert "please export useTimer()" in r.stdout and "carlo" in r.stdout
    assert "ov say --to p-1-T2 --reply-to m-000101" in r.stdout
    assert "[reply with: ov say --reply-to m-000102" in r.stdout  # human sender: no --to
    assert (tmp_path / "inbox.cursor").read_text() == "m-000102"
    r = ov(hub.url, "inbox", "--wait", "2", state_dir=tmp_path)
    assert r.returncode == 0 and "no new messages" in r.stdout
    assert hub.requests[-1]["query"] == {"since": ["m-000102"], "wait": ["2"]}


def test_inbox_json(stub_hub, tmp_path):
    hub = stub_hub()
    r = ov(hub.url, "inbox", "--json", state_dir=tmp_path)
    data = json.loads(r.stdout)
    assert [m["id"] for m in data["messages"]] == ["m-000101", "m-000102"] and data["cursor"] == "m-000102"


def test_progress_and_board(stub_hub, tmp_path):
    hub = stub_hub()
    r = ov(hub.url, "progress", "140", "tests", "passing", state_dir=tmp_path)
    assert r.returncode == 0 and "progress 100%" in r.stdout
    assert hub.requests[-1]["body"] == {"progress": 100, "note": "tests passing"}
    assert ov(hub.url, "progress", "abc", state_dir=tmp_path).returncode == 1
    r = ov(hub.url, "board", state_dir=tmp_path)
    assert r.returncode == 0
    assert "T1" in r.stdout and "Timer" in r.stdout and "core logic" in r.stdout and "id: p-1-T2" in r.stdout
    assert "mock@lap1" in r.stdout


def test_plan_submitted(stub_hub, tmp_path):
    hub = stub_hub(accept_plan=True)
    plan = tmp_path / "plan.in.json"
    plan.write_text(json.dumps({"title": "X", "summary": "s", "tasks": [{"ref": "T1", "title": "a"}]}))
    r = ov(hub.url, "plan", str(plan), state_dir=tmp_path / "state")
    assert r.returncode == 0 and "submitted" in r.stdout
    assert hub.requests[-1]["path"] == "/api/agent/plan"
    assert (tmp_path / "state" / "plan.submitted").exists()
    assert json.loads((tmp_path / "state" / "plan.json").read_text())["title"] == "X"


def test_plan_saved_locally_when_hub_rejects(stub_hub, tmp_path):
    hub = stub_hub(accept_plan=False)
    plan = tmp_path / "plan.in.json"
    plan.write_text(json.dumps([{"title": "a"}, {"title": "b", "depends_on": ["T1"]}]))
    r = ov(hub.url, "plan", str(plan), state_dir=tmp_path / "state")
    assert r.returncode == 0 and "saved" in r.stdout and "warning" in r.stderr
    assert not (tmp_path / "state" / "plan.submitted").exists()
    assert len(json.loads((tmp_path / "state" / "plan.json").read_text())["tasks"]) == 2


def test_plan_invalid_file(stub_hub, tmp_path):
    hub = stub_hub()
    bad = tmp_path / "bad.json"
    bad.write_text('{"tasks": [{"title": ""}]}')
    r = ov(hub.url, "plan", str(bad), state_dir=tmp_path)
    assert r.returncode == 1 and "non-empty title" in r.stderr
    assert ov(hub.url, "plan", str(tmp_path / "missing.json"), state_dir=tmp_path).returncode == 1


def test_errors_exit_1(stub_hub, tmp_path):
    hub = stub_hub()
    r = ov(hub.url, "board", token="wrong", state_dir=tmp_path)
    assert r.returncode == 1 and "HTTP 401" in r.stderr and "bad token" in r.stderr
    dead = f"http://127.0.0.1:{free_port()}"
    r = ov(dead, "say", "x", state_dir=tmp_path)
    assert r.returncode == 1 and "cannot reach" in r.stderr
    env = {k: v for k, v in os.environ.items() if not k.startswith("OV_")}
    r = subprocess.run([sys.executable, OV, "board"], env=env, capture_output=True, text=True, timeout=20)
    assert r.returncode == 1 and "OV_SERVER is not set" in r.stderr


def test_never_hangs_beyond_wait_plus_10(stub_hub, tmp_path):
    hub = stub_hub(inbox_delay=30)
    start = time.monotonic()
    r = ov(hub.url, "inbox", "--wait", "0", state_dir=tmp_path, timeout=25)
    elapsed = time.monotonic() - start
    assert r.returncode == 1
    assert elapsed < 11.5
