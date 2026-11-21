# OnlyVibes — Voice in Zapbox Browser (AR) — Implementation Plan

Self-contained hand-off for an implementing agent. Read all of section 0 before writing code.
Do **not** commit unless the user asks. Do **not** add dependencies.

---

## 0. Context

### 0.1 Goal
A person wearing a **Zapbox** headset, viewing the OnlyVibes AR table in the **Zapbox Browser** app, can **hold a
controller button, ask out loud** ("what is Kay's Claude doing?", "is anyone stuck?") and **hear a spoken answer**
grounded in the live agent data, with a floating "you said / reply" card in front of their eyes.

The **mic source is switchable at runtime**:

| Mic mode | Who captures audio | Page |
|---|---|---|
| `browser` (default) | iPhone mic, via `getUserMedia` inside Zapbox Browser | `https://<hub>/?role=browser` in Zapbox Browser |
| `web` (fallback) | any other browser mic: laptop page, or a second phone on `/mic` | `https://<hub>/` or `https://<hub>/mic` |

Replies are spoken where the `speaker` setting says (default: the headset iPhone), see §1.3.

### 0.2 Zapbox facts (researched — do not re-assume)
Sources: zappar.com/zapbox/faq, /zapbox/zapbox-browser, Zappar's 2022 post to the W3C immersive-web list.

- **The headset has no electronics, mic or speaker.** It is a lens shell holding an **iPhone 11 or later**
  (needs the ultra-wide camera); Android is **not** supported. Only the **2 Bluetooth LE controllers** are powered
  (trigger, grip, thumbstick, A/B or X/Y buttons, IMU). ⇒ **Mic and speaker are the iPhone's** (or AirPods
  paired to it).
- **Zapbox Browser** (iOS app) runs WebXR pages; `immersive-ar` is rendered in stereo with the camera feed
  composited behind. Controllers are WebXR input sources (Oculus-Touch-like layout).
- **Undocumented, must be probed (Phase 1):** whether pages may use the mic (`getUserMedia`) at all, whether the
  mic keeps working inside an AR session, whether audio plays in AR, whether `hit-test` is supported, gamepad
  button indices.
- Zapbox **Link** (PC VR streaming via NVIDIA CloudXR) is **out of scope**.

### 0.3 What already exists in the repo (read these files first)
Repo root: `/Users/Rohithn/AI/experiments/mistralvibehack/OnlyVibes`

| File | What it is |
|---|---|
| `server/main.py` | FastAPI hub. `WS /ws` (reporters in), `WS /view` (broadcast `snapshot()` 2×/s), `GET /api/state`, `POST /summarize`, `GET /` serves `web/index.html`. Has `MISTRAL_API_KEY`, `MISTRAL_MODEL`, `httpx`, `snapshot()`, `WEB_DIR`. Run: `cd server && uv run uvicorn main:app --host 0.0.0.0 --port 8000`. |
| `web/index.html` | Three.js scene: table, one laptop per reporter, floating text cards (`makeCard`, `setCardText`), `ARButton` with `optionalFeatures: ['hit-test','local-floor']`; `sessionstart` shrinks `world` to 0.15 scale, 0.8 m ahead. Fake-data fallback when no server. |
| `voice/voice/webapp/server.py` | Reference: `POST /api/token` (mints `rt_*` token via `POST https://api.mistral.ai/v1/client/sessions` with `{"purpose":"realtime","model":...}` → `data["client_secret"]["value"]`) and `POST /api/chat` (SSE proxy). |
| `voice/voice/webapp/static/index.html` | Reference browser client — **reuse its JS**: mic AudioWorklet (16 kHz int16 + noise gate), Voxtral realtime STT WS, realtime TTS WS, gapless 24 kHz float32 AudioWorklet player, push-to-talk turn logic (`beginTurn`, `stopListening`, `runReply`, `endTurn`, `interrupt`). |
| `reporter/mock_reporter.py` | Fake laptops: `cd reporter && SERVER_URL=ws://localhost:8000/ws uv run mock_reporter.py` |

Voxtral constants used by the reference client (keep identical):
`STT_MODEL="voxtral-mini-transcribe-realtime-3"`, `TTS_MODEL="voxtral-mini-tts-3-0"`, `TTS_RATE=24000`
(**float32 LE** PCM), `MIC_RATE=16000` (int16 PCM), `WS_BASE="wss://api.mistral.ai/v1"`,
STT WS `${WS_BASE}/audio/transcriptions/realtime?model=...`, TTS WS `${WS_BASE}/audio/speech/realtime?model=...`,
subprotocols `["realtime", token]`.

