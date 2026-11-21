"""
Tests for the ported "Mistral brain" (legacy.LegacyBrain): cached background summaries
with rate limiting, server-side staleness (_seen), agent states (error -> stuck), collision
alerts, and the new /view + /api/state fields.

mistral_chat is monkeypatched everywhere — the real Mistral API is never called.

    cd server && uv run pytest -q
"""
from __future__ import annotations

import asyncio
import json
import sys
import time
from pathlib import Path

import anyio
import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import legacy  # noqa: E402
from config import Settings  # noqa: E402
from main import create_app  # noqa: E402


# ----------------------------------------------------------------------------- helpers
def make_settings(tmp_path: Path, **overrides) -> Settings:
    web = tmp_path / "web"
    web.mkdir(exist_ok=True)
    (web / "index.html").write_text("<h1>board</h1>")
    (web / "ar.html").write_text("<h1>ar</h1>")
    s = Settings(
        data_dir=tmp_path / "data", web_dir=web, tick_interval=0.05, save_debounce=0.02,
        frame_flush_interval=0.05, reattach_grace=0.0, restart_grace=0.0, ping_interval=30.0,
    )
    for k, v in overrides.items():
        setattr(s, k, v)
    return s


@pytest.fixture
def client(tmp_path):
    c = TestClient(create_app(make_settings(tmp_path)))
    c.__enter__()
    yield c
    c.__exit__(None, None, None)


async def _recv(rx, timeout):
    with anyio.fail_after(timeout):
        return await rx.receive()


def recv_json(ws, timeout: float = 5.0) -> dict:
    msg = ws.portal.call(_recv, ws._send_rx, timeout)
    return json.loads(msg.get("text") or msg["bytes"].decode())


def recv_until(ws, pred, timeout: float = 5.0) -> dict:
    deadline = time.monotonic() + timeout
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise AssertionError("expected frame never arrived")
        m = recv_json(ws, remaining)
        if pred(m):
            return m


def status_msg(laptop: str, ip: str, agents: list, ts=None) -> dict:
    return {"laptop": laptop, "owner": "tester", "tailscale_ip": ip,
            "ts": time.time() if ts is None else ts, "agents": agents}


def agent(name="vibe", pid=1, cpu=0.0, log="hello", summary="") -> dict:
    return {"name": name, "pid": pid, "cpu": cpu, "cwd": "/", "last_log": log, "summary": summary}


# ----------------------------------------------------------------------------- brain units
def test_laptop_key_prefers_tailscale_ip_and_falls_back_to_hostname():
    assert legacy.laptop_key({"tailscale_ip": "100.64.0.1", "laptop": "mbp"}) == "100.64.0.1"
    assert legacy.laptop_key({"tailscale_ip": "", "laptop": "mbp"}) == "mbp"
    assert legacy.laptop_key({"tailscale_ip": "127.0.0.1", "laptop": "mbp"}) == "mbp"
    assert legacy.laptop_key({"tailscale_ip": None, "laptop": ""}) == "unknown"


def test_same_line_every_half_second_triggers_at_most_two_mistral_calls(monkeypatch):
    """One agent reporting the same log line every 0.5 s for 6 s: the summary cache plus the
    SUMMARY_EVERY throttle allow at most 2 Mistral calls, and they run in the background."""
    calls: list[str] = []

    async def fake_chat(system, user, api_key, model):
        calls.append(user)
        await asyncio.sleep(0.001)  # simulate API latency
        return "a short summary"

    monkeypatch.setattr(legacy, "mistral_chat", fake_chat)

    async def run():
        brain = legacy.LegacyBrain("key", "model")
        for i in range(13):  # t = 0.0 .. 6.0 s, one message every 0.5 s
            data = status_msg("lap", "100.0.0.1", [agent(log="same log line")], ts=i * 0.5)
            brain.on_status(legacy.laptop_key(data), data, i * 0.5)
            await asyncio.sleep(0)  # let a scheduled background task start
        await asyncio.sleep(0.05)  # let all background tasks finish
        return brain

    brain = asyncio.run(run())
    assert 1 <= len(calls) <= 2
    assert brain.summary_cache.get(("vibe", "same log line")) == "a short summary"


def test_error_line_becomes_stuck_after_stuck_after(monkeypatch):
    monkeypatch.setattr(legacy, "STUCK_AFTER", 2.0)

    async def run():
        brain = legacy.LegacyBrain("", "model")  # no key: no Mistral calls at all
        states = []
        for i in range(4):
            t = 1000.0 + i
            data = status_msg("lap", "100.0.0.1", [agent(log="Traceback (most recent call last): boom")])
            brain.on_status(legacy.laptop_key(data), data, t)
            states.append(data["agents"][0]["state"])
        return states

    states = asyncio.run(run())
    assert states[0] == "error"
    assert states[-1] == "stuck"  # still erroring after STUCK_AFTER


