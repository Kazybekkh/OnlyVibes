"""Voice routes (server/voice.py): key gating, validation, grounding, pages."""
from __future__ import annotations

import time

import httpx

import voice as voice_mod
from test_server import make_client  # noqa: F401  (fixture)

K = {"X-OV-Key": "s3cret"}


def test_voice_spending_routes_need_the_key(make_client):
    c = make_client(control_secret="s3cret", mistral_api_key="sk-test")
    for path, body in [("/api/voice/token", {"model": "voxtral-mini-tts-3-0"}),
                       ("/api/voice/chat", {"messages": [{"role": "user", "content": "hi"}]}),
                       ("/api/voice/turn", {"heard": "x", "reply": "y"}),
                       ("/api/voice/config", {"toggle": "mic"})]:
        assert c.post(path, json=body).status_code == 403, path
        assert c.post(path + "?key=wrong", json=body).status_code == 403, path
    # reading stays open
    assert c.get("/api/voice/health").json()["voice"] is True
    assert c.get("/api/voice/config").json()["mic"] in ("browser", "web")
    assert c.post("/api/voice/log", json={"ev": "probe"}).json() == {"ok": True}


def test_voice_token_validation_and_no_key(make_client):
    c = make_client(control_secret="s3cret")
    assert c.post("/api/voice/token", json={"model": "gpt-4"}, headers=K).status_code == 400
    assert c.post("/api/voice/token?key=s3cret", json={"model": "voxtral-mini-tts-3-0"}).status_code == 503
    assert c.get("/api/voice/health").json()["voice"] is False


def test_voice_token_mint_proxied(make_client, monkeypatch):
    seen = {}

    async def fake_post(self, url, headers=None, json=None, **kw):
        seen.update(url=url, auth=headers["Authorization"], body=json)
        return httpx.Response(200, json={"client_secret": {"value": "rt_abc"}, "expires_at": 123})

    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)
    c = make_client(control_secret="s3cret", mistral_api_key="sk-test")
    r = c.post("/api/voice/token", json={"model": "voxtral-mini-transcribe-realtime-3"}, headers=K)
    assert r.status_code == 200 and r.json() == {"token": "rt_abc", "expires_at": 123,
                                                 "model": "voxtral-mini-transcribe-realtime-3"}
    assert seen["url"].endswith("/client/sessions") and seen["auth"] == "Bearer sk-test"
    assert "sk-test" not in r.text


def test_voice_turn_and_config_reach_view(make_client):
    c = make_client(control_secret="s3cret")
    assert c.post("/api/voice/config", json={"mic": "nope"}, headers=K).status_code == 400
    cfg = c.post("/api/voice/config", json={"mic": "web", "speaker": "browser"}, headers=K).json()
    assert cfg["mic"] == "web" and cfg["speaker"] == "browser"
    tid = c.post("/api/voice/turn", json={"heard": "who is stuck", "reply": "nobody", "source_role": "web",
                                          "speak_on": ["browser", "evil"]}, headers=K).json()["id"]
    with c.websocket_connect("/view") as v:
        snap = v.receive_json()
    assert snap["voice"]["id"] == tid and snap["voice"]["speak_on"] == ["browser"]
    assert snap["voice_config"]["mic"] == "web"


def test_voice_context_is_grounded_on_live_agents(make_client):
    c = make_client()
    with c.websocket_connect("/ws") as rep:
        rep.send_json({"laptop": "kay-mbp", "owner": "kay", "tailscale_ip": "100.0.0.9", "ts": time.time(),
                       "agents": [{"name": "claude-code · OnlyVibes", "pid": 1, "cpu": 3, "cwd": "/x/OnlyVibes",
                                   "last_log": "", "summary": "", "kind": "session", "task": "fix AR voice"}]})
        time.sleep(0.2)
        hub = c.app.state.hub
        txt = voice_mod.voice_context(hub.legacy_laptops(time.time()))
    assert "kay" in txt and "claude-code · OnlyVibes" in txt and "fix AR voice" in txt and "OnlyVibes" in txt


def test_voice_pages_served(make_client):
    from pathlib import Path
    c = make_client(web_dir=Path(__file__).resolve().parents[2] / "web")
    for path in ("/mic", "/probe", "/voice.js", "/ar"):
        assert c.get(path).status_code == 200, path
    assert "Hold to talk" in c.get("/ar").text and "voice.js" in c.get("/ar").text