### 0.4 Rules
- Mistral API key never reaches the browser; only short-lived `rt_*` tokens.
- Standalone `web/index.html` (no server, fake data) must keep working; voice hides itself.
- Mic and WebXR need a **secure context** → `tailscale serve` HTTPS on the hub (or `localhost` for desktop tests).
- Keep code compact, match existing style (section comments `// ---- name`).

---

## 1. Design

### 1.1 Architecture
```
hub (Mac): server/main.py :8000 ── tailscale serve ──► https://<hub>.<tailnet>.ts.net
  /api/voice/{token,chat,turn,config,health,log}  /voice.js  /mic  /probe   WS /view (+voice, +voice_config)

iPhone in Zapbox ─ Zapbox Browser ─ https://<hub>/?role=browser
  [tap "Enable voice" once, before AR]  → getUserMedia + AudioContexts created & resumed
  [Enter AR]
  hold A/X   → mic PCM ──► wss://api.mistral.ai/v1/audio/transcriptions/realtime  (rt_* token)
  release    → transcript ──► POST /api/voice/chat  (server injects live agent state) ──► SSE tokens
  tokens     ──► wss://api.mistral.ai/v1/audio/speech/realtime ──► PCM ──► iPhone speaker / AirPods
  heard+reply → head-locked 3D card, and POST /api/voice/turn → included in /view for other clients

fallback mic: laptop https://<hub>/  or second phone https://<hub>/mic  (role=web)
```

### 1.2 Roles
`const ROLE = params.get('role') || 'web'` — `browser` or `web`.
- Zapbox bookmark: `https://<hub>.<tailnet>.ts.net/?role=browser`
- `/mic` page is always `web`.
- Optional: if the Phase 1 probe shows a distinctive Zapbox user-agent marker, default `ROLE='browser'` when present.

Each page load creates `CLIENT_ID = crypto.randomUUID?.() || String(Math.random()).slice(2)`.

### 1.3 Shared config (server-held, broadcast via `/view`)
`voice_config = {"mic": "browser"|"web", "speaker": "mic"|"browser"|"web"|"all", "rev": int}`
- `mic`: only clients whose `ROLE === mic` may start a turn; others show a hint card.
- `speaker`: where replies are spoken. `mic` = same role as the mic; `all` = both roles.
  `speak_on = speaker=='mic' ? [mic] : speaker=='all' ? ['browser','web'] : [speaker]`.
- Defaults from env `VOICE_MIC` (default `browser`) and `VOICE_SPEAKER` (default `browser`, so the headset
  always hears replies); runtime changes are in-memory only.
- Switch from: web voice bar toggle, controller **B/Y long-press (1 s)** in AR, or curl.

### 1.4 Who speaks a reply
- Producer (mic client) streams TTS locally iff `speak_on.includes(ROLE)` (lowest latency).
- Any other client speaks a `/view` turn iff `turn.client_id !== CLIENT_ID && turn.source_role !== ROLE &&
  turn.speak_on.includes(ROLE)`.
- Known limitation (document it): two tabs with the same role both speak.

---

## 2. Phase 1 — Capability probe (do first)

### 2.1 Server: log + probe routes (`server/main.py`)
```python
@app.post("/api/voice/log")
async def api_voice_log(payload: dict) -> dict:
    print(f"[voice-log] {json.dumps(payload)[:2000]}")
    return {"ok": True}


@app.get("/probe")
async def probe():
    return FileResponse(WEB_DIR / "probe.html")
```

### 2.2 `web/probe.html` (new, standalone)
Inside AR the DOM is invisible, so **every result also goes to the server log**:
```js
const log = (o) => { pre.textContent += JSON.stringify(o) + '\n';
  fetch('/api/voice/log', { method: 'POST', headers: {'Content-Type':'application/json'},
    body: JSON.stringify({ probe: true, t: Date.now(), ...o }) }).catch(() => {}); };
```
Checks:
1. **On load:** `isSecureContext`, `navigator.userAgent`, `!!navigator.xr`, `!!navigator.mediaDevices?.getUserMedia`,
   `await navigator.xr?.isSessionSupported('immersive-ar')`.
2. **Button "1. Mic":** `getUserMedia({audio:true})` → log ok / `err.name: err.message`;
   `new AudioContext({sampleRate:16000})` → log `sampleRate`, `state` after `resume()`; measure 1 s mic RMS via
   `AnalyserNode` and log it. Keep the stream open.
3. **Button "2. Enter AR":** `navigator.xr.requestSession('immersive-ar', {optionalFeatures:['local-floor','hit-test']})`
   → log ok/error and `session.enabledFeatures` if available. Minimal render loop (three.js like the main page is
   fine) so the session stays alive. In session:
   - every 1 s, **only when changed**, log `inputSources.map(s=>({handedness:s.handedness, profiles:s.profiles,
     mode:s.targetRayMode, buttons:s.gamepad?.buttons.map(b=>b.pressed)}))`;
   - `selectstart`: log + try a **fresh** `getUserMedia({audio:true})` (is `select` a user gesture?);
   - `selectend`: play 0.2 s 440 Hz `OscillatorNode` beep on the step-2 context, log `ctx.state`;
   - every 2 s log the step-2 stream RMS (does the mic survive inside AR?).