def test_brain_collisions_and_annotate_keep_synthesized_summary():
    brain = legacy.LegacyBrain("", "model")
    t = 500.0
    a = status_msg("lapA", "100.0.0.11", [agent(name="codex", log="editing server/main.py")])
    b = status_msg("lapB", "100.0.0.12", [agent(name="vibe", pid=2, log="patched server/main.py")])
    brain.on_status(legacy.laptop_key(a), a, t)
    brain.on_status(legacy.laptop_key(b), b, t)
    laptops = [a, b, {"laptop": "w", "owner": "o", "tailscale_ip": "100.0.0.13", "ts": t,
                      "agents": [{"name": "mock", "pid": 0, "cpu": 0, "cwd": "", "last_log": "thinking",
                                  "summary": "T1 · Fix — 10%", "agent_id": "task:p-T1"}]}]
    brain.annotate(laptops, t)
    alerts = brain.collisions(laptops, t)
    assert len(alerts) == 1 and alerts[0]["kind"] == "collision"
    assert alerts[0]["file"] == "server/main.py" and len(alerts[0]["agents"]) == 2
    # synthesized orchestra worker agent: state added, its own summary kept
    synth = laptops[2]["agents"][0]
    assert synth["agent_id"] == "task:p-T1" and synth["summary"] == "T1 · Fix — 10%"
    assert synth["state"] == "working"  # log line just changed
    # reporter agents got agent_id + state too
    assert a["agents"][0]["agent_id"].startswith("100.0.0.11:") and a["agents"][0]["state"] == "working"
    assert legacy.count_line(laptops) == "3 working across 3 laptop(s)"


def test_summary_fallback_is_empty_never_the_raw_line(monkeypatch):
    """No API key or a failed Mistral call -> "" (Kazybek's task-title style), never the raw log."""
    async def boom(system, user, api_key, model):
        raise RuntimeError("mistral down")

    monkeypatch.setattr(legacy, "mistral_chat", boom)

    async def run():
        # summarize(): no key and API error both give ""
        assert await legacy.summarize("editing server/main.py", "", "m") == ""
        assert await legacy.summarize("editing server/main.py", "key", "m") == ""
        # brain without a key: summary stays "", no Mistral call
        keyless = legacy.LegacyBrain("", "model")
        d1 = status_msg("lap", "100.0.0.1", [agent(log="raw log line here")])
        keyless.on_status(legacy.laptop_key(d1), d1, 1.0)
        assert d1["agents"][0]["summary"] == ""
        # brain with a key but a failing call: "" while pending and after the error
        brain = legacy.LegacyBrain("key", "model")
        d2 = status_msg("lap", "100.0.0.1", [agent(log="raw log line here")])
        brain.on_status(legacy.laptop_key(d2), d2, 1.0)
        assert d2["agents"][0]["summary"] == ""
        await asyncio.sleep(0.01)  # let the background task fail
        d3 = status_msg("lap", "100.0.0.1", [agent(log="raw log line here")])
        brain.on_status(legacy.laptop_key(d3), d3, 2.0)
        assert d3["agents"][0]["summary"] == ""
        assert ("vibe", "raw log line here") not in brain.summary_cache  # failures not cached

    asyncio.run(run())


# ----------------------------------------------------------------------------- app level
def test_laptop_with_wrong_clock_still_appears_in_view(client):
    with client.websocket_connect("/ws") as rep:
        rep.send_json(status_msg("badclock", "100.7.7.7", [agent(log="hi")], ts=0))  # clock broken
        with client.websocket_connect("/view") as view:
            snap = recv_until(view, lambda m: m.get("type") == "state" and any(
                lap.get("tailscale_ip") == "100.7.7.7" for lap in m.get("laptops", [])))
            lap = next(lap for lap in snap["laptops"] if lap["tailscale_ip"] == "100.7.7.7")
            assert lap["ts"] == 0            # reporter's ts kept as sent...
            assert lap["_seen"] > 0          # ...but staleness uses the server receive time
            assert lap["agents"][0]["state"] == "working"
            assert lap["agents"][0]["agent_id"].startswith("100.7.7.7:")


def test_two_laptops_editing_same_file_produce_collision_alert(client):
    with client.websocket_connect("/ws") as rep:
        rep.send_json(status_msg("lapA", "100.0.0.11", [agent(name="codex", log="editing server/main.py now")]))
        rep.send_json(status_msg("lapB", "100.0.0.12", [agent(name="vibe", pid=2, log="patched server/main.py")]))
        with client.websocket_connect("/view") as view:
            snap = recv_until(view, lambda m: any(a.get("kind") == "collision" for a in m.get("alerts", [])))
            alert = next(a for a in snap["alerts"] if a["kind"] == "collision")
            assert alert["file"] == "server/main.py"
            assert len(alert["agents"]) == 2
            assert "codex" in alert["text"] and "vibe" in alert["text"]


def test_view_frames_and_api_state_have_team_summary_and_alerts(client):
    with client.websocket_connect("/ws") as rep:
        rep.send_json(status_msg("lap", "100.0.0.21", [agent(cpu=9.0, log="working hard")]))
        with client.websocket_connect("/view") as view:
            snap = recv_until(view, lambda m: m.get("type") == "state" and any(
                lap.get("tailscale_ip") == "100.0.0.21" for lap in m.get("laptops", [])))
            assert {"type", "ts", "team_summary", "alerts", "laptops"} <= set(snap)
            assert isinstance(snap["alerts"], list)
            assert isinstance(snap["team_summary"], str) and snap["team_summary"]
            lap = next(lap for lap in snap["laptops"] if lap["tailscale_ip"] == "100.0.0.21")
            assert lap["agents"][0]["state"] == "working"  # cpu 9 > WORKING_CPU
            assert "working" in snap["team_summary"]        # count_line fallback (no API key)
    state = client.get("/api/state").json()
    assert "team_summary" in state and "alerts" in state and "laptops" in state
    assert isinstance(state["alerts"], list) and state["team_summary"]
