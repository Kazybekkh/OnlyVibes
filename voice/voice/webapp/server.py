"""OnlyVibes voice webapp — browser-based near-real-time voice chat.

No terminal, no macOS Accessibility permission: the browser asks for the mic
via getUserMedia (a normal permission prompt). The Mistral API key stays on
this server:

  browser ──rt_* token──► wss://api.mistral.ai/v1/audio/transcriptions/realtime (STT)
  browser ──rt_* token──► wss://api.mistral.ai/v1/audio/speech/realtime       (TTS)
  browser ──POST /api/chat (SSE proxy)──► /v1/chat/completions              (LLM)

Run from the repo root:
  uv run python -m voice.webapp            # serves http://localhost:8080
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from ..voxtral import API_BASE, load_dotenv

STATIC = Path(__file__).parent / "static"

load_dotenv()
app = FastAPI(title="OnlyVibes voice webapp")


def _api_key() -> str:
    key = os.environ.get("MISTRAL_API_KEY")
    if not key:
        raise RuntimeError("MISTRAL_API_KEY not set (see voice/.env)")
    return key


@app.get("/api/health")
async def health() -> dict:
    return {"ok": True, "api_key_present": bool(os.environ.get("MISTRAL_API_KEY"))}


@app.post("/api/token")
async def mint_token(req: Request) -> JSONResponse:
    """Mint a short-lived rt_* realtime token for one model (browser-safe)."""
    body = await req.json()
    model = body.get("model", "")
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.post(
            f"{API_BASE}/client/sessions",
            headers={"Authorization": f"Bearer {_api_key()}",
                     "Content-Type": "application/json"},
            json={"purpose": "realtime", "model": model},
        )
    if r.status_code >= 300:
        return JSONResponse({"error": r.text}, status_code=r.status_code)
    data = r.json()
    return JSONResponse({
        "token": data["client_secret"]["value"],
        "expires_at": data.get("expires_at"),
        "model": model,
    })


@app.post("/api/chat")
async def chat_stream(req: Request):
    """SSE proxy for streaming chat completions (key never reaches the browser)."""
    body = await req.json()
    messages = body.get("messages", [])
    model = body.get("model", "mistral-small-latest")

    async def gen():
        async with httpx.AsyncClient(timeout=120) as client:
            async with client.stream(
                "POST", f"{API_BASE}/chat/completions",
                headers={"Authorization": f"Bearer {_api_key()}",
                         "Content-Type": "application/json"},
                json={"model": model, "messages": messages, "stream": True},
            ) as r:
                r.raise_for_status()
                async for line in r.aiter_lines():
                    if line.startswith("data:"):
                        yield f"{line}\n\n"

    return StreamingResponse(gen(), media_type="text/event-stream")


@app.get("/")
async def index():
    return FileResponse(STATIC / "index.html")


app.mount("/static", StaticFiles(directory=STATIC), name="static")


def main() -> None:
    import uvicorn
    port = int(os.environ.get("PORT", "8090"))
    uvicorn.run("voice.webapp.server:app", host="0.0.0.0", port=port, reload=False)


if __name__ == "__main__":
    main()
