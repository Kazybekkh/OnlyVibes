// OnlyVibes voice client — Voxtral realtime STT/TTS for the Zapbox Browser (AR) page.
// Classic script (not a module) so <script src="voice.js"> also works from file://.
// No three.js dependency. Exposes window.OnlyVibesVoice; the page wires it up.
// The Mistral API key never reaches the browser: /api/voice/token mints rt_* tokens.
(function () {
"use strict";

const STT_MODEL = "voxtral-mini-transcribe-realtime-3";
const TTS_MODEL = "voxtral-mini-tts-3-0";
const TTS_RATE = 24000;   // Voxtral pcm = float32 LE, 24 kHz mono (NOT int16)
const MIC_RATE = 16000;   // mic worklet emits int16 PCM at 16 kHz
const WS_BASE = "wss://api.mistral.ai/v1";
const MAX_TURN_MS = 30000;
const AGG_MS = 250;       // aggregate decoded TTS chunks to ~250 ms before posting to the player

let opts = {};
let config = { mic: "browser", speaker: "browser", rev: -1 };
let micReady = false;
const ui = { state: "off", heard: "", reply: "", status: "", timings: "" };
// state: off | ready | listening | thinking | speaking | error

let micCtx = null, micStream = null, workletNode = null;
let sttWs = null, ttsWs = null;
let audioCtx = null, playerNode = null, pcmCarry = new Uint8Array(0), aggBuf = new Float32Array(0);
let holding = false, turnActive = false, t0 = 0, firstAudio = 0;
let endTimer = null, doneFallback = null, chatAbort = null;
let speaking = false, genPending = false, dropGenerations = 0;
const history = [];

// ---------- UI emit + server log ----------
function emit(p) { Object.assign(ui, p); opts.onUpdate?.({ ...ui, config, micReady }); }
const log = (o) => fetch("/api/voice/log", { method: "POST",
  headers: { "Content-Type": "application/json" },
  body: JSON.stringify({ t: Date.now(), role: opts.role, ...o }) }).catch(() => {});

async function mintToken(model) {
  const r = await fetch("/api/voice/token", { method: "POST",
    headers: { "Content-Type": "application/json" }, body: JSON.stringify({ model }) });
  if (!r.ok) throw new Error("token mint failed: " + await r.text());
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

// ---------- audio out: gapless AudioWorklet player ----------
// The context runs at the TTS rate, so chunks are played sample-exact with
// no per-chunk scheduling (no seams/clicks). The player prebuffers ~120ms,
// rebuffers on underrun instead of stuttering, and ramps in/out so starts,
// stops and underruns never produce a click. Decoded chunks are aggregated
// to ~AGG_MS on the main thread before being posted (flush on end).
const PLAYER_JS = `
class Player extends AudioWorkletProcessor {
  constructor() {
    super();
    this.q = []; this.cur = null; this.pos = 0; this.buffered = 0;
    this.playing = false; this.ended = false; this.gain = 0; this.last = 0;
    this.prebuf = Math.round(sampleRate * 0.12);
    this.port.onmessage = ({ data: m }) => {
      if (m.type === "pcm") { this.q.push(m.data); this.buffered += m.data.length; }
      else if (m.type === "end") this.ended = true;
      else if (m.type === "reset") { this.q = []; this.cur = null; this.buffered = 0;
                                     this.playing = false; this.ended = false; }
    };
  }
  process(_, outputs) {
    const out = outputs[0][0];
    if (!this.playing && this.buffered > 0 && (this.buffered >= this.prebuf || this.ended))
      this.playing = true;
    for (let i = 0; i < out.length; i++) {
      if (this.playing && (!this.cur || this.pos >= this.cur.length)) {
        this.cur = this.q.shift() || null; this.pos = 0;
        if (!this.cur) this.playing = false;        // underrun -> rebuffer
      }
      if (this.playing) {
        this.gain = Math.min(1, this.gain + 1 / 96);   // ~4ms fade-in
        this.last = this.cur[this.pos++] * this.gain; this.buffered--;
      } else {
        this.gain = 0; this.last *= 0.98;              // decay to 0, no click
      }
      out[i] = this.last;
    }
    if (this.ended && !this.playing && this.buffered <= 0) {
      this.ended = false; this.port.postMessage("drained");
    }
    return true;
  }
}
registerProcessor("player", Player);`;

async function initAudioOut() {
  audioCtx = new AudioContext({ sampleRate: TTS_RATE, latencyHint: "interactive" });
  await audioCtx.audioWorklet.addModule(
    URL.createObjectURL(new Blob([PLAYER_JS], { type: "application/javascript" })));
  playerNode = new AudioWorkletNode(audioCtx, "player", { outputChannelCount: [1] });
  playerNode.port.onmessage = (e) => { if (e.data === "drained") onPlaybackDrained(); };
  playerNode.connect(audioCtx.destination);
}
function decodePcm(b64) {
  // float32 LE; carry any partial sample over to the next chunk
  const bin = atob(b64);
  const bytes = new Uint8Array(pcmCarry.length + bin.length);
  bytes.set(pcmCarry);
  for (let i = 0; i < bin.length; i++) bytes[pcmCarry.length + i] = bin.charCodeAt(i);
  const n = bytes.length >> 2;
  pcmCarry = bytes.slice(n * 4);
  return new Float32Array(bytes.buffer, 0, n).slice();
}
function playPcm(b64) {
  if (!playerNode) return;
  const f = decodePcm(b64);
  if (!f.length) return;
  const merged = new Float32Array(aggBuf.length + f.length);
  merged.set(aggBuf); merged.set(f, aggBuf.length);
  aggBuf = merged;
  const target = Math.round(TTS_RATE * AGG_MS / 1000);
  while (aggBuf.length >= target) {
    const chunk = aggBuf.slice(0, target);
    aggBuf = aggBuf.slice(target);
    playerNode.port.postMessage({ type: "pcm", data: chunk }, [chunk.buffer]);
  }
}
function flushAgg() {
  if (playerNode && aggBuf.length) {
    const chunk = aggBuf;
    aggBuf = new Float32Array(0);
    playerNode.port.postMessage({ type: "pcm", data: chunk }, [chunk.buffer]);
  }
}
function resetPlayback() {
  pcmCarry = new Uint8Array(0);
  aggBuf = new Float32Array(0);
  if (playerNode) playerNode.port.postMessage({ type: "reset" });
}

// ---------- STT: one fresh session per turn (it ends at transcription.done) ----------
async function openStt() {
  if (sttWs) { try { sttWs.close(); } catch {} sttWs = null; }
  const token = await mintToken(STT_MODEL);
  sttWs = new WebSocket(`${WS_BASE}/audio/transcriptions/realtime?model=${STT_MODEL}`,
                        ["realtime", token]);
  const opened = new Promise((res, rej) => { sttWs.onopen = res; sttWs.onerror = rej; });
  attach(sttWs, (msg) => {
    if (msg.type === "transcription.text.delta") {
      emit({ heard: ui.heard + msg.text });
    } else if (msg.type === "transcription.done") {
      const text = (msg.text || ui.heard).trim();
      try { sttWs.close(); } catch {}
      sttWs = null;
      if (doneFallback) { clearTimeout(doneFallback); doneFallback = null; }
      if (text) runReply(text); else endTurn();
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

// ---------- TTS: one reusable session, several generations ----------
async function openTts() {
  if (ttsWs && ttsWs.readyState === WebSocket.OPEN) return;
  const token = await mintToken(TTS_MODEL);
  ttsWs = new WebSocket(`${WS_BASE}/audio/speech/realtime?model=${TTS_MODEL}`,
                        ["realtime", token]);
  const opened = new Promise((res, rej) => { ttsWs.onopen = res; ttsWs.onerror = rej; });
  dropGenerations = 0; genPending = false;
  attach(ttsWs, (msg) => {
    if (msg.type === "speech.audio.delta") {
      if (dropGenerations) return;                 // interrupted reply: discard
      if (!firstAudio) {
        firstAudio = performance.now();
        emit({ state: "speaking",
               timings: `first audio ${((firstAudio - t0) / 1000).toFixed(2)}s` });
      }
      try { playPcm(msg.audio_data); }
      catch (e) { emit({ status: "audio playback error: " + e.message }); log({ ev: "playback-fail", err: String(e) }); }
    } else if (msg.type === "speech.audio.done") {
      if (dropGenerations) { dropGenerations--; return; }
      genPending = false;
      pcmCarry = new Uint8Array(0);
      flushAgg();
      playerNode.port.postMessage({ type: "end" });   // -> "drained" -> endTurn
    } else if (msg.type === "error") {
      log({ ev: "tts-error", error: msg.error });
      endTurn();
      emit({ state: "error", status: "TTS error: " + JSON.stringify(msg.error) });
    }
  });
  ttsWs.onclose = () => { ttsWs = null; };
  await opened;
  ttsWs.onerror = () => log({ ev: "tts-ws-error" });
  await waitEvent(ttsWs, "session.created");
  const voice = opts.voice || "voice_592";
  send(ttsWs, { type: "session.update", session: {
    voice, response_format: "pcm", streaming_mode: opts.streamingMode || "responsive" } });
  await waitEvent(ttsWs, "session.updated");
}

function onPlaybackDrained() {
  if (!speaking) return;
  emit({ timings: (ui.timings ? ui.timings + " | " : "") +
        `total ${((performance.now() - t0) / 1000).toFixed(2)}s` });
  endTurn();
}

// ---------- chat (SSE via our server) -> straight into TTS ----------
async function runReply(userText) {
  history.push({ role: "user", content: userText });
  emit({ state: "thinking", reply: "", status: "thinking… (interrupt to cut in)" });
  speaking = true;
  if (micCtx) micCtx.suspend();   // don't let the mic hear our own reply
  const speakOn = config.speaker === "mic" ? [config.mic]
                : config.speaker === "all" ? ["browser", "web"] : [config.speaker];
  const speakHere = speakOn.includes(opts.role);
  let full = "";
  const ac = chatAbort = new AbortController();
  try {
    if (speakHere) { await openTts(); resetPlayback(); }
    const r = await fetch("/api/voice/chat", { method: "POST", signal: ac.signal,
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ messages: history.slice(-12) }) });   // no client-side system message
    if (!r.ok) throw new Error("chat failed: " + r.status);
    const reader = r.body.getReader();
    const dec = new TextDecoder();
    let buffer = "";
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      buffer += dec.decode(value, { stream: true });
      let idx;
      while ((idx = buffer.indexOf("\n\n")) >= 0) {
        const line = buffer.slice(0, idx).trim(); buffer = buffer.slice(idx + 2);
        if (!line.startsWith("data:")) continue;
        const d = line.slice(5).trim();
        if (d === "[DONE]") continue;
        try {
          const delta = JSON.parse(d).choices?.[0]?.delta?.content;
          if (delta && !ac.signal.aborted) {
            full += delta;
            emit({ reply: full });
            if (speakHere) {
              genPending = true;
              send(ttsWs, { type: "input_text.append", text: delta.replace(/[*_#`~>|]/g, "") });
            }
          }
        } catch {}
      }
    }
  } catch (e) {
    if (e.name !== "AbortError") {
      emit({ state: "error", status: "reply error: " + e.message });
      log({ ev: "reply-fail", err: String(e) });
    }
  } finally {
    if (chatAbort === ac) chatAbort = null;
    if (full && speakHere) send(ttsWs, { type: "input_text.flush" });   // always close the generation
  }
  history.push({ role: "assistant", content: full });
  fetch("/api/voice/turn", { method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ heard: userText, reply: full, source_role: opts.role,
                           client_id: opts.clientId, speak_on: speakOn }) }).catch(() => {});
  if (!speakHere || !full || !ttsWs) { if (speaking) endTurn(); }   // text-only: end when the stream ends
}

// ---------- turn control ----------
function endTurn() {
  holding = false; turnActive = false; speaking = false;
  if (endTimer) { clearTimeout(endTimer); endTimer = null; }
  if (micCtx && micCtx.state === "suspended") micCtx.resume();
  emit({ state: "ready", status: "ready — hold to talk" });
}

function interrupt() {
  // barge-in: stop the current reply and discard its remaining audio
  if (chatAbort) chatAbort.abort();
  if (genPending) { dropGenerations++; genPending = false; }
  resetPlayback();
  endTurn();
}

async function beginTurn() {
  if (speaking) interrupt();
  if (turnActive) return;
  if (config.mic !== opts.role) { opts.onBlocked?.("mic is set to " + config.mic); return; }
  if (!micReady) { opts.onBlocked?.("no mic on this device"); return; }
  // iOS may suspend the AudioContexts when the AR session starts
  if (audioCtx && audioCtx.state === "suspended") audioCtx.resume();
  if (micCtx && micCtx.state === "suspended") micCtx.resume();
  turnActive = true; holding = true; firstAudio = 0; t0 = performance.now();
  emit({ state: "listening", heard: "", reply: "", status: "transcribing live…", timings: "" });
  try { await openStt(); }
  catch (e) {
    log({ ev: "stt-connect-fail", err: String(e) });
    endTurn();
    emit({ state: "error", status: "STT connect error: " + e.message });
    return;
  }
  // safety: never listen longer than MAX_TURN_MS
  endTimer = setTimeout(stopListening, MAX_TURN_MS);
}

function stopListening() {
  if (!turnActive || speaking || !holding) return;
  holding = false;                    // stop streaming mic chunks
  send(sttWs, { type: "input_audio.end" });
  emit({ state: "thinking", status: "processing…" });
  // fallback: if transcription.done never arrives, reset anyway
  if (doneFallback) clearTimeout(doneFallback);
  doneFallback = setTimeout(() => { if (!speaking) endTurn(); }, 5000);
}

// ---------- enable (call from a real user tap, before entering AR) ----------
async function enable() {
  if (ui.state !== "off" && ui.state !== "error") return true;
  try { await initAudioOut(); await audioCtx.resume(); }
  catch (e) {
    emit({ state: "error", status: "audio: " + e.message });
    log({ ev: "audio-fail", err: String(e) });
    return false;
  }
  try { await startMic(); await micCtx.resume(); micReady = true; }
  catch (e) { micReady = false; log({ ev: "mic-fail", err: String(e) }); }
  emit({ state: "ready", status: micReady ? "voice ready" : "audio only (no mic)" });
  log({ ev: "enabled", micReady, micRate: micCtx?.sampleRate, outRate: audioCtx.sampleRate });
  return true;
}

// ---------- speak a remote turn (TTS only, nothing posted back) ----------
async function speak(text) {
  if (!text || ui.state === "off") return;
  if (speaking) interrupt();
  speaking = true; firstAudio = 0; t0 = performance.now();
  emit({ state: "speaking", reply: text, status: "speaking…", timings: "" });
  try {
    await openTts(); resetPlayback(); genPending = true;
    send(ttsWs, { type: "input_text.append", text: text.replace(/[*_#`~>|]/g, "") });
    send(ttsWs, { type: "input_text.flush" });
  } catch (e) { log({ ev: "speak-fail", err: String(e) }); endTurn(); }
}

window.OnlyVibesVoice = {
  init(o) { opts = { ...o }; },
  setConfig(c) { if (c && c.rev !== config.rev) { config = c; emit({}); } },
  enable, begin: beginTurn, end: stopListening, interrupt, speak, log,
  get state() { return ui.state; },
  get micReady() { return micReady; },
};
})();
