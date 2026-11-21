"""Voice in the AR view (Ron's voice-integration, ported onto the hub app).

Voxtral realtime STT/TTS run browser <-> Mistral directly with short-lived rt_* tokens minted
here; the grounded chat is proxied here. The Mistral API key never leaves the server.
Anything that spends the key (token, chat) or changes shared voice state (turn, config)
requires the control key when OV_CONTROL_SECRET is set (?key= or X-OV-Key).

    POST /api/voice/log        browser voice debug log (printed, size-capped)
    POST /api/voice/token      {model} -> {token, expires_at, model}          [key]
    POST /api/voice/chat       {messages} -> SSE, grounded on the live state   [key]
    POST /api/voice/turn       store the latest turn for /view clients         [key]
    GET  /api/voice/config     mic + speaker roles
    POST /api/voice/config     change them                                     [key]
    GET  /api/voice/health     availability + config
"""
from __future__ import annotations

import json
import os
import time
from typing import Callable

import httpx
from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, StreamingResponse

VOICE_MODELS = {"voxtral-mini-transcribe-realtime-3", "voxtral-mini-tts-3-0"}
VOICE_ROLES = ["browser", "web"]
SPEAKER_OPTIONS = {"mic", "all", "browser", "web"}

VOICE_SYSTEM = (
    "You are OnlyVibes, a voice assistant inside an AR view of the team's AI coding agents. "
    "Answer ONLY from the live data below; if it is not there, say you don't see it. "
    "Reply in one to three short spoken sentences: no markdown, lists, emoji or code; "
    "say file names naturally. Mention stuck agents and collisions first when asked how things are going.\n\n"
    "LIVE DATA:\n"
)


def _env_choice(name: str, default: str, allowed) -> str:
    v = os.environ.get(name, default)
    return v if v in allowed else default


class VoiceState:
    def __init__(self) -> None:
        self.turn: dict = {}
        self.config = {"mic": _env_choice("VOICE_MIC", "browser", VOICE_ROLES),
                       "speaker": _env_choice("VOICE_SPEAKER", "browser", SPEAKER_OPTIONS), "rev": 0}


def voice_context(laptops: list[dict]) -> str:
    """Plain-text digest of the live laptops/agents, injected into every voice chat call."""
    lines: list[str] = []
    n = sum(len(l.get("agents") or []) for l in laptops)
    lines.append(f"{len(laptops)} laptop(s), {n} agent(s) running")
    for st in laptops:
        who = f"{st.get('owner') or '?'} ({st.get('laptop')})"
        dev = st.get("device") or {}
        if dev.get("model") or dev.get("os"):
            lines.append(f"{who} uses {' '.join(x for x in (dev.get('model'), dev.get('os')) if x)}")
        agents = st.get("agents") or []
        if not agents:
            lines.append(f"{who}: no agents running")
        for a in agents:
            proj = (a.get("cwd") or "").rstrip("/").rsplit("/", 1)[-1]
            bits = [f"{who}: {a.get('name')}"]
            if a.get("kind") == "subagent":
                bits.append(f"(subagent of {a.get('parent')})")
            if a.get("state"):
                bits.append(f"is {a['state']}")
            if proj:
                bits.append(f"in project {proj}")
            what = a.get("task") or a.get("summary") or a.get("last_log") or ""
            if what:
                bits.append(f"- {what}")
            lines.append(" ".join(bits))
    return "\n".join(lines)[:4000]


