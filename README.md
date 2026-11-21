# OnlyVibes

Live AR/3D view of **which AI coding agents (Vibe, Claude Code, Codex, Cursor) are running on each teammate's laptop** — all laptops on one Tailscale network.

```
┌─────────────────┐    WS /ws     ┌──────────────────────┐   WS /view   ┌────────────────────┐
│ reporter.py     │ ─────────────> │ server (FastAPI)     │ ────────────> │ web/index.html     │
│ on each laptop  │  status 1/s   │  /ws  /view /api     │  broadcast   │ Three.js table +   │
│ (psutil + logs) │               │  summarize() ──> Mistral Small        │ WebXR "Enter AR"   │
└─────────────────┘               └──────────────────────┘               └────────────────────┘
```

## Repo layout

```
OnlyVibes/
├── shared/
│   └── schema.json        # the status message contract (see below)
├── reporter/              # uv project — runs on EACH laptop
│   ├── pyproject.toml
│   ├── reporter.py        # real: detects agent processes (psutil), tails latest log line
│   └── mock_reporter.py   # fake: streams 3 laptops' worth of data, no agents needed
├── server/                # uv project — runs on ONE laptop (the hub)
│   ├── pyproject.toml
│   └── main.py            # /ws (ingest), /view (broadcast), /api/state, /summarize, serves web/
└── web/
    └── index.html         # Three.js 3D table, one box per laptop, floating text card per agent, WebXR AR
```

Each part is independent and runnable alone with fake data, so 4 people can work in parallel:
**schema+README**, **reporter**, **server**, **web**.

## Quickstart (demo with fake data, 2 minutes)

```bash
# terminal 1 — the server (hub)
cd server
uv run uvicorn main:app --host 0.0.0.0 --port 8000
# or: uv run main.py

# terminal 2 — fake reporters for 3 laptops (no real agents needed)
cd reporter
SERVER_URL=ws://localhost:8000/ws uv run mock_reporter.py

# terminal 3 — open the 3D view
open http://localhost:8000/        # macOS; or browse to it
# debug snapshot: curl http://localhost:8000/api/state
```

## Real deployment over Tailscale

On the hub laptop:

```bash
cd server
MISTRAL_API_KEY=sk-... uv run uvicorn main:app --host 0.0.0.0 --port 8000
# for WebXR AR you need HTTPS:  tailscale serve 8000   (then open https://<hub>.<tailnet>.ts.net/)
```

On **each teammate's laptop**:

```bash
cd reporter
SERVER_URL=ws://<hub-tailscale-ip>:8000/ws uv run reporter.py
# the reporter auto-detects its Tailscale IP via `tailscale ip -4`
```

Open the viewer from any device on the Tailnet: `http://<hub-tailscale-ip>:8000/` (or the `tailscale serve` HTTPS URL for AR).

## Env vars

| Var               | Used by  | Default                    | Purpose                                             |
|-------------------|----------|----------------------------|-----------------------------------------------------|
| `SERVER_URL`      | reporter | `ws://localhost:8000/ws`   | websocket endpoint of the server's `/ws`            |
| `INTERVAL`        | reporter | `1`                        | seconds between status reports                      |
| `MISTRAL_API_KEY` | server   | *(unset)*                  | enables `summarize()` via Mistral; without it the stub returns the truncated raw log line |
| `MISTRAL_MODEL`   | server   | `mistral-small-latest`     | model used by `summarize()`                         |
| `PORT`            | server   | `8000`                     | listen port when run as `uv run main.py`            |
| `STALE_AFTER`     | server   | `5`                        | seconds before a silent laptop disappears from the view |
| `VOICE_MIC`       | server   | `browser`                  | which role may talk: `browser` (Zapbox headset phone) or `web` (fallback `/mic` page) |
| `VOICE_SPEAKER`   | server   | `browser`                  | where replies are spoken: `browser`, `web`, `mic` (same as mic), or `all` |
| `VOICE_CHAT_MODEL`| server   | `MISTRAL_MODEL`            | LLM behind `/api/voice/chat` (grounded in the live snapshot) |
| `MISTRAL_API_BASE`| server   | `https://api.mistral.ai/v1`| Mistral API base (token mint + chat + SSE)              |

