"""Voice in the AR view: push-to-talk speech-to-text only (Voxtral realtime STT).

The browser streams mic audio straight to Mistral's realtime transcription websocket with a
short-lived rt_* token minted here (the Mistral API key never leaves the server). The page
then sends the transcript to the selected agent through POST /api/control action=redirect
(interrupt + new prompt). No TTS, no chat.

    POST /api/voice/token   {model} -> {token, expires_at, model}   [control key when OV_CONTROL_SECRET is set]
    POST /api/voice/log     browser voice debug log (printed, size-capped)
    GET  /api/voice/health  {"ok", "voice": STT available}
"""
from __future__ import annotations

import json
import os
from typing import Callable

import httpx
from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse

STT_MODEL = "voxtral-mini-transcribe-realtime-3"
VOICE_MODELS = {STT_MODEL}


def make_router(settings, require_control_key: Callable) -> APIRouter:
    router = APIRouter()
    base = os.environ.get("MISTRAL_API_BASE", "https://api.mistral.ai/v1")

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

    @router.post("/api/voice/token", dependencies=[Depends(require_control_key)])
    async def voice_token(req: Request) -> JSONResponse:
        """Mint a short-lived rt_* realtime token for the STT model (browser-safe)."""
        model = str((await body_of(req)).get("model") or STT_MODEL)
        if model not in VOICE_MODELS:
            return JSONResponse({"error": f"model not allowed: {model}"}, status_code=400)
        if not settings.mistral_api_key:
            return JSONResponse({"error": "MISTRAL_API_KEY not set"}, status_code=503)
        async with httpx.AsyncClient(timeout=30) as client:
            r = await client.post(f"{base}/client/sessions",
                                  headers={"Authorization": f"Bearer {settings.mistral_api_key}",
                                           "Content-Type": "application/json"},
                                  json={"purpose": "realtime", "model": model})
        if r.status_code >= 300:
            return JSONResponse({"error": f"token mint failed ({r.status_code})"}, status_code=502)
        data = r.json()
        return JSONResponse({"token": data["client_secret"]["value"], "expires_at": data.get("expires_at"),
                             "model": model})

    @router.get("/api/voice/health")
    async def voice_health() -> dict:
        return {"ok": True, "voice": bool(settings.mistral_api_key)}

    return router
