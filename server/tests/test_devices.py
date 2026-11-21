"""Device info (OS / model / chip) flows from reporters and workers to /api/state and /view."""
from __future__ import annotations

import time

from test_server import FakeWorker, client, make_client  # noqa: F401  (fixtures)

MAC = {"hostname": "kay-mbp", "os": "macOS 15.1", "os_family": "macos", "os_version": "15.1", "arch": "arm64",
       "vendor": "Apple", "model": "MacBook Pro", "model_id": "Mac15,6", "chip": "Apple M3 Pro"}


def test_reporter_device_passthrough_and_sanitised(client):
    with client.websocket_connect("/ws") as rep:
        rep.send_json({"laptop": "kay-mbp", "owner": "kay", "tailscale_ip": "100.0.0.5", "ts": time.time(),
                       "agents": [], "device": {**MAC, "serial_number": "C02XYZ", "chip": "x" * 500}})
        rep.send_json({"laptop": "old", "owner": "zed", "tailscale_ip": "100.0.0.6", "ts": time.time(), "agents": []})
        with client.websocket_connect("/view") as view:
            for _ in range(20):
                laps = {lap["tailscale_ip"]: lap for lap in view.receive_json()["laptops"]}
                if "100.0.0.5" in laps and "100.0.0.6" in laps:
                    break
    dev = laps["100.0.0.5"]["device"]
    assert dev["model"] == "MacBook Pro" and dev["os"] == "macOS 15.1" and dev["arch"] == "arm64"
    assert "serial_number" not in dev and len(dev["chip"]) == 80
    assert "device" not in laps["100.0.0.6"]  # old reporters keep working without it


def test_worker_device_in_state_and_synth_laptop(client):
    w = FakeWorker.__new__(FakeWorker)
    w.id = "w9"
    w._cm = client.websocket_connect("/worker")
    w.ws = w._cm.__enter__()
    w.send(type="hello", running=[], worker={"id": "w9", "laptop": "w9", "owner": "don", "tailscale_ip": "100.0.0.9",
                                             "agents": ["mock"], "max_concurrent": 1, "version": "2",
                                             "device": {"os": "Ubuntu 24.04 LTS", "os_family": "linux",
                                                        "vendor": "LENOVO", "model": "ThinkPad X1 Carbon",
                                                        "uuid": "nope"}})
    w.expect("welcome")
    state = client.get("/api/state").json()
    worker = next(x for x in state["workers"] if x["id"] == "w9")
    assert worker["device"] == {"os": "Ubuntu 24.04 LTS", "os_family": "linux", "vendor": "LENOVO",
                                "model": "ThinkPad X1 Carbon"}
    lap = next(x for x in state["laptops"] if x["tailscale_ip"] == "100.0.0.9")
    assert lap["device"]["model"] == "ThinkPad X1 Carbon"
    w._cm.__exit__(None, None, None)