4. **Button "3. Beep"** (outside AR).

### 2.3 Run it
```bash
cd server && MISTRAL_API_KEY=... uv run uvicorn main:app --host 0.0.0.0 --port 8000
tailscale serve --bg 8000            # older tailscale: `tailscale serve 8000`
```
iPhone → Zapbox Browser → `https://<hub>.<tailnet>.ts.net/probe` → tap 1, tap 2; in AR press trigger, grip, stick,
A/X, B/Y one at a time and talk; watch `[voice-log]` lines in the server terminal.

### 2.4 Record results in `voice/ZAPBOX_PROBE.md` and decide
| Result | Action |
|---|---|
| Mic ok outside AR **and** RMS > 0 inside AR | `browser` mic usable (mic opened before Enter AR) — proceed |
| Mic also works from `selectstart` | Same; still open the mic before AR (more robust) |
| Mic fails, or RMS = 0 in AR | `browser` is **audio-only**: set default `VOICE_MIC=web`, still build everything |
| `immersive-ar` unsupported | Stop, report to user |
| Beep inaudible in AR | Report; try `audioCtx.resume()` every turn; the text card still works |

Also record **gamepad button index ↔ physical button**. Expected `xr-standard`: 0 trigger, 1 grip, 3 stick press,
4 A/X, 5 B/Y — **confirm**. If there is no `gamepad` at all, PTT must use `?ptt=trigger` (select events).

---

## 3. Phase 2 — Server (`server/main.py`)

### 3.1 Imports, constants, state
```python
from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
```
```python
MISTRAL_API_BASE = os.environ.get("MISTRAL_API_BASE", "https://api.mistral.ai/v1")
VOICE_CHAT_MODEL = os.environ.get("VOICE_CHAT_MODEL", MISTRAL_MODEL)
VOICE_MODELS = {"voxtral-mini-transcribe-realtime-3", "voxtral-mini-tts-3-0"}
VOICE_ROLES = ["browser", "web"]
SPEAKER_OPTIONS = {"mic", "all", *VOICE_ROLES}


def _env_choice(name: str, default: str, allowed) -> str:
    v = os.environ.get(name, default)
    return v if v in allowed else default
```
Next to `team_summary`:
```python
voice_turn: dict = {}
voice_config = {"mic": _env_choice("VOICE_MIC", "browser", VOICE_ROLES),
                "speaker": _env_choice("VOICE_SPEAKER", "browser", SPEAKER_OPTIONS), "rev": 0}
```

### 3.2 Token
```python
@app.post("/api/voice/token")
async def api_voice_token(payload: dict) -> JSONResponse:
    model = payload.get("model", "")
    if not MISTRAL_API_KEY:
        return JSONResponse({"error": "MISTRAL_API_KEY not set"}, status_code=503)
    if model not in VOICE_MODELS:
        return JSONResponse({"error": f"model not allowed: {model}"}, status_code=400)
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.post(f"{MISTRAL_API_BASE}/client/sessions",
                              headers={"Authorization": f"Bearer {MISTRAL_API_KEY}"},
                              json={"purpose": "realtime", "model": model})
    if r.status_code >= 300:
        return JSONResponse({"error": r.text}, status_code=r.status_code)
    data = r.json()
    return JSONResponse({"token": data["client_secret"]["value"],
                         "expires_at": data.get("expires_at"), "model": model})
```

