#!/usr/bin/env python3
"""
OnlyVibes reporter — runs on each teammate's laptop.

Detects running AI coding agent processes (Vibe, Claude Code, Codex, Cursor)
with psutil, tails the latest readable line of each agent's log file, and
streams a status message (see shared/schema.json) every INTERVAL seconds to
SERVER_URL over a websocket.

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

# Match on the EXECUTABLE name only (basename of cmdline[0], fallback to
# proc.name()) — never a substring of the full cmdline. Exact equality, so
# "Cursor Helper (Renderer)" etc. never match "cursor".
AGENT_EXE_NAMES: dict[str, set[str]] = {
    "vibe": {"vibe", "vibe-rs"},  # vibe-rs = Rust CLI binary the `vibe` shim execs into
    "claude-code": {"claude"},
    "codex": {"codex"},
    "cursor": {"cursor"},  # main Cursor app only; helpers have their own exe names
}

# never report our own stack
EXCLUDE_CMDLINE_SUBSTRINGS = ("onlyvibes", "reporter.py", "mock_reporter", "main.py", "uvicorn")

# candidate log dirs per agent, in PRIORITY order (first dir that yields a
# file wins). Claude Code sessions are one .jsonl per session under
# ~/.claude/projects/**, so that dir comes first.
LOG_DIRS: dict[str, list[str]] = {
    "vibe": ["~/.vibe/logs"],
    "claude-code": ["~/.claude/projects", "~/.claude/logs", "~/.config/claude/logs"],
    "codex": ["~/.codex/logs", "~/.codex/sessions"],
    "cursor": ["~/.cursor/logs", "~/.config/Cursor/logs", "~/Library/Application Support/Cursor/logs"],
}

MAX_LOG_CHARS = 300
SELF_PIDS = {os.getpid(), os.getppid()}

# pid -> psutil.Process, kept across ticks so cpu_percent(None) is meaningful
_PROCS: dict[int, psutil.Process] = {}


# `tailscale ip -4` via the PATH binary, or via the macOS app bundle when the
# CLI is not on PATH (Tailscale.app ships its own CLI at this fixed location)
TAILSCALE_CLI_CANDIDATES = [
    ["tailscale", "ip", "-4"],
    ["/Applications/Tailscale.app/Contents/MacOS/Tailscale", "ip", "-4"],
]


def tailscale_ip() -> str:
    """Best-effort Tailscale IPv4 address of this laptop."""
    for cmd in TAILSCALE_CLI_CANDIDATES:
        try:
            out = subprocess.run(cmd, capture_output=True, text=True, timeout=3)
        except (FileNotFoundError, OSError):
            continue  # not on PATH / not installed here -> try next candidate
        if out.returncode == 0 and out.stdout.strip():
            return out.stdout.strip().split()[0]
    # fallback: local IP used for outbound traffic (on a Tailnet this is the
    # Tailscale IP; otherwise it is the LAN IP — still unique per laptop)
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("8.8.8.8", 80))
            return s.getsockname()[0]
    except Exception:
        return "127.0.0.1"


def exe_name(info: dict) -> str:
    """Executable name used for agent matching.

    basename of cmdline[0] — unless that is a python/node interpreter
    (python, python3, python3.x, node), in which case skip interpreter flags
    (args starting with "-") and use basename of the first real argument,
    i.e. the script/binary being run: `node /usr/local/bin/vibe` -> "vibe",
    `python3 -u -m vibe` -> "vibe". Falls back to proc.name() if no cmdline.
    """
    cmdline = [str(c) for c in (info.get("cmdline") or []) if c]
    if not cmdline:
        return (info.get("name") or "").lower()
    base = os.path.basename(cmdline[0]).lower()
    if base == "node" or base.startswith("python"):
        for arg in cmdline[1:]:
            if arg.startswith("-"):
                continue  # skip interpreter flags (-u, -m, ...)
            return os.path.basename(arg).lower()
    return base


def detect_agents() -> list[dict]:
    """Scan the process table for known agent executables.

    Matches on executable name only. Reports at most ONE entry per agent
    name: the highest-CPU matching process. (shared/schema.json sets
    additionalProperties:false on agent objects, so no extra "count" field
    is sent — dedupe only.)
    """
    agents: list[dict] = []
    for proc in psutil.process_iter(["pid", "name", "cmdline"]):
        pid = proc.info["pid"]
        if pid in SELF_PIDS:
            continue
        cmdline = " ".join(proc.info["cmdline"] or []).lower()
        if any(s in cmdline for s in EXCLUDE_CMDLINE_SUBSTRINGS):
            continue
        exe = exe_name(proc.info)
        for agent_name, wanted in AGENT_EXE_NAMES.items():
            if exe in wanted:
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
                        "name": agent_name,
                        "pid": pid,
                        "cpu": cpu,
                        "cwd": cwd,
                        "last_log": tail_log(agent_name),
                        "summary": "",  # filled by the server's summarize()
                    }
                )
                break
    # dedupe: one entry per agent name, highest CPU wins
    best: dict[str, dict] = {}
    for a in agents:
        if a["name"] not in best or a["cpu"] > best[a["name"]]["cpu"]:
            best[a["name"]] = a
    return list(best.values())


def _newest_log_file(d: str) -> str | None:
    """Most recently modified log file under dir d (recursive), or None."""
    best_file, best_mtime = None, 0.0
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
    return best_file


def readable_from_jsonl_line(line: str) -> str:
    """Reduce one Claude Code session .jsonl line to readable text.

    Prefers message content blocks (assistant text, tool_use name +
    command/file), then common top-level text fields; falls back to raw.
    """
    try:
        obj = json.loads(line)
    except json.JSONDecodeError:
        return line
    if not isinstance(obj, dict):
        return line
    msg = obj.get("message")
    if isinstance(msg, dict):
        content = msg.get("content")
        if isinstance(content, str) and content.strip():
            return content.strip()
        if isinstance(content, list):
            parts = []
            for block in content:
                if not isinstance(block, dict):
                    continue
                if block.get("type") == "text" and block.get("text"):
                    parts.append(str(block["text"]).strip())
                elif block.get("type") == "tool_use":
                    tool = block.get("name", "tool")
                    inp = block.get("input") if isinstance(block.get("input"), dict) else {}
                    detail = inp.get("command") or inp.get("file_path") or inp.get("description") or ""
                    parts.append(f"[{tool}] {detail}".strip())
                elif block.get("type") == "tool_result":
                    c = block.get("content")
                    if isinstance(c, str) and c.strip():
                        parts.append(c.strip())
                    elif isinstance(c, list):
                        for cb in c:
                            if isinstance(cb, dict) and cb.get("type") == "text" and cb.get("text"):
                                parts.append(str(cb["text"]).strip())
            if parts:
                return " | ".join(parts)
    for key in ("summary", "text", "command", "tool_name", "name"):
        v = obj.get(key)
        if isinstance(v, str) and v.strip():
            return v.strip()
    return line


def tail_log(agent: str) -> str:
    """Latest readable line of the agent's most recently modified log file.

    Log dirs are tried in priority order. .jsonl files (Claude Code
    sessions under ~/.claude/projects/**) are parsed as JSON and reduced
    to a readable text field instead of sending raw JSON.
    """
    for d in LOG_DIRS.get(agent, []):
        d = os.path.expanduser(d)
        if not os.path.isdir(d):
            continue
        path = _newest_log_file(d)
        if not path:
            continue
        try:
            with open(path, "r", errors="ignore") as fh:
                lines = [ln.strip() for ln in fh if ln.strip()]
        except OSError:
            continue
        if not lines:
            continue
        last = lines[-1]
        if path.endswith(".jsonl"):
            last = readable_from_jsonl_line(last)
        return last[:MAX_LOG_CHARS]
    return ""


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
