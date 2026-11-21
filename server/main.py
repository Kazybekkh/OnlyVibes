#!/usr/bin/env python3
"""
OnlyVibes server.

Endpoints:
    WS   /ws         <- reporters stream laptop status messages (shared/schema.json)
    WS   /view       -> viewers (web/index.html) get a broadcast of all laptop states, 2x/s
    GET  /api/state  -> same snapshot as JSON (debugging)
    POST /summarize  -> {"last_log": "..."} -> {"summary": "..."} via Mistral Small
    GET  /           -> serves ../web/index.html (Three.js / WebXR viewer)

How summaries work (rate-limit safe):
    - Statuses are stored the moment they arrive; nothing waits for Mistral.
    - Each distinct (agent name, log line) is summarised once and cached.
    - Each agent gets at most one Mistral call every SUMMARY_EVERY seconds,
      and at most MAX_CONCURRENT calls run at the same time.
    - Until a summary is ready, the agent keeps its previous summary
      (or the raw log line, shortened).

Each agent in the broadcast also gets an "agent_id" and a "state":
    "stuck"   - still failing after STUCK_AFTER seconds (looping on an error)
    "error"   - the log line looks like a failure (error, failed, traceback, ...)
    "working" - CPU above WORKING_CPU or the log line changed recently
    "idle"    - otherwise

The broadcast also carries:
    "alerts"        - collisions: two agents editing the same file within 60s
    "team_summary"  - one headline for the whole team (Mistral every 10s when
                      something changed; plain counts without a key)

Env vars:
    MISTRAL_API_KEY  enables summarize() through the Mistral API; without it the
                     stub falls back to a truncated raw log line
    MISTRAL_MODEL    model used by summarize() (default mistral-small-latest)
    PORT             listen port when run as `uv run main.py` (default 8000)
    STALE_AFTER      seconds without updates before a laptop is dropped (default 5)
    SUMMARY_EVERY    min seconds between Mistral calls per agent (default 5)
    STUCK_AFTER      seconds of continuous errors before an agent is "stuck" (default 45)

Run:
    cd server
    uv run uvicorn main:app --host 0.0.0.0 --port 8000
    # or: uv run main.py
"""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import json
import os
import re
import time
from pathlib import Path

import httpx
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse

MISTRAL_API_KEY = os.environ.get("MISTRAL_API_KEY", "")
MISTRAL_MODEL = os.environ.get("MISTRAL_MODEL", "mistral-small-latest")
STALE_AFTER = float(os.environ.get("STALE_AFTER", "5"))
SUMMARY_EVERY = float(os.environ.get("SUMMARY_EVERY", "5"))
MAX_CONCURRENT = 2          # Mistral calls in flight at once
CACHE_MAX = 2000            # distinct log lines kept in the summary cache
WORKING_CPU = 5.0           # % CPU above which an agent counts as working
WORKING_WINDOW = 10.0       # seconds a new log line keeps an agent "working"
ERROR_RE = re.compile(
    r"\b(error|failed|failure|traceback|exception|exit (code )?[1-9]|tests? failing)\b",
    re.IGNORECASE,
)

STUCK_AFTER = float(os.environ.get("STUCK_AFTER", "45"))   # seconds of errors -> "stuck"
COLLISION_WINDOW = 60.0     # seconds an edit counts towards a collision
TEAM_SUMMARY_EVERY = 10.0   # seconds between team-headline refreshes
EDIT_RE = re.compile(r"\b(edit|edited|editing|write|wrote|writing|patch|patched|modif|updat|creat|apply|applied)", re.IGNORECASE)
FILE_RE = re.compile(r"[\w./-]*[\w-]+\.(?:py|js|ts|tsx|jsx|html|css|json|toml|md|go|rs|java|c|cpp|h|yml|yaml|sh)\b")

WEB_DIR = Path(__file__).resolve().parent.parent / "web"


@asynccontextmanager
async def lifespan(_app: FastAPI):
    task = asyncio.create_task(team_summary_loop())
    yield
    task.cancel()


app = FastAPI(title="OnlyVibes server", lifespan=lifespan)

# key = tailscale_ip (fallback: laptop hostname) -> latest status message
states: dict[str, dict] = {}
lock = asyncio.Lock()

# summary bookkeeping
summary_cache: dict[tuple[str, str], str] = {}   # (agent name, log line) -> summary
in_flight: set[tuple[str, str]] = set()          # lines being summarised right now
last_call: dict[str, float] = {}                 # agent key -> time of last Mistral call
last_summary: dict[str, str] = {}                # agent key -> latest summary shown
last_log_seen: dict[str, tuple[str, float]] = {} # agent key -> (log line, when it changed)
mistral_sem = asyncio.Semaphore(MAX_CONCURRENT)

# stuck / collision / team-headline bookkeeping
error_since: dict[str, float] = {}                # agent key -> when its errors started
recent_edits: dict[str, dict[str, float]] = {}    # agent key -> {file: last edit time}
team_summary: str = ""