### 3.3 Grounding + chat (SSE)
```python
VOICE_SYSTEM = (
    "You are OnlyVibes, a voice assistant inside an AR view of the team's AI coding agents. "
    "Answer ONLY from the live data below; if it is not there, say you don't see it. "
    "Reply in one to three short spoken sentences: no markdown, lists, emoji or code; "
    "say file names naturally. Mention stuck agents and collisions first when asked how things are going.\n\n"
    "LIVE DATA:\n"
)


def voice_context() -> str:
    snap = snapshot()
    lines = [f"Team headline: {snap['team_summary']}"] + [f"ALERT: {a['text']}" for a in snap["alerts"]]
    for st in snap["laptops"]:
        agents = st.get("agents") or []
        if not agents:
            lines.append(f"{st.get('owner')} ({st.get('laptop')}): no agents running")
        for a in agents:
            proj = (a.get("cwd") or "").rstrip("/").rsplit("/", 1)[-1] or "?"
            lines.append(f"{st.get('owner')} ({st.get('laptop')}): {a.get('name')} is {a.get('state')}, "
                         f"{a.get('cpu')}% cpu, project {proj} - {a.get('summary') or a.get('last_log') or ''}")
    return "\n".join(lines)[:4000]


@app.post("/api/voice/chat")
async def api_voice_chat(req: Request):
    if not MISTRAL_API_KEY:
        return JSONResponse({"error": "MISTRAL_API_KEY not set"}, status_code=503)
    body = await req.json()
    history = [m for m in body.get("messages", []) if m.get("role") in ("user", "assistant")][-12:]
    messages = [{"role": "system", "content": VOICE_SYSTEM + voice_context()}] + history

    async def gen():
        async with httpx.AsyncClient(timeout=120) as client:
            async with client.stream("POST", f"{MISTRAL_API_BASE}/chat/completions",
                                     headers={"Authorization": f"Bearer {MISTRAL_API_KEY}"},
                                     json={"model": VOICE_CHAT_MODEL, "messages": messages,
                                           "stream": True, "temperature": 0.3}) as r:
                r.raise_for_status()
                async for line in r.aiter_lines():
                    if line.startswith("data:"):
                        yield f"{line}\n\n"

    return StreamingResponse(gen(), media_type="text/event-stream")
```

### 3.4 Turn, config, health, static
```python
@app.post("/api/voice/turn")
async def api_voice_turn(payload: dict) -> dict:
    global voice_turn
    voice_turn = {"id": int(time.time() * 1000),
                  "heard": str(payload.get("heard", ""))[:500],
                  "reply": str(payload.get("reply", ""))[:1000],
                  "source_role": payload.get("source_role") if payload.get("source_role") in VOICE_ROLES else "web",
                  "client_id": str(payload.get("client_id", ""))[:64],
                  "speak_on": [r for r in payload.get("speak_on", []) if r in VOICE_ROLES],
                  "ts": time.time()}
    return {"ok": True, "id": voice_turn["id"]}


@app.get("/api/voice/config")
async def api_voice_config_get() -> dict:
    return voice_config


@app.post("/api/voice/config")
async def api_voice_config_set(payload: dict) -> JSONResponse:
    mic, speaker = payload.get("mic"), payload.get("speaker")
    if payload.get("toggle") == "mic":
        mic = "web" if voice_config["mic"] == "browser" else "browser"
    if mic is not None and mic not in VOICE_ROLES:
        return JSONResponse({"error": f"mic must be one of {VOICE_ROLES}"}, status_code=400)
    if speaker is not None and speaker not in SPEAKER_OPTIONS:
        return JSONResponse({"error": f"speaker must be one of {sorted(SPEAKER_OPTIONS)}"}, status_code=400)
    if mic: voice_config["mic"] = mic
    if speaker: voice_config["speaker"] = speaker
    voice_config["rev"] += 1
    print(f"[voice] config -> {voice_config}")
    return JSONResponse(voice_config)


@app.get("/api/voice/health")
async def api_voice_health() -> dict:
    return {"ok": True, "voice": bool(MISTRAL_API_KEY), "config": voice_config}


@app.get("/voice.js")
async def voice_js():
    return FileResponse(WEB_DIR / "voice.js", media_type="application/javascript")


@app.get("/mic")
async def mic_page():
    return FileResponse(WEB_DIR / "mic.html")
```
In `snapshot()` add `"voice": voice_turn, "voice_config": voice_config,` to the returned dict.
Update the module docstring endpoint list and env vars (`VOICE_MIC`, `VOICE_SPEAKER`, `VOICE_CHAT_MODEL`,
`MISTRAL_API_BASE`).

### 3.5 Verify
```bash
cd server && MISTRAL_API_KEY=... uv run uvicorn main:app --port 8000
cd reporter && SERVER_URL=ws://localhost:8000/ws uv run mock_reporter.py          # 2nd terminal
curl -s localhost:8000/api/voice/health
curl -s -XPOST localhost:8000/api/voice/token -H 'content-type: application/json' -d '{"model":"voxtral-mini-tts-3-0"}' | head -c 60   # {"token":"rt_...
curl -s -XPOST localhost:8000/api/voice/token -H 'content-type: application/json' -d '{"model":"x"}'           # 400
curl -N -XPOST localhost:8000/api/voice/chat -H 'content-type: application/json' \
  -d '{"messages":[{"role":"user","content":"who is stuck and what is kay doing?"}]}'                          # SSE, mentions mock owners
curl -s -XPOST localhost:8000/api/voice/config -H 'content-type: application/json' -d '{"toggle":"mic"}'      # browser -> web
curl -s -XPOST localhost:8000/api/voice/config -H 'content-type: application/json' -d '{"mic":"laptop"}'      # 400
curl -s localhost:8000/api/state | python3 -c 'import sys,json;d=json.load(sys.stdin);print(d["voice_config"],d["voice"])'
```

