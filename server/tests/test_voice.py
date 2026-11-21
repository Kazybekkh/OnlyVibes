"""Voice (server/voice.py, STT only) + the redirect control action it feeds."""
from __future__ import annotations

import threading
import time
from pathlib import Path

import httpx

from test_server import make_client  # noqa: F401  (fixture)

K = {"X-OV-Key": "s3cret"}
STT = "voxtral-mini-transcribe-realtime-3"


def test_voice_token_needs_key_and_only_stt(make_client):
    c = make_client(control_secret="s3cret", mistral_api_key="sk-test")
    assert c.post("/api/voice/token", json={"model": STT}).status_code == 403
    assert c.post("/api/voice/token?key=wrong", json={"model": STT}).status_code == 403
    assert c.post("/api/voice/token", json={"model": "voxtral-mini-tts-3-0"}, headers=K).status_code == 400
    assert c.get("/api/voice/health").json()["voice"] is True
    assert c.post("/api/voice/log", json={"ev": "probe"}).json() == {"ok": True}
    # the Q&A / TTS routes are gone
    for path in ("/api/voice/chat", "/api/voice/turn", "/api/voice/config"):
        assert c.post(path, json={}, headers=K).status_code in (404, 405), path


def test_voice_token_without_mistral_key(make_client):
    c = make_client(control_secret="s3cret")
    assert c.post("/api/voice/token?key=s3cret", json={"model": STT}).status_code == 503
    assert c.get("/api/voice/health").json()["voice"] is False


def test_voice_token_mint_proxied(make_client, monkeypatch):
    seen = {}

    async def fake_post(self, url, headers=None, json=None, **kw):
        seen.update(url=url, auth=headers["Authorization"], body=json)
        return httpx.Response(200, json={"client_secret": {"value": "rt_abc"}, "expires_at": 123})

    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)
    c = make_client(control_secret="s3cret", mistral_api_key="sk-test")
    r = c.post("/api/voice/token", json={"model": STT}, headers=K)
    assert r.status_code == 200 and r.json() == {"token": "rt_abc", "expires_at": 123, "model": STT}
    assert seen["url"].endswith("/client/sessions") and seen["auth"] == "Bearer sk-test"
    assert seen["body"] == {"purpose": "realtime", "model": STT} and "sk-test" not in r.text


def test_redirect_forwarded_to_reporter(make_client):
    """Voice prompt -> /api/control redirect -> the target laptop's reporter (key required, text required)."""
    c = make_client(control_secret="s3cret")
    body = {"laptop": "100.0.0.8", "agent": "claude-code · OnlyVibes", "action": "redirect", "text": "run the tests"}
    assert c.post("/api/control", json=body).status_code == 403
    assert c.post("/api/control", json={**body, "text": ""}, headers=K).status_code == 400
    with c.websocket_connect("/ws") as rep:
        rep.send_json({"laptop": "kay-mbp", "owner": "kay", "tailscale_ip": "100.0.0.8", "ts": time.time(),
                       "agents": [{"name": "claude-code · OnlyVibes", "pid": 1, "cpu": 0, "cwd": "", "last_log": "",
                                   "summary": "", "kind": "session"}]})
        seen = {}

        def answer():
            msg = rep.receive_json()
            seen.update(msg)
            rep.send_json({"type": "control_result", "id": msg["id"], "ok": True,
                           "message": "interrupted claude-code · OnlyVibes and sent the new prompt"})

        th = threading.Thread(target=answer)
        th.start()
        r = c.post("/api/control", json=body, headers=K)
        th.join(5)
    assert r.status_code == 200 and r.json()["ok"] is True
    assert seen["action"] == "redirect" and seen["text"] == "run the tests" and seen["agent"] == body["agent"]


def test_redirect_task_card_goes_to_inbox(make_client):
    c = make_client(control_secret="s3cret")
    r = c.post("/api/control", json={"task_id": "nope", "action": "redirect", "text": "x"}, headers=K)
    assert r.status_code == 404


def test_voice_pages_served(make_client):
    c = make_client(web_dir=Path(__file__).resolve().parents[2] / "web")
    for path in ("/mic", "/probe", "/voice.js", "/ar"):
        assert c.get(path).status_code == 200, path
    ar = c.get("/ar").text
    assert "Hold to talk" in ar and "voice.js" in ar and "action: 'redirect'" in ar
    assert "action: 'redirect'" in c.get("/mic").text
