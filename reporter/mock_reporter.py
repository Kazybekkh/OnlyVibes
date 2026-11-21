#!/usr/bin/env python3
"""
OnlyVibes mock reporter — sends fake status for 3 laptops, no real agents needed.

Use it to demo/test the server + web viewer without running real agent CLIs:

    cd reporter
    SERVER_URL=ws://localhost:8000/ws uv run mock_reporter.py

Env vars:
    SERVER_URL  (default ws://localhost:8000/ws)
    INTERVAL    seconds between rounds (default 1); each round sends one
                status message per fake laptop
"""
from __future__ import annotations

import json
import os
import random
import time

from websockets.sync.client import connect

SERVER_URL = os.environ.get("SERVER_URL", "ws://localhost:8000/ws")
INTERVAL = float(os.environ.get("INTERVAL", "1"))

FAKE_LOGS: dict[str, list[str]] = {
    "vibe": [
        "⏺ Read src/main.ts (312 lines)",
        "⏺ Edit server/main.py",
        "✻ Thinking… (esc to interrupt)",
        "⏺ Ran `uv run pytest` — 12 passed",
    ],
    "claude-code": [
        "esc to interrupt · planning next step",
        "Read reporter/reporter.py",
        "Bash(uv sync) completed",
        "TodoWrite: 3/5 items done",
    ],
    "codex": [
        "working on web/index.html",
        "command: npm run build (exit 0)",
        "reasoning about WebXR session setup",
        "patched 3 files",
    ],
    "cursor": [
        "Agent: editing web/index.html",
        "Chat: applied suggestion (cmd+enter)",
        "Terminal: tailscale status ok",
        "Composer: 2 files changed",
    ],
}

# 3 fake laptops on a Tailnet
LAPTOPS = [
    {
        "laptop": "kay-mbp",
        "owner": "kay",
        "tailscale_ip": "100.64.0.11",
        "agents": ["vibe", "cursor"],
    },
    {
        "laptop": "amina-air",
        "owner": "amina",
        "tailscale_ip": "100.64.0.27",
        "agents": ["claude-code", "codex"],
    },
    {
        "laptop": "dao-legion",
        "owner": "dao",
        "tailscale_ip": "100.64.0.42",
        "agents": ["codex", "vibe", "claude-code"],
    },
]


def fake_status(spec: dict, tick: int) -> dict:
    agents = []
    for i, name in enumerate(spec["agents"]):
        logs = FAKE_LOGS[name]
        agents.append(
            {
                "name": name,
                "pid": 3000 + i * 17 + (tick % 3),  # wiggle a little
                "cpu": round(random.uniform(1.5, 88.0), 1),
                "cwd": f"/Users/{spec['owner']}/Developer/OnlyVibes",
                "last_log": logs[(tick + i) % len(logs)],
                "summary": "",  # server fills this via summarize()
            }
        )
    return {
        "laptop": spec["laptop"],
        "owner": spec["owner"],
        "tailscale_ip": spec["tailscale_ip"],
        "ts": time.time(),
        "agents": agents,
    }


def main() -> None:
    print(f"[mock] sending fake data for {len(LAPTOPS)} laptops to {SERVER_URL} every {INTERVAL}s")
    tick = 0
    while True:
        try:
            with connect(SERVER_URL) as ws:
                while True:
                    for spec in LAPTOPS:
                        ws.send(json.dumps(fake_status(spec, tick)))
                    print(f"[mock] round {tick}: sent {len(LAPTOPS)} laptop statuses")
                    tick += 1
                    time.sleep(INTERVAL)
        except Exception as e:
            print(f"[mock] connection error: {e} — retrying in 2s")
            time.sleep(2)


if __name__ == "__main__":
    main()