---

## 4. Phase 3 — Browser voice module `web/voice.js` (new)

**Classic script** (not a module) so `<script src="voice.js">` never breaks `file://` mode; no three.js
dependency; exposes `window.OnlyVibesVoice`.

### 4.1 Port from `voice/voice/webapp/static/index.html`
Copy: constants; `mintToken` (URL → `/api/voice/token`); `attach`, `waitEvent`, `send`; `startMic` + `Pcm`
worklet; `PLAYER_JS`, `initAudioOut`, `decodePcm`, `playPcm`, `resetPlayback`; `openStt`, `openTts`,
`onPlaybackDrained`; `runReply` (URL → `/api/voice/chat`, **remove the client system message**, send
`history.slice(-12)`); `endTurn`, `interrupt`, `beginTurn`, `stopListening`.
**Delete** the auto-start IIFE and all DOM listeners at the bottom.

### 4.2 Replace DOM writes with `emit`
```js
let opts = {}, config = { mic: 'browser', speaker: 'browser', rev: -1 }, micReady = false;
const ui = { state: 'off', heard: '', reply: '', status: '', timings: '' };
// state: off | ready | listening | thinking | speaking | error
function emit(p) { Object.assign(ui, p); opts.onUpdate?.({ ...ui, config, micReady }); }
const log = (o) => fetch('/api/voice/log', { method: 'POST', headers: {'Content-Type':'application/json'},
  body: JSON.stringify({ t: Date.now(), role: opts.role, ...o }) }).catch(() => {});
```
Mapping: transcript delta → `emit({heard: ui.heard + msg.text})`; reply delta → `emit({reply: full})`;
`status(x)` → `emit({status:x})`; `beginTurn` → `listening`; `stopListening` → `thinking`; first TTS audio →
`speaking`; `endTurn` → `ready`. Call `log()` in every `catch` and on STT/TTS `error` messages.

### 4.3 `enable()` — call from a real DOM tap, before entering AR
```js
async function enable() {
  if (ui.state !== 'off' && ui.state !== 'error') return true;
  try { await initAudioOut(); await audioCtx.resume(); }
  catch (e) { emit({ state: 'error', status: 'audio: ' + e.message }); log({ ev: 'audio-fail', err: String(e) }); return false; }
  try { await startMic(); await micCtx.resume(); micReady = true; }
  catch (e) { micReady = false; log({ ev: 'mic-fail', err: String(e) }); }
  emit({ state: 'ready', status: micReady ? 'voice ready' : 'audio only (no mic)' });
  log({ ev: 'enabled', micReady, micRate: micCtx?.sampleRate, outRate: audioCtx.sampleRate });
  return true;
}
```
- The mic stream stays open for the whole session; the worklet already sends only while `holding && turnActive`,
  so PTT inside AR needs no new permission prompt.
- If the mic is unavailable, the page still plays replies (audio-only), so `mic=web` + `speaker=browser` works.
- **Sample-rate fallback:** if `new AudioContext({sampleRate:16000})` throws or reports another rate, create it at
  the default rate and downsample in the `Pcm` worklet (`processorOptions:{ratio: sampleRate/16000}`; average each
  `ratio`-sized window). Log which path ran.
- In `beginTurn()` also call `audioCtx?.resume(); micCtx?.resume();` (iOS may suspend on AR start).

### 4.4 Gate + speaker routing
Top of `beginTurn()`:
```js
if (speaking) interrupt();
if (config.mic !== opts.role) { opts.onBlocked?.(`mic is set to ${config.mic}`); return; }
if (!micReady) { opts.onBlocked?.('no mic on this device'); return; }
```
In `runReply` compute:
```js
const speakOn = config.speaker === 'mic' ? [config.mic]
              : config.speaker === 'all' ? ['browser', 'web'] : [config.speaker];
const speakHere = speakOn.includes(opts.role);
```
If `!speakHere`: skip `openTts` and all TTS `send`s, show text only, `endTurn()` when the SSE stream ends.
After the reply (both paths):
```js
fetch('/api/voice/turn', { method: 'POST', headers: {'Content-Type':'application/json'},
  body: JSON.stringify({ heard: userText, reply: full, source_role: opts.role,
                         client_id: opts.clientId, speak_on: speakOn }) }).catch(() => {});
```

