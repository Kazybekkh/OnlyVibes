# voice — pluggable voice interface (Mistral Voxtral)

Add talk-and-listen to **any app**: the user speaks, Voxtral transcribes
(`voxtral-mini-transcribe-3`), your app logic answers, Voxtral speaks the reply
(`voxtral-mini-tts-3-0`). Every piece is swappable.

```
 mic ──► Recorder ──► STT (Voxtral) ──► Responder (YOUR app logic) ──► TTS (Voxtral) ──► Player ──► speaker
```

## Setup

```bash
pip install -r requirements.txt        # sounddevice+numpy only if you want local mic/speaker
export MISTRAL_API_KEY=...             # https://console.mistral.ai  (see .env.example)
```

The API client itself is **stdlib-only** — no SDK to install for the Voxtral calls.

## Use it in any app (3 levels)

```python
# 1) One-liner facade — defaults: Voxtral STT/TTS, Mistral chat responder, mic+speaker
from voice import VoiceInterface
voice = VoiceInterface()
turn = voice.listen_and_reply()        # talk -> hear the reply
print(turn.heard.text, "->", turn.reply_text)
```

```python
# 2) Keep Voxtral speech, plug in YOUR brain (any str -> str callable)
from voice import VoiceAgent, VoxtralSTT, VoxtralTTS, SoundDeviceRecorder, SoundDevicePlayer
agent = VoiceAgent(
    responder=my_app_logic,            # your agent / commands / RAG
    stt=VoxtralSTT(), tts=VoxtralTTS(),
    recorder=SoundDeviceRecorder(), player=SoundDevicePlayer(),
)
agent.turn()                           # one full voice turn
```

```python
# 3) Fully custom — no mic, no Mistral: swap STT/TTS/audio I/O too
from voice import VoiceAgent, FileRecorder, FilePlayer
agent = VoiceAgent(responder=lambda t: f"echo: {t}",
                   stt=MySTT(), tts=MyTTS(),
                   recorder=FileRecorder("in.wav"), player=FilePlayer("."))
```

See `examples/plug_into_app.py` (level 3 runs fully offline).

## CLI

```bash
python -m voice                          # press Enter, talk, hear the reply
python -m voice --realtime               # near-real-time streaming loop (mic required)
python -m voice --realtime --ptt         # push-to-talk: hold RIGHT OPTION to talk, release to send
python -m voice --text "What time is it?" --no-play
python -m voice --turns 3 --language en
```

`--ptt` listens globally for the right Option key (pynput, `Key.alt_r` /
VK 0x3E). Tested with the real mic: audio flows only while the key is held
(first chunk ~0.13s after key-down).

**macOS permission ("process is not trusted"):** the trusted process is the
Python binary itself, not your terminal. Under `uv run` that is the resolved
real path of `.venv/bin/python3`, e.g.
`~/.local/share/uv/python/cpython-3.12.11-macos-aarch64-none/bin/python3.12`
(the exact path is printed in the error). Add it to **both**:
System Settings → Privacy & Security → **Accessibility** and → **Input
Monitoring** → "+" → paste the path. Then **restart** the voice process.
Open the pane directly:

```bash
open 'x-apple.systempreferences:com.apple.settings.PrivacySecurity.extension?Privacy_Accessibility'
```

If you skip this, just run `--realtime` without `--ptt` — VAD mode needs no
special permissions.

## Browser webapp (no terminal, no Accessibility permission)

```bash
uv run python -m voice.webapp        # http://localhost:8090  (PORT env to change)
```

Open the page, allow the **microphone** in the browser prompt, then hold the
big button (or hold SPACE) to talk — release to send. The API key never
leaves the server: `/api/token` mints short-lived `rt_*` tokens
(`POST /v1/client/sessions`), the browser opens the Voxtral realtime
STT/TTS websockets directly with `Sec-WebSocket-Protocol: ["realtime", token]`,
and `/api/chat` is an SSE proxy for the streaming LLM. Live transcript deltas
and streamed reply audio (48 kHz PCM via Web Audio) included.

## Near-real-time mode (`--realtime`)

Every stage streams, via the official `mistralai[realtime]` SDK:

```
 mic (VAD, 100ms chunks) → Voxtral Mini Transcribe Realtime 3 (WS, live deltas)
   → streaming chat tokens → Voxtral Mini TTS 3 realtime (WS, pcm chunks) → speaker
```

Measured live (headless test, audio streamed as fast as possible):
transcript deltas from **0.84s**, realtime-TTS first audio from **1.16s**,
full turn heard→spoken-reply in **3.65s**. With a real mic the stages overlap
with the user speaking.

```python
from voice import RealtimeVoiceAgent
agent = RealtimeVoiceAgent()             # all defaults, still fully pluggable
turn = asyncio.run(agent.turn())        # or await agent.converse(turns=3)
print(agent.last_timings)                # transcript/first-audio/total seconds
```

Latency knobs: `StreamingRecorder(silence_timeout=0.8, energy_threshold=...)`,
`SdkRealtimeSTT(target_streaming_delay_ms=480)` (lower = snappier transcript,
less accurate), `SdkRealtimeTTS` uses `streaming_mode="responsive"` +
`response_format="pcm"` — realtime PCM is **48 kHz** mono int16 (the offline
endpoint returns 24 kHz WAV/MP3; playing realtime PCM at 24 kHz is what caused
slowed/garbled audio). Override with `VOXTRAL_TTS_PCM_RATE` if Mistral changes it.

## The plug points (protocols in `voice/interface.py`)

| Piece | Protocol | Default | Swap with |
|---|---|---|---|
| mic → bytes | `Recorder.record()` | `SoundDeviceRecorder` | `FileRecorder`, browser WebSocket bridge, … |
| bytes → text | `STT.transcribe()` | `VoxtralSTT` | Whisper, your own model, … |
| text → reply text | `Responder` (`str -> str`) | Mistral chat (`mistral-small-latest`) | your agent, command router, … |
| text → audio | `TTS.synthesize()` | `VoxtralTTS` | any TTS, cloned `voice_id`, … |
| audio → speaker | `Player.play()` | `SoundDevicePlayer` | `FilePlayer`, `NullPlayer`, … |

## Config (env vars)

`MISTRAL_API_KEY` (required), `VOXTRAL_STT_MODEL`, `VOXTRAL_TTS_MODEL`,
`VOXTRAL_VOICE_ID`, `VOICE_CHAT_MODEL`, `VOICE_SAMPLE_RATE`, `MISTRAL_API_BASE`.

## API endpoints used (docs.mistral.ai)

- `POST /v1/audio/transcriptions` — Voxtral Mini Transcribe 3 (speech → text)
- `POST /v1/audio/speech` — Voxtral Mini TTS 3 (text → speech, preset/cloned voices)
- `POST /v1/chat/completions` — default responder brain