def fallback(line: str) -> str:
    return line.strip()[:120]


async def mistral_chat(system: str, user: str, temperature: float = 0.2) -> str:
    """One Mistral chat call, limited to MAX_CONCURRENT in flight. Raises on error."""
    async with mistral_sem:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(
                "https://api.mistral.ai/v1/chat/completions",
                headers={
                    "Authorization": f"Bearer {MISTRAL_API_KEY}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": MISTRAL_MODEL,
                    "messages": [
                        {"role": "system", "content": system},
                        {"role": "user", "content": user},
                    ],
                    "temperature": temperature,
                },
            )
    resp.raise_for_status()
    return resp.json()["choices"][0]["message"]["content"].strip()


async def summarize(last_log: str) -> str:
    """
    Turn one raw agent log line into a single plain-English line using
    Mistral Small. Without MISTRAL_API_KEY it returns the truncated raw line,
    so the demo works keyless. Raises on API errors (callers fall back).
    """
    line = (last_log or "").strip()
    if not line:
        return ""
    if not MISTRAL_API_KEY:
        return fallback(line)
    try:
        return await mistral_chat(
            "You summarize one log line from an AI coding agent "
            "into a single short plain-English sentence (max 12 words). "
            "No preamble, no quotes, no emoji.",
            line[:500],
        )
    except Exception as exc:  # never let summarization break ingestion
        print(f"[summarize] fallback after error: {exc}")
        raise


def agent_key(laptop_key: str, agent: dict) -> str:
    return f"{laptop_key}:{agent.get('name')}:{agent.get('pid')}"


async def summarize_in_background(akey: str, name: str, line: str) -> None:
    """Summarise one line, cache it, and patch it into the live state."""
    ck = (name, line)
    try:
        summary = await summarize(line)
        if len(summary_cache) >= CACHE_MAX:
            summary_cache.clear()
        summary_cache[ck] = summary
    except Exception:
        summary = fallback(line)  # not cached, so it is retried after SUMMARY_EVERY
    finally:
        in_flight.discard(ck)
    last_summary[akey] = summary


def fill_agent(laptop_key: str, agent: dict, now: float) -> None:
    """Set summary + state on one agent without ever waiting for Mistral."""
    akey = agent_key(laptop_key, agent)
    name = agent.get("name", "")
    line = (agent.get("last_log") or "").strip()

    # track when the log line last changed (drives the "working" state)
    prev = last_log_seen.get(akey)
    if prev is None or prev[0] != line:
        last_log_seen[akey] = (line, now)
    changed_at = last_log_seen[akey][1]

    # summary: cached > previous > raw line; schedule Mistral if needed
    if agent.get("summary"):
        pass  # reporter already sent one
    elif not line:
        agent["summary"] = ""
    elif (name, line) in summary_cache:
        agent["summary"] = summary_cache[(name, line)]
        last_summary[akey] = agent["summary"]
    else:
        agent["summary"] = last_summary.get(akey) or fallback(line)
        ck = (name, line)
        if ck not in in_flight and now - last_call.get(akey, 0) >= SUMMARY_EVERY:
            in_flight.add(ck)
            last_call[akey] = now
            asyncio.create_task(summarize_in_background(akey, name, line))

    # state: error -> stuck if the agent keeps failing for STUCK_AFTER seconds
    if line and ERROR_RE.search(line):
        error_since.setdefault(akey, now)
        stuck = now - error_since[akey] >= STUCK_AFTER
        agent["state"] = "stuck" if stuck else "error"
    else:
        error_since.pop(akey, None)
        if float(agent.get("cpu") or 0) > WORKING_CPU or now - changed_at < WORKING_WINDOW:
            agent["state"] = "working"
        else:
            agent["state"] = "idle"

    # remember files this agent is editing (for collision alerts)
    if line and EDIT_RE.search(line):
        for f in FILE_RE.findall(line):
            recent_edits.setdefault(akey, {})[f.lstrip("./")] = now
    agent["agent_id"] = akey


def find_collisions(laptops: list[dict], now: float) -> list[dict]:
    """Two or more different agents editing the same file within COLLISION_WINDOW."""
    live = {}  # agent key -> (owner, agent name)
    for st in laptops:
        for a in st.get("agents", []):
            live[a.get("agent_id", "")] = (st.get("owner", "?"), a.get("name", "?"))
    by_file: dict[str, list[str]] = {}
    for akey, files in recent_edits.items():
        if akey not in live:
            continue
        for f, t in files.items():
            if now - t <= COLLISION_WINDOW:
                by_file.setdefault(f, []).append(akey)
    alerts = []
    for f, akeys in sorted(by_file.items()):
        if len(akeys) >= 2:
            who = " and ".join(f"{live[k][1]} ({live[k][0]})" for k in akeys)
            alerts.append({"kind": "collision", "file": f, "agents": akeys,
                           "text": f"{who} are both editing {f}"})
    return alerts