### 4.5 `speak(text)` — TTS only (for turns from the other role)
```js
async function speak(text) {
  if (!text || ui.state === 'off') return;
  if (speaking) interrupt();
  speaking = true; firstAudio = 0; t0 = performance.now();
  emit({ state: 'speaking', reply: text });
  try { await openTts(); resetPlayback(); genPending = true;
        send(ttsWs, { type: 'input_text.append', text: text.replace(/[*_#`~>|]/g, '') });
        send(ttsWs, { type: 'input_text.flush' }); }
  catch (e) { log({ ev: 'speak-fail', err: String(e) }); endTurn(); }
}
```

### 4.6 Export
```js
window.OnlyVibesVoice = {
  init(o) { opts = { ...o }; },
  setConfig(c) { if (c && c.rev !== config.rev) { config = c; emit({}); } },
  enable, begin: beginTurn, end: stopListening, interrupt, speak, log,
  get state() { return ui.state; }, get micReady() { return micReady; },
};
```

---

## 5. Phase 4 — AR page `web/index.html`

### 5.1 Load + params
- Add `<script src="voice.js"></script>` **before** the `<script type="module">`.
- In the module:
  ```js
  const params = new URLSearchParams(location.search);
  const ROLE = params.get('role') || 'web';
  const CLIENT_ID = crypto.randomUUID?.() || String(Math.random()).slice(2);
  const PTT = params.get('ptt') || '4';            // gamepad index, or 'trigger'
  const SWITCH = +(params.get('switch') || 5);     // long-press to toggle mic source
  ```
  Use the indices confirmed in Phase 1.
- Keep the existing `ARButton` as-is. Do **not** add `dom-overlay` (no DOM in stereo; an unsupported feature must
  never become required).

### 5.2 Voice bar (DOM — usable on the iPhone before entering AR, and on web)
```html
<div id="voicebar">
  <button id="voiceEnable" hidden>🎙 Enable voice</button>
  <button id="voiceTalk" hidden>Hold to talk</button>
  <button id="micToggle" hidden>mic: browser</button>
  <select id="speakerSel" hidden><option>browser</option><option>web</option><option>mic</option><option>all</option></select>
</div>
```
CSS: `#voicebar{position:fixed;right:14px;bottom:24px;z-index:21;display:flex;gap:8px}` + plain button styles.
The existing `#app button, body > button {… !important}` rule only hits direct `body` children — keep these inside
`#voicebar`. On narrow phones make sure the bar does not cover the centered Enter AR button (e.g. place the bar
above it with `bottom:80px` when `innerWidth < 600`).

Wiring:
```js
const voice = window.OnlyVibesVoice;
const $ = (id) => document.getElementById(id);
let lastCfg = {};
function renderConfig(c) {
  lastCfg = c;
  $('micToggle').textContent = `mic: ${c.mic}${c.mic === ROLE ? ' (this)' : ''}`;
  $('speakerSel').value = c.speaker;
}
const postConfig = (body) => fetch('/api/voice/config', { method: 'POST',
  headers: {'Content-Type':'application/json'}, body: JSON.stringify(body) })
  .then(r => r.json()).then(c => { voice.setConfig(c); renderConfig(c); return c; });
fetch('/api/voice/health').then(r => r.json()).then(h => {
  if (!h.voice || !voice) return;
  voice.setConfig(h.config); renderConfig(h.config);
  $('voiceEnable').hidden = $('micToggle').hidden = $('speakerSel').hidden = false;
}).catch(() => {});
$('voiceEnable').onclick = async () => { if (await voice.enable()) { $('voiceEnable').hidden = true; $('voiceTalk').hidden = false; } };
$('voiceTalk').addEventListener('pointerdown', (e) => { e.preventDefault(); $('voiceTalk').setPointerCapture(e.pointerId); voice.begin(); });
$('voiceTalk').addEventListener('pointerup', () => voice.end());
$('voiceTalk').addEventListener('pointercancel', () => voice.end());
addEventListener('keydown', (e) => { if (e.code === 'Space' && !e.repeat && voice && voice.state !== 'off') { e.preventDefault(); voice.begin(); } });
addEventListener('keyup', (e) => { if (e.code === 'Space') voice?.end(); });
$('micToggle').onclick = () => postConfig({ toggle: 'mic' });
$('speakerSel').onchange = (e) => postConfig({ speaker: e.target.value });
```
**User flow on Zapbox:** open bookmark → tap **Enable voice** (allow mic) → tap **Enter AR** → put the phone in the
headset.