## Voice in Zapbox Browser (AR)

A person wearing a Zapbox headset (iPhone inside) opens the hub in Zapbox Browser, taps **Enable voice**, enters AR,
then **holds A/X (gamepad button 4)** to ask out loud and hears a grounded spoken answer on the iPhone, with a
head-locked "you said / reply" card. Replies are grounded in the live agent data via `/api/voice/chat`.

```bash
cd server && uv run uvicorn main:app --host 0.0.0.0 --port 8000
tailscale serve --bg 8000        # secure context required for mic + WebXR
# iPhone → Zapbox Browser → https://<hub>.<tailnet>.ts.net/?role=browser
```

- Bookmark `https://<hub>.<tailnet>.ts.net/?role=browser`; tap **Enable voice** (mic permission) before **Enter AR**.
- In AR: hold A/X to talk, release to send; **long-press B/Y (1 s)** toggles `mic: browser ⇄ web`.
- Fallback mic when the headset browser blocks the mic: open `https://<hub>/mic` on another phone, set `mic=web`,
  `speaker=browser` → talk into that phone, reply speaks in the headset.
- Web toggle bar (bottom-right) switches mic source and speaker at runtime (`POST /api/voice/config`).
- URL params: `?role=browser|web`, `?ptt=4|trigger`, `?switch=5`. Capability probe page: `/probe`
  (results recorded in `voice/ZAPBOX_PROBE.md`).
- Browser never sees the API key: `/api/voice/token` mints short-lived `rt_*` tokens
  (`POST /v1/client/sessions`), and the page talks to the Voxtral realtime STT/TTS websockets directly
  (`voxtral-mini-transcribe-realtime-3` / `voxtral-mini-tts-3-0`, PCM float32 @24 kHz).
- Implementation plan: `voice/ZAPBOX_VOICE_PLAN.md`.

## Message schema (`shared/schema.json`)

One status message per laptop per second, sent to `WS /ws`:

```json
{
  "laptop": "kay-mbp",
  "owner": "kay",
  "tailscale_ip": "100.64.0.11",
  "ts": 1761998400.12,
  "agents": [
    {
      "name": "vibe",
      "pid": 3001,
      "cpu": 42.1,
      "cwd": "/Users/kay/Developer/OnlyVibes",
      "last_log": "⏺ Edit server/main.py",
      "summary": "editing the FastAPI server"
    }
  ]
}
```

- `reporter` fills everything except `summary` (sent empty).
- The server fills `summary` via `summarize()` (Mistral Small: one plain-English line from `last_log`), stores the latest message per `tailscale_ip`, and rebroadcasts all states on `WS /view` as `{"type":"state","ts":...,"laptops":[...]}` at 2 Hz.
- The web page renders one box per laptop on a 3D table and a floating text card per agent.

## WebXR / AR notes

- WebXR requires a **secure context**: `localhost` or HTTPS. On Tailscale use `tailscale serve 8000` on the hub and open the `https://....ts.net` URL on your headset/phone.
- The **📱 Enter AR** button requests an `immersive-ar` session (with `hit-test` + `local-floor`); on unsupported browsers it stays hidden and the desktop 3D view keeps working.
- Opening `web/index.html` standalone (no server) shows fake data after ~2.5s, so the frontend can be developed alone.

## Hackathon division of labor

| Person | Owns | Runs alone via |
|--------|------|----------------|
| 1 | `shared/schema.json` + README | — |
| 2 | `reporter/` | `SERVER_URL=ws://localhost:8000/ws uv run mock_reporter.py` |
| 3 | `server/` | `uv run uvicorn main:app --port 8000` + `curl localhost:8000/api/state` |
| 4 | `web/` | open `web/index.html` in a browser (fake-data fallback) |