def count_line(laptops: list[dict]) -> str:
    """Plain team summary without Mistral, e.g. '3 working, 1 stuck, 2 idle'."""
    counts: dict[str, int] = {}
    for st in laptops:
        for a in st.get("agents", []):
            counts[a.get("state", "idle")] = counts.get(a.get("state", "idle"), 0) + 1
    if not counts:
        return "No agents running"
    order = ["working", "stuck", "error", "idle"]
    parts = [f"{counts[k]} {k}" for k in order if counts.get(k)]
    return ", ".join(parts) + f" across {len(laptops)} laptop(s)"


async def team_summary_loop() -> None:
    """Every TEAM_SUMMARY_EVERY seconds, one Mistral call for a one-line team headline."""
    global team_summary
    last_input = ""
    while True:
        await asyncio.sleep(TEAM_SUMMARY_EVERY)
        try:
            snap = snapshot()
            laptops = snap["laptops"]
            if not MISTRAL_API_KEY or not laptops:
                team_summary = count_line(laptops)
                continue
            lines = [
                f"{st.get('owner')}'s {a.get('name')}: {a.get('state')} - {a.get('summary')}"
                for st in laptops for a in st.get("agents", [])
            ]
            lines += [f"ALERT: {al['text']}" for al in snap["alerts"]]
            text = "\n".join(lines)
            if text == last_input:
                continue  # nothing changed, no API call
            last_input = text
            team_summary = await mistral_chat(
                "You are the team lead watching AI coding agents on several laptops. "
                "Write ONE short headline (max 15 words) about how the team is doing. "
                "Mention anything stuck or colliding first. No preamble, no emoji.",
                text[:3000],
            )
        except Exception as exc:
            print(f"[team_summary] fallback after error: {exc}")
            team_summary = count_line(snapshot()["laptops"])


def refresh_summaries() -> None:
    """Swap in summaries that finished since the status arrived."""
    for lkey, st in states.items():
        for a in st.get("agents", []):
            line = (a.get("last_log") or "").strip()
            cached = summary_cache.get((a.get("name", ""), line))
            if cached:
                a["summary"] = cached


def snapshot() -> dict:
    """All non-stale laptop states, ready to broadcast to viewers."""
    now = time.time()
    refresh_summaries()
    # staleness uses the server's own clock, so laptop clock drift can't hide a laptop
    laptops = [s for s in states.values() if now - s.get("_seen", 0) <= STALE_AFTER]
    return {
        "type": "state",
        "ts": now,
        "team_summary": team_summary or count_line(laptops),
        "alerts": find_collisions(laptops, now),
        "laptops": laptops,
    }


@app.websocket("/ws")
async def ws_reporter(websocket: WebSocket) -> None:
    """Receive status messages from reporters (schema: shared/schema.json)."""
    await websocket.accept()
    print(f"[server] reporter connected: {websocket.client}")
    try:
        while True:
            raw = await websocket.receive_text()
            try:
                data = json.loads(raw)
            except json.JSONDecodeError:
                continue
            now = time.time()
            data.setdefault("ts", now)
            data["_seen"] = now
            # key by Tailscale IP; fall back to hostname when the IP is
            # missing or just the loopback fallback from the reporter
            key = (data.get("tailscale_ip") or "").strip()
            if not key or key == "127.0.0.1":
                key = data.get("laptop") or "unknown"
            for a in data.get("agents", []):
                fill_agent(key, a, now)
            async with lock:
                states[key] = data
    except WebSocketDisconnect:
        print(f"[server] reporter disconnected: {websocket.client}")


@app.websocket("/view")
async def ws_view(websocket: WebSocket) -> None:
    """Broadcast the full laptop-state snapshot to every viewer, 2x per second."""
    await websocket.accept()
    print(f"[server] viewer connected: {websocket.client}")
    try:
        while True:
            async with lock:
                now = time.time()
                for k in [k for k, s in states.items() if now - s.get("_seen", 0) > STALE_AFTER]:
                    del states[k]
                msg = json.dumps(snapshot())
            await websocket.send_text(msg)
            await asyncio.sleep(0.5)
    except WebSocketDisconnect:
        print(f"[server] viewer disconnected: {websocket.client}")


@app.get("/api/state")
async def api_state() -> JSONResponse:
    async with lock:
        return JSONResponse(snapshot())


@app.post("/summarize")
async def api_summarize(payload: dict) -> dict:
    line = payload.get("last_log", "")
    try:
        return {"summary": await summarize(line)}
    except Exception:
        return {"summary": fallback(line)}


@app.get("/")
async def index():
    index_file = WEB_DIR / "index.html"
    if index_file.exists():
        return FileResponse(index_file)
    return JSONResponse({"ok": True, "note": "web/index.html not found yet — parallel work in progress"})


def serve() -> None:
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", "8000")))


if __name__ == "__main__":
    serve()