### 5.3 Head-locked voice card
```js
const voiceCard = makeCard({ fontPx: 22, bg: 'rgba(20,16,40,0.92)', fg: '#ece6ff' });
voiceCard.visible = false;
scene.add(voiceCard);                                   // scene, not world (world is scaled 0.15 in AR)
const VOICE_BG = { ready: 'rgba(20,16,40,0.92)', listening: 'rgba(120,20,40,0.92)',
  thinking: 'rgba(60,50,10,0.92)', speaking: 'rgba(10,60,40,0.92)', error: 'rgba(90,0,0,0.92)', off: 'rgba(20,16,40,0.92)' };
let voiceHide = null, lastUi = null, lastVoiceText = '';
function setVoiceText(t, state = 'ready', holdMs = 8000) {
  lastVoiceText = t;
  voiceCard.userData.bg = VOICE_BG[state] || VOICE_BG.ready;
  setCardText(voiceCard, t);
  if (renderer.xr.isPresenting) voiceCard.scale.multiplyScalar(0.25);   // tune in headset
  voiceCard.visible = true;
  clearTimeout(voiceHide);
  if (holdMs) voiceHide = setTimeout(() => { voiceCard.visible = false; }, holdMs);
}
const cfgLine = (c) => `[mic: ${c.mic ?? '?'} · speaker: ${c.speaker ?? '?'} · this: ${ROLE}]`;
voice?.init({ role: ROLE, clientId: CLIENT_ID,
  onUpdate: (u) => {
    lastUi = u;
    const head = { listening: 'listening…', thinking: 'thinking…', speaking: 'speaking', error: 'error: ' + u.status }[u.state] || '';
    if (u.heard || u.reply || u.state === 'listening' || u.state === 'error')
      setVoiceText([cfgLine(u.config), head, u.heard && `you: ${u.heard}`, u.reply && `→ ${u.reply}`].filter(Boolean).join('\n'),
                   u.state, u.state === 'ready' ? 8000 : 0);
    $('voiceTalk').textContent = u.state === 'listening' ? 'Listening…' : u.state === 'speaking' ? 'Interrupt' : 'Hold to talk';
  },
  onBlocked: (why) => setVoiceText(`${cfgLine(lastCfg)}\n${why} — ${ROLE === 'browser' ? 'hold B/Y 1s' : 'use the mic toggle'} to switch`, 'error', 4000),
});
```
If emoji/unicode renders as boxes in the headset, use plain ASCII.
Animation loop, after `cam.getWorldQuaternion(_camQ)`:
```js
if (voiceCard.visible) {
  cam.getWorldPosition(_camP);
  _fwd.set(0, -0.25, -1).normalize().applyQuaternion(_camQ);
  voiceCard.position.copy(_camP).addScaledVector(_fwd, renderer.xr.isPresenting ? 0.7 : 2.2);
  voiceCard.quaternion.copy(_camQ);
}
```
Declare `const _camP = new THREE.Vector3(), _fwd = new THREE.Vector3();` beside `_camQ`. In the existing
`sessionstart`/`sessionend` handlers, if `voiceCard.visible`, call `setVoiceText(lastVoiceText)` to rescale.

