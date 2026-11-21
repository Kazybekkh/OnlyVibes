// OnlyVibes voice: push-to-talk speech-to-text only (Voxtral realtime STT). No TTS, no chat.
// The page decides what to do with the transcript (web/ar.html: interrupt the selected agent and
// send it as the new prompt; web/mic.html: same, target from a dropdown).
// Classic script (not a module). Exposes window.OnlyVibesVoice.
// The Mistral API key never reaches the browser: /api/voice/token mints a short-lived rt_* token
// (needs the control key, ?key= in the page URL, when the hub sets OV_CONTROL_SECRET).
(function () {
"use strict";

const STT_MODEL = "voxtral-mini-transcribe-realtime-3";
const MIC_RATE = 16000;   // mic worklet emits int16 PCM at 16 kHz
const WS_BASE = "wss://api.mistral.ai/v1";
const MAX_TURN_MS = 30000;

let opts = {};
let micReady = false;
const ui = { state: "off", heard: "", status: "" };
// state: off | ready | listening | thinking | error

let micCtx = null, micStream = null, workletNode = null, sttWs = null;
let holding = false, turnActive = false, endTimer = null, doneFallback = null;

const OV_KEY = new URLSearchParams(location.search).get("key") || "";
const H = () => (OV_KEY ? { "Content-Type": "application/json", "X-OV-Key": OV_KEY }
                        : { "Content-Type": "application/json" });

function emit(p) { Object.assign(ui, p); opts.onUpdate?.({ ...ui, micReady }); }
const log = (o) => fetch("/api/voice/log", { method: "POST", headers: H(),
  body: JSON.stringify({ t: Date.now(), role: opts.role, ...o }) }).catch(() => {});

async function mintToken() {
  const r = await fetch("/api/voice/token", { method: "POST", headers: H(), body: JSON.stringify({ model: STT_MODEL }) });
  if (r.status === 403) throw new Error("locked: open this page with ?key=<control key>");
  if (!r.ok) throw new Error("token mint failed (" + r.status + ")");
  return (await r.json()).token;
}

// ---------- websocket helper: dispatch + waiters ----------
function attach(ws, onEvent) {
  ws._waiters = [];
  ws.onmessage = (ev) => {
    const msg = JSON.parse(ev.data);
    ws._waiters = ws._waiters.filter(w => !w(msg));
    onEvent(msg);
  };
}
function waitEvent(ws, type) {
  return new Promise((res) => {
    ws._waiters.push((msg) => { if (msg.type === type) { res(msg); return true; } return false; });
  });
}
const send = (ws, obj) => { if (ws && ws.readyState === WebSocket.OPEN) ws.send(JSON.stringify(obj)); };

// ---------- mic ----------
async function startMic() {
  micStream = await navigator.mediaDevices.getUserMedia({ audio: {
    channelCount: 1, sampleRate: MIC_RATE,
    echoCancellation: true, noiseSuppression: true, autoGainControl: true,
    googEchoCancellation: true, googNoiseSuppression: true, googAutoGainControl: true,
    googHighpassFilter: true, googTypingNoiseDetection: true } });
  // sample-rate fallback: if a 16 kHz AudioContext is not honoured, run at the
  // default rate and downsample inside the worklet via processorOptions.ratio
  let ratio = 1;
  try {
    micCtx = new AudioContext({ sampleRate: MIC_RATE });
    if (micCtx.sampleRate !== MIC_RATE) { micCtx.close(); micCtx = null; throw new Error("16k not honoured"); }
    log({ ev: "mic-path", path: "native16k", ctxRate: micCtx.sampleRate });
  } catch (e) {
    micCtx = new AudioContext();
    ratio = micCtx.sampleRate / MIC_RATE;
    log({ ev: "mic-path", path: "downsample", ctxRate: micCtx.sampleRate, ratio });
  }
  const src = micCtx.createMediaStreamSource(micStream);
  const workletUrl = URL.createObjectURL(new Blob([`
    // optional downsample (ratio = ctxRate/16k) + high-pass (~100Hz one-pole)
    // + noise gate: sends silence when no speech, so hiss/room noise never
    // reaches the STT. Emits int16 PCM at 16 kHz.
    class Pcm extends AudioWorkletProcessor {
      constructor(options) {
        super();
        const p = options.processorOptions || {};
        this.ratio = p.ratio || 1;
        this.acc = 0; this.sum = 0; this.cnt = 0;
        this.y = 0; this.x1 = 0; this.gate = 0;
      }
      process(inputs) {
        const ch = inputs[0][0];
        if (!ch) return true;
        const ds = new Float32Array(Math.ceil(ch.length / this.ratio));
        let n = 0, sum = 0;
        const a = 0.985;                    // ~100Hz high-pass at 16kHz
        for (let i = 0; i < ch.length; i++) {
          const x0 = ch[i];
          sum += x0 * x0;
          this.sum += x0; this.cnt++; this.acc += 1;
          if (this.acc >= this.ratio) {
            const x = this.sum / this.cnt;
            this.acc -= this.ratio; this.sum = 0; this.cnt = 0;
            this.y = a * (this.y + x - this.x1); this.x1 = x;
            ds[n++] = this.y;
          }
        }
        const rms = Math.sqrt(sum / ch.length);
        // smooth gate: open above 0.012, close below 0.006 (hysteresis)
        this.gate = rms > 0.012 ? Math.min(1, this.gate + 0.2)
                                : (rms < 0.006 ? Math.max(0, this.gate - 0.2) : this.gate);
        const g = this.gate < 0.5 ? 0 : this.gate;   // hard gate when closed
        const buf = new Int16Array(n);
        for (let i = 0; i < n; i++)
          buf[i] = Math.max(-32768, Math.min(32767, ds[i] * g * 32768));
        this.port.postMessage(buf.buffer, [buf.buffer]);
        return true;
      }
    }
    registerProcessor("pcm", Pcm);`], { type: "application/javascript" }));
  await micCtx.audioWorklet.addModule(workletUrl);
  workletNode = new AudioWorkletNode(micCtx, "pcm", { processorOptions: { ratio } });
  workletNode.port.onmessage = (e) => {
    if (holding && turnActive) send(sttWs, { type: "input_audio.append",
      audio: btoa(String.fromCharCode(...new Uint8Array(e.data))) });
  };
  src.connect(workletNode).connect(micCtx.destination);
}

// ---------- STT: one fresh session per push-to-talk turn ----------
function finish(text) {
  endTurn();
  if (text) opts.onTranscript?.(text);
}
async function openStt() {
  if (sttWs) { try { sttWs.close(); } catch {} sttWs = null; }
  const token = await mintToken();
  sttWs = new WebSocket(`${WS_BASE}/audio/transcriptions/realtime?model=${STT_MODEL}`, ["realtime", token]);
  const opened = new Promise((res, rej) => { sttWs.onopen = res; sttWs.onerror = rej; });
  attach(sttWs, (msg) => {
    if (msg.type === "transcription.text.delta") {
      emit({ heard: ui.heard + msg.text });
    } else if (msg.type === "transcription.done") {
      const text = (msg.text || ui.heard).trim();
      try { sttWs.close(); } catch {}
      sttWs = null;
      if (doneFallback) { clearTimeout(doneFallback); doneFallback = null; }
      finish(text);
    } else if (msg.type === "error") {
      log({ ev: "stt-error", error: msg.error });
      endTurn();
      emit({ state: "error", status: "STT error: " + JSON.stringify(msg.error) });
    }
  });
  await opened;
  sttWs.onerror = () => log({ ev: "stt-ws-error" });
  await waitEvent(sttWs, "session.created");
}

// ---------- turn control ----------
function endTurn() {
  holding = false; turnActive = false;
  if (endTimer) { clearTimeout(endTimer); endTimer = null; }
  emit({ state: "ready", status: "ready: hold to talk" });
}
async function beginTurn() {
  if (turnActive) return;
  if (!micReady) { opts.onBlocked?.("no mic on this device"); return; }
  if (micCtx && micCtx.state === "suspended") micCtx.resume();  // iOS may suspend it when AR starts
  turnActive = true; holding = true;
  emit({ state: "listening", heard: "", status: "listening…" });
  try { await openStt(); }
  catch (e) {
    log({ ev: "stt-connect-fail", err: String(e) });
    endTurn();
    emit({ state: "error", status: e.message });
    return;
  }
  endTimer = setTimeout(stopListening, MAX_TURN_MS);  // never listen longer than this
}
function stopListening() {
  if (!turnActive || !holding) return;
  holding = false;  // stop streaming mic chunks
  send(sttWs, { type: "input_audio.end" });
  emit({ state: "thinking", status: "transcribing…" });
  if (doneFallback) clearTimeout(doneFallback);
  doneFallback = setTimeout(() => finish(ui.heard.trim()), 5000);  // transcription.done never came
}

// ---------- enable (call from a real user tap, before entering AR) ----------
async function enable() {
  if (micReady) return true;
  try { await startMic(); await micCtx.resume(); micReady = true; }
  catch (e) { micReady = false; log({ ev: "mic-fail", err: String(e) }); }
  emit({ state: micReady ? "ready" : "error", status: micReady ? "voice ready" : "no mic on this device" });
  log({ ev: "enabled", micReady, micRate: micCtx?.sampleRate });
  return micReady;
}

window.OnlyVibesVoice = {
  init(o) { opts = { ...o }; },
  enable, begin: beginTurn, end: stopListening, log,
  get state() { return ui.state; },
  get micReady() { return micReady; },
};
})();
