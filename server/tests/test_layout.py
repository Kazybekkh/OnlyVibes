"""GET/POST /api/layout: real-table AR placement, persisted, locked by the control secret."""
from __future__ import annotations

from test_server import make_client  # noqa: F401  (fixture)

LAYOUT = {"ref": ["100.0.0.1", "100.0.0.2"], "laptops": {
    "100.0.0.1": {"x": 0, "y": 0, "z": 0, "yaw": 0.5, "owner": "kay"},
    "100.0.0.2": {"x": 1.2, "y": 0, "z": 0, "yaw": -0.5, "owner": "carlo"},
    "100.0.0.3": {"x": 0.6, "y": 0.01, "z": 0.9, "yaw": 3.1, "owner": "don"}}}


def test_layout_locked_saved_and_in_view(make_client, tmp_path):
    c = make_client(control_secret="s3cret")
    assert c.get("/api/layout").json()["laptops"] == {}
    assert c.post("/api/layout", json=LAYOUT).status_code == 403
    assert c.post("/api/layout?key=nope", json=LAYOUT).status_code == 403
    r = c.post("/api/layout", json=LAYOUT, headers={"X-OV-Key": "s3cret"})
    assert r.status_code == 200 and r.json()["ref"] == ["100.0.0.1", "100.0.0.2"]
    got = c.get("/api/layout").json()
    assert got["laptops"]["100.0.0.3"] == {"x": 0.6, "y": 0.01, "z": 0.9, "yaw": 3.1, "owner": "don"}
    with c.websocket_connect("/view") as v:
        assert v.receive_json()["layout"]["laptops"]["100.0.0.2"]["x"] == 1.2
    assert (tmp_path / "data" / "layout.json").exists() or any(tmp_path.rglob("layout.json"))
    bad = {"laptops": {"k": {"x": "1"}}}
    assert c.post("/api/layout?key=s3cret", json=bad).status_code == 400
    assert c.post("/api/layout?key=s3cret", json={"laptops": {"k": {"x": float("1e9")}}}).status_code == 400