def make_router(settings, hub, require_control_key: Callable, now: Callable[[], float]) -> tuple[APIRouter, VoiceState]:
    router = APIRouter()
    state = VoiceState()
    base = os.environ.get("MISTRAL_API_BASE", "https://api.mistral.ai/v1")
    chat_model = os.environ.get("VOICE_CHAT_MODEL", settings.mistral_model)
    locked = [Depends(require_control_key)]

    def key() -> str:
        return settings.mistral_api_key

    async def body_of(req: Request) -> dict:
        try:
            b = await req.json()
        except Exception:
            return {}
        return b if isinstance(b, dict) else {}

    @router.post("/api/voice/log")
    async def voice_log(req: Request) -> dict:
        print(f"[voice-log] {json.dumps(await body_of(req))[:2000]}")
        return {"ok": True}

    @router.post("/api/voice/token", dependencies=locked)
    async def voice_token(req: Request) -> JSONResponse:
        """Mint a short-lived rt_* realtime token for one Voxtral model (browser-safe)."""
        model = str((await body_of(req)).get("model", ""))
        if model not in VOICE_MODELS:
            return JSONResponse({"error": f"model not allowed: {model}"}, status_code=400)
        if not key():
            return JSONResponse({"error": "MISTRAL_API_KEY not set"}, status_code=503)
        async with httpx.AsyncClient(timeout=30) as client:
            r = await client.post(f"{base}/client/sessions",
                                  headers={"Authorization": f"Bearer {key()}", "Content-Type": "application/json"},
                                  json={"purpose": "realtime", "model": model})
        if r.status_code >= 300:
            return JSONResponse({"error": f"token mint failed ({r.status_code})"}, status_code=502)
        data = r.json()
        return JSONResponse({"token": data["client_secret"]["value"], "expires_at": data.get("expires_at"),
                             "model": model})

    @router.post("/api/voice/chat", dependencies=locked)
    async def voice_chat(req: Request):
        """SSE proxy for streaming chat completions, grounded on the live agent data."""
        if not key():
            return JSONResponse({"error": "MISTRAL_API_KEY not set"}, status_code=503)
        body = await body_of(req)
        history = [{"role": m["role"], "content": str(m.get("content", ""))[:2000]}
                   for m in body.get("messages", []) or []
                   if isinstance(m, dict) and m.get("role") in ("user", "assistant")][-12:]
        async with hub.lock:
            laptops = hub.legacy_laptops(now())
        messages = [{"role": "system", "content": VOICE_SYSTEM + voice_context(laptops)}] + history

        async def gen():
            async with httpx.AsyncClient(timeout=120) as client:
                async with client.stream(
                    "POST", f"{base}/chat/completions",
                    headers={"Authorization": f"Bearer {key()}", "Content-Type": "application/json"},
                    json={"model": chat_model, "messages": messages, "stream": True, "temperature": 0.3},
                ) as r:
                    if r.status_code >= 300:
                        yield f"data: {json.dumps({'error': f'chat failed ({r.status_code})'})}\n\n"
                        return
                    async for line in r.aiter_lines():
                        if line.startswith("data:"):
                            yield f"{line}\n\n"

        return StreamingResponse(gen(), media_type="text/event-stream")

    @router.post("/api/voice/turn", dependencies=locked)
    async def voice_turn(req: Request) -> dict:
        p = await body_of(req)
        state.turn = {"id": int(time.time() * 1000),
                      "heard": str(p.get("heard", ""))[:500],
                      "reply": str(p.get("reply", ""))[:1000],
                      "source_role": p.get("source_role") if p.get("source_role") in VOICE_ROLES else "web",
                      "client_id": str(p.get("client_id", ""))[:64],
                      "speak_on": [r for r in (p.get("speak_on") or []) if r in VOICE_ROLES],
                      "ts": time.time()}
        return {"ok": True, "id": state.turn["id"]}

    @router.get("/api/voice/config")
    async def voice_config_get() -> dict:
        return state.config

    @router.post("/api/voice/config", dependencies=locked)
    async def voice_config_set(req: Request) -> JSONResponse:
        p = await body_of(req)
        mic, speaker = p.get("mic"), p.get("speaker")
        if p.get("toggle") == "mic":
            mic = "web" if state.config["mic"] == "browser" else "browser"
        if mic is not None and mic not in VOICE_ROLES:
            return JSONResponse({"error": f"mic must be one of {VOICE_ROLES}"}, status_code=400)
        if speaker is not None and speaker not in SPEAKER_OPTIONS:
            return JSONResponse({"error": f"speaker must be one of {sorted(SPEAKER_OPTIONS)}"}, status_code=400)
        if mic:
            state.config["mic"] = mic
        if speaker:
            state.config["speaker"] = speaker
        state.config["rev"] += 1
        print(f"[voice] config -> {state.config}")
        return JSONResponse(state.config)

    @router.get("/api/voice/health")
    async def voice_health() -> dict:
        return {"ok": True, "voice": bool(key()), "config": state.config}

    return router, state
