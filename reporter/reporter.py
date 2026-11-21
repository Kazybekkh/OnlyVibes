#!/usr/bin/env python3
"""
OnlyVibes reporter — runs on each teammate's laptop.

Detects running AI coding agent processes (Vibe, Claude Code, Codex, Cursor)
with psutil, tails the latest line of each agent's log file, and streams a
status message (see shared/schema.json) every INTERVAL seconds to SERVER_URL
over a websocket.

Env vars:
    SERVER_URL   websocket endpoint of the OnlyVibes server
                 (default ws://localhost:8000/ws)
    INTERVAL     seconds between reports (default 1)

Run:
    cd reporter
    uv run reporter.py
    # or against a server on the Tailnet:
    SERVER_URL=ws://100.x.x.x:8000/ws uv run reporter.py
"""
from __future__ import annotations

import json
import os
import platform
import socket
import subprocess
import time
from getpass import getuser

import psutil
from websockets.sync.client import connect

SERVER_URL = os.environ.get("SERVER_URL", "ws://localhost:8000/ws")
INTERVAL = float(os.environ.get("INTERVAL", "1"))

# cmdline substrings that identify each agent process
AGENT_PATTERNS: dict[str, list[str]] = {
    "vibe": ["vibe"],
    "claude-code": ["claude"],
    "codex": ["codex"],
    "cursor": ["cursor"],
}

# candidate log dirs per agent (first existing dir wins, newest file wins)
LOG_DIRS: dict[str, list[str]] = {
    "vibe": ["~/.vibe/logs"],
    "claude-code": ["~/.claude/logs", "~/.config/claude/logs"],
    "codex": ["~/.codex/logs", "~/.codex/sessions"],
    "cursor": ["~/.cursor/logs", "~/.config/Cursor/logs", "~/Library/Application Support/Cursor/logs"],
}

MAX_LOG_CHARS = 300
SELF_PIDS = {os.getpid(), os.getppid()}

# pid -> psutil.Process, kept across ticks so cpu_percent(None) is meaningful
_PROCS: dict[int, psutil.Process] = {}


def tailscale_ip() -> str:
    """Best-effort Tailscale IPv4 address of this laptop."""
    try:
        out = subprocess.run(
            ["tailscale", "ip", "-4"], capture_output=True, text=True, timeout=3
        )
        if out.returncode == 0 and out.stdout.strip():
            return out.stdout.strip().split()[0]
    except Exception:
        pass
    # fallback: local IP used for outbound traffic (on a Tailnet this is the
    # Tailscale IP; otherwise it is the LAN IP — still unique per laptop)
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("8.8.8.8", 80))
            return s.getsockname()[0]
    except Exception:
        return "127.0.0.1"


def tail_log(agent: str) -> str:
    """Latest non-empty line of the most recently modified log file of an agent."""
    best_file, best_mtime = None, 0.0
    for d in LOG_DIRS.get(agent, []):
        d = os.path.expanduser(d)
        if not os.path.isdir(d):
            continue
        for root, _dirs, files in os.walk(d):
            for f in files:
                if not f.endswith((".log", ".jsonl", ".txt")):
                    continue
                path = os.path.join(root, f)
                try:
                    m = os.path.getmtime(path)
                except OSError:
                    continue
                if m > best_mtime:
                    best_file, best_mtime = path, m
    if not best_file:
        return ""
    try:
        with open(best_file, "r", errors="ignore") as fh:
            lines = [ln.strip() for ln in fh if ln.strip()]
        return lines[-1][:MAX_LOG_CHARS] if lines else ""
    except OSError:
        return ""


def detect_agents() -> list[dict]:
    """Scan the process table for known agent CLIs/editors."""
    agents: list[dict] = []
    for proc in psutil.process_iter(["pid", "name", "cmdline"]):
        pid = proc.info["pid"]
        if pid in SELF_PIDS:
            continue
        cmdline = " ".join(proc.info["cmdline"] or []).lower()
        if "reporter.py" in cmdline or "mock_reporter" in cmdline:
            continue
        for name, needles in AGENT_PATTERNS.items():
            if any(n in cmdline for n in needles):
                p = _PROCS.setdefault(pid, proc)
                try:
                    cpu = round(p.cpu_percent(None), 1)  # non-blocking since last tick
                except Exception:
                    cpu = 0.0
                try:
                    cwd = p.cwd()
                except Exception:
                    cwd = ""
                agents.append(
                    {
                        "name": name,
                        "pid": pid,
                        "cpu": cpu,
                        "cwd": cwd,
                        "last_log": tail_log(name),
                        "summary": "",  # filled by the server's summarize()
                    }
                )
                break
    # one entry per agent: keep the busiest process (editors spawn many helpers)
    best: dict[str, dict] = {}
    for a in agents:
        if a["name"] not in best or a["cpu"] > best[a["name"]]["cpu"]:
            best[a["name"]] = a
    return list(best.values())


def build_status() -> dict:
    """One message conforming to shared/schema.json."""
    return {
        "laptop": platform.node(),
        "owner": getuser(),
        "tailscale_ip": tailscale_ip(),
        "ts": time.time(),
        "agents": detect_agents(),
    }


def main() -> None:
    print(f"[reporter] laptop={platform.node()} owner={getuser()} ip={tailscale_ip()}")
    print(f"[reporter] streaming to {SERVER_URL} every {INTERVAL}s")
    while True:
        try:
            with connect(SERVER_URL) as ws:
                while True:
                    status = build_status()
                    ws.send(json.dumps(status))
                    names = ", ".join(a["name"] for a in status["agents"]) or "-"
                    print(f"[reporter] sent {len(status['agents'])} agent(s): {names}")
                    time.sleep(INTERVAL)
        except Exception as e:
            print(f"[reporter] connection error: {e} — retrying in 2s")
            time.sleep(2)


if __name__ == "__main__":
    main()
