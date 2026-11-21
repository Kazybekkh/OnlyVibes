#!/usr/bin/env python3
"""
OnlyVibes server.

Endpoints:
    WS   /ws         <- reporters stream laptop status messages (shared/schema.json)
    WS   /view       -> viewers (web/index.html) get a broadcast of all laptop states, 2x/s
    GET  /api/state  -> same snapshot as JSON (debugging)
    POST /summarize  -> {"last_log": "..."} -> {"summary": "..."} via Mistral Small
    GET  /           -> serves ../web/index.html (Three.js / WebXR viewer)

Env vars:
    MISTRAL_API_KEY  enables summarize() through the Mistral API; without it the
                     stub falls back to a truncated raw log line
    MISTRAL_MODEL    model used by summarize() (default mistral-small-latest)
    PORT             listen port when run as `uv run main.py` (default 8000)
    STALE_AFTER      seconds without updates before a laptop is dropped (default 5)

Run:
    cd server
    uv run uvicorn main:app --host 0.0.0.0 --port 8000
    # or: uv run main.py
"""
from __future__ import annotations

import asyncio
import json
import os
import time
from pathlib import Path

import httpx
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse

MISTRAL_API_KEY = os.environ.get("MISTRAL_API_KEY", "")
MISTRAL_MODEL = os.environ.get("MISTRAL_MODEL", "mistral-small-latest")
STALE_AFTER = float(os.environ.get("STALE_AFTER", "5"))

WEB_DIR = Path(__file__).resolve().parent.parent / "web"

app = FastAPI(title="OnlyVibes server")

# key = tailscale_ip (fallback: laptop hostname) -> latest status message
states: dict[str, dict] = {}
lock = asyncio.Lock()

# summary cache: (laptop_key, agent_name) -> (last_log, summary).
# Mistral is only called when last_log CHANGED since the last call.
summary_cache: dict[tuple[str, str], tuple[str, str]] = {}
# rate limit: (laptop_key, agent_name) -> ts of last Mistral call
# (at most 1 Mistral call per agent per RATE_LIMIT_S seconds)
summary_rate: dict[tuple[str, str], float] = {}
RATE_LIMIT_S = 5.0


async def get_summary(laptop_key: str, agent: dict) -> str:
    """Summary for one agent, using the cache + rate limit around summarize()."""
    name = agent.get("name", "")
    log = (agent.get("last_log") or "").strip()
    key = (laptop_key, name)
    cached = summary_cache.get(key)
    if cached and cached[0] == log:
        return cached[1]  # unchanged log -> reuse cached summary, no API call
    if time.time() - summary_rate.get(key, 0.0) < RATE_LIMIT_S:
        # rate-limited: reuse the previous summary (or a local fallback), no call
        return cached[1] if cached else log[:120]
    summary_rate[key] = time.time()
    print(f"[summarize] Mistral call for {laptop_key}/{name}")
    s = await summarize(log)
    summary_cache[key] = (log, s)
    return s


async def summarize(last_log: str) -> str:
    """
    Turn one raw agent log line into a single plain-English line using
    Mistral Small. Stub behavior: without MISTRAL_API_KEY (or on any API
    error) it returns the truncated raw line, so the demo works keyless.
    """
    line = (last_log or "").strip()
    if not line:
        return ""
    if not MISTRAL_API_KEY:
        return line[:120]
    try:
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
                        {
                            "role": "system",
                            "content": (
                                "You summarize one log line from an AI coding agent "
                                "into a single short plain-English sentence (max 12 words). "
                                "No preamble, no quotes, no emoji."
                            ),
                        },
                        {"role": "user", "content": line[:500]},
                    ],
                    "temperature": 0.2,
                },
            )
        resp.raise_for_status()
        return resp.json()["choices"][0]["message"]["content"].strip()
    except Exception as exc:  # never let summarization break ingestion
        print(f"[summarize] fallback after error: {exc}")
        return line[:120]


def snapshot() -> dict:
    """All non-stale laptop states, ready to broadcast to viewers."""
    now = time.time()
    return {
        "type": "state",
        "ts": now,
        "laptops": [s for s in states.values() if now - s.get("ts", 0) <= STALE_AFTER],
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
            data.setdefault("ts", time.time())
            key = data.get("tailscale_ip") or data.get("laptop") or "unknown"
            # fill in summaries for agents that arrived without one,
            # going through the cache + rate limiter (get_summary)
            todo = [a for a in data.get("agents", []) if not a.get("summary") and a.get("last_log")]
            if todo:
                sums = await asyncio.gather(*(get_summary(key, a) for a in todo))
                for a, s in zip(todo, sums):
                    a["summary"] = s
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
                for k in [k for k, s in states.items() if now - s.get("ts", 0) > STALE_AFTER]:
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
    return {"summary": await summarize(payload.get("last_log", ""))}


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