### 5.4 Controller PTT + mic toggle (in AR)
Add to the **existing** `sessionstart` handler (don't register a second one):
```js
const s = renderer.xr.getSession();
voice?.log({ ev: 'xr-start', voice: voice?.state, micReady: voice?.micReady });
if (PTT === 'trigger') { s.addEventListener('selectstart', () => voice?.begin()); s.addEventListener('selectend', () => voice?.end()); }
```
In the animation loop:
```js
const xs = renderer.xr.getSession();
if (xs && voice && voice.state !== 'off') {
  const pressed = (i) => [...xs.inputSources].some(src => src.gamepad?.buttons[i]?.pressed);
  if (PTT !== 'trigger') {
    const d = pressed(+PTT);
    if (d && !pttDown) voice.begin();
    if (!d && pttDown) voice.end();
    pttDown = d;
  }
  const sw = pressed(SWITCH);
  if (sw && !switchSince) switchSince = performance.now();
  if (!sw) { switchSince = 0; switchFired = false; }
  if (sw && !switchFired && performance.now() - switchSince > 1000) {
    switchFired = true;
    postConfig({ toggle: 'mic' }).then((c) => setVoiceText(`mic → ${c.mic}`, 'ready', 2500));
  }
}
```
Declare `let pttDown = false, switchSince = 0, switchFired = false;`. If the probe shows **no gamepad**, use
`?ptt=trigger` and document that switching then needs the web toggle.

### 5.5 `/view` handling
Refactor `ws.onmessage` to parse once (keep the `try/catch`):
```js
const snap = JSON.parse(ev.data);
applyState(snap);
if (snap.voice_config) { voice?.setConfig(snap.voice_config); renderConfig(snap.voice_config); }
const vt = snap.voice;
if (vt?.id && vt.id !== lastVoiceId) {
  const first = lastVoiceId === null; lastVoiceId = vt.id;
  if (!first && vt.client_id !== CLIENT_ID) {
    setVoiceText(`${cfgLine(snap.voice_config || {})}\n${vt.source_role}: ${vt.heard}\n→ ${vt.reply}`);
    if (vt.source_role !== ROLE && (vt.speak_on || []).includes(ROLE)) voice?.speak(vt.reply);
  }
}
```
Declare `let lastVoiceId = null;` (`first` avoids replaying an old turn on page load).
`renderConfig` must tolerate being called before the health response (elements exist, just hidden).

### 5.6 Must not regress
Fake-data mode (no server): health fetch fails → all voice UI hidden. Desktop 3D view and AR placement unchanged.

---

## 6. Phase 5 — Fallback mic page `web/mic.html` (new)

Phone-friendly single page for `web` mode on a bare phone: big **Hold to talk** button, `micToggle` +
`speakerSel`, heard/reply text, status line. Loads `voice.js`; `init({role:'web', clientId, onUpdate, onBlocked})`;
first tap calls `enable()`. Opens `WS /view` (same URL logic as `index.html`: `wss://` on https) for
`voice_config` and remote turns (same rule as §5.5, including `speak()`).
Typical use when Zapbox Browser blocks the mic: `mic=web`, `speaker=browser` → talk into this phone, hear the
reply in the headset.

---

## 7. Phase 6 — Tests

Local (no Zapbox):
1. `http://localhost:8000/` (role `web`), set `mic=web`, `speaker=web`, Enable voice, hold Space, ask "who is
   working right now?" → transcript, grounded reply naming mock owners, audio, no server errors.
2. `/mic` in another tab: same.
3. `mic=browser` while on the web page: Space shows "mic is set to browser"; server log shows **no** token request.

Zapbox Browser (`https://<hub>.<tailnet>.ts.net/?role=browser`):
4. `mic=browser`, `speaker=browser`: Enable voice → Enter AR → hold A/X → red "listening" card → yellow → green +
   audio from the iPhone.
5. Interrupt: PTT while speaking stops audio and starts a new turn.
6. Echo: the reply is not transcribed as the next question (mic suspended while speaking).
7. Hold B/Y ≥ 1 s → server prints config change, card shows `mic → web`; short press does nothing; A/X now shows
   the "mic is set to web" hint.
8. `mic=web`, `speaker=browser`: talk on `/mic` (second phone) → reply spoken in the headset, card shows it.
9. `speaker=all`: each turn spoken once on each role; no double playback on the producer.
10. Mic denied in Zapbox Browser → "audio only (no mic)"; test 8 still works.

Failure paths:
11. Server without `MISTRAL_API_KEY` → no voice UI; AR still works.
12. Kill server mid-turn → error card; next turn works after restart.
13. Report time-to-first-audio (`timings`) for tests 4 and 8; target ≲ 1.5 s after release.

---

## 8. Docs
- `README.md`: section "Voice in Zapbox Browser": bookmark `/?role=browser`, Enable voice → Enter AR → hold A/X,
  B/Y long-press toggles mic browser⇄web, `/mic` fallback, params `?ptt=` `?switch=`, env `VOICE_MIC`,
  `VOICE_SPEAKER`, `VOICE_CHAT_MODEL`.
- `server/main.py` docstring: new endpoints + env vars.
- `voice/ZAPBOX_PROBE.md`: probe results (iOS version, Zapbox Browser version, user agent, mic/audio behaviour,
  button indices, hit-test support).

## 9. Files touched
| File | Change |
|---|---|
| `server/main.py` | `/api/voice/{token,chat,turn,config,health,log}`, `/voice.js`, `/mic`, `/probe`, `voice` + `voice_config` in `snapshot()`, env vars |
| `web/voice.js` | new — ported Voxtral client, callbacks, role/config gate, audio-only fallback, speaker routing, `speak()` |
| `web/index.html` | load `voice.js`, params, voice bar, head-locked card, controller PTT + B/Y toggle, `/view` turn handling |
| `web/probe.html` | new — capability probe |
| `web/mic.html` | new — fallback mic page |
| `README.md`, `voice/ZAPBOX_PROBE.md` | docs |

## 10. Risks
- Zapbox Browser may block the mic → `browser` becomes audio-only; `web` mic + `speaker=browser` covers it.
- iOS suspends AudioContexts on AR start/interruptions → resume both every turn; log `state` when silent.
- 16 kHz AudioContext not honoured on some iOS → downsampling fallback (§4.3).
- Per-turn STT token mint adds ~100–300 ms → optional: prefetch the next token after each turn (use if < 50 s old).
- Card size/distance in stereo needs tuning in the headset (0.25 scale, 0.7 m).

## 11. Acceptance criteria
- In Zapbox Browser AR: hold A/X, ask a question, hear a grounded answer from the iPhone, see the card (if the
  probe allows the mic).
- Mic source switchable `browser` ⇄ `web` from the web toggle, B/Y long-press, or curl; non-active clients refuse
  PTT with a hint; replies spoken per `speaker`.
- With the Zapbox mic unavailable, `/mic` on another phone drives the conversation and the headset speaks.
- No API key in any browser payload; standalone fake-data page still works; no new dependencies.
