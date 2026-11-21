"""GET/POST /api/xrpose: in-memory headset pose for the laptop spectator, locked by the control secret."""
from __future__ import annotations

from test_server import make_client  # noqa: F401  (fixture)

POSE = {
    "mode": "vr",
    "head": {"pos": [0.1, 1.5, -0.2], "quat": [0, 0, 0, 1]},
    "controllers": [
        {"connected": True, "hand": "left", "pos": [-0.2, 1.1, -0.3], "quat": [0, 0, 0, 1],
         "trigger": True, "squeeze": False, "ray": 0.8},
        {"connected": True, "hand": "right", "pos": [0.2, 1.05, -0.25], "quat": [0, 0.1, 0, 0.995],
         "trigger": False, "squeeze": True, "ray": 0.5},
    ],
    "world": {"pos": [0, 1.5, 0], "quat": [0, 0, 0, 1], "scale": 0.32},
    "selected": {"ak": "claude-code", "laptop": "100.0.0.1"},
    "hover": {"ak": "codex", "laptop": "100.0.0.2"},
    "panel": True, "grabbed": True, "ptt": True,
    "voice": {"text": "stop and retry"},
}


def test_xrpose_locked_stored_and_aged(make_client):
    c = make_client(control_secret="s3cret")
    assert c.get("/api/xrpose").status_code == 403
    assert c.post("/api/xrpose", json=POSE).status_code == 403
    assert c.post("/api/xrpose?key=nope", json=POSE).status_code == 403
    empty = c.get("/api/xrpose?key=s3cret").json()
    assert empty == {"pose": None, "age_ms": None}
    r = c.post("/api/xrpose", json=POSE, headers={"X-OV-Key": "s3cret"})
    assert r.status_code == 200
    body = r.json()
    assert body["age_ms"] == 0
    pose = body["pose"]
    assert pose["mode"] == "vr"
    assert pose["head"]["pos"] == [0.1, 1.5, -0.2]
    assert pose["controllers"][0]["trigger"] is True
    assert pose["controllers"][1]["squeeze"] is True
    assert pose["controllers"][1]["hand"] == "right"
    assert pose["world"]["scale"] == 0.32
    assert pose["selected"] == {"ak": "claude-code", "laptop": "100.0.0.1"}
    assert pose["hover"] == {"ak": "codex", "laptop": "100.0.0.2"}
    assert pose["panel"] is True and pose["grabbed"] is True and pose["ptt"] is True
    assert pose["voice"] == {"text": "stop and retry"}
    got = c.get("/api/xrpose", headers={"X-OV-Key": "s3cret"}).json()
    assert got["pose"]["head"]["pos"] == [0.1, 1.5, -0.2]
    assert got["age_ms"] is not None and got["age_ms"] >= 0


def test_xrpose_rejects_bad_payloads(make_client):
    c = make_client(control_secret="s3cret")
    key = {"X-OV-Key": "s3cret"}
    assert c.post("/api/xrpose", json={}, headers=key).status_code == 400
    assert c.post("/api/xrpose", json={"head": {"pos": [0, 0, 0]}}, headers=key).status_code == 400
    assert c.post("/api/xrpose", json={"head": {"pos": [0, 0, "x"], "quat": [0, 0, 0, 1]}}, headers=key).status_code == 400
    assert c.post("/api/xrpose", json={"head": {"pos": [1e9, 0, 0], "quat": [0, 0, 0, 1]}}, headers=key).status_code == 400
    assert c.post("/api/xrpose", json={"head": {"pos": [0, 0, 0], "quat": [0, 0, 0, 1]},
                                      "world": {"pos": [0, 0, 0], "quat": [0, 0, 0, 1], "scale": -1}},
                  headers=key).status_code == 400


def test_xrpose_open_when_no_secret(make_client):
    c = make_client()
    r = c.post("/api/xrpose", json={"head": {"pos": [0, 1, 0], "quat": [0, 0, 0, 1]}})
    assert r.status_code == 200 and r.json()["pose"]["head"]["pos"] == [0, 1, 0]
    assert c.get("/api/xrpose").json()["pose"]["head"]["pos"] == [0, 1, 0]


def test_spectate_page_served(make_client):
    c = make_client()
    r = c.get("/spectate")
    assert r.status_code == 200
    # tests use a stub web dir; /spectate is the same file as /ar
    assert r.text == c.get("/ar").text == "<h1>ar</h1>"
