"""Near-real-time voice loop via the official mistralai SDK (mistralai[realtime]).

Pipeline per turn, streaming at every stage:

  mic (VAD, 100ms chunks) ──► Voxtral Mini Transcribe Realtime 3  (WS, deltas live)
       ──► streaming chat (SSE tokens)
       ──► Voxtral Mini TTS 3 realtime (WS, pcm chunks) ──► gapless speaker

Models (mistral.ai/news/voxtral-3):
  STT: voxtral-mini-transcribe-realtime-3   client.audio.realtime
  TTS: voxtral-mini-tts-3-0                  client.audio.realtime_speech
"""

from __future__ import annotations

import asyncio
import os
import queue
import time
from typing import Any, AsyncIterator, Callable, Dict, List, Optional

from .interface import ConversationTurn, Transcript, Utterance
from .voxtral import CHAT_MODEL, DEFAULT_VOICE_ID, TTS_MODEL, load_dotenv

STT_RT_MODEL = os.environ.get("VOXTRAL_STT_RT_MODEL",
                              "voxtral-mini-transcribe-realtime-3")

MIC_RATE = int(os.environ.get("VOICE_SAMPLE_RATE", "16000"))
# Voxtral TTS `pcm` output is raw float32 little-endian, 24 kHz mono
# (docs.mistral.ai audio formats). Reading it as int16 at 48 kHz has the same
# duration but turns every sample into noise.
TTS_RATE = int(os.environ.get("VOXTRAL_TTS_PCM_RATE", "24000"))
TTS_DTYPE = "<f4"
CHUNK_MS = 100


def _client():
    """Mistral SDK client (api key from env / .env)."""
    from mistralai.client import Mistral
    load_dotenv()
    return Mistral(api_key=os.environ.get("MISTRAL_API_KEY"))


# --------------------------------------------------------------------------
# Mic capture with energy-based VAD
# --------------------------------------------------------------------------

class StreamingRecorder:
    """Async-iterates 16 kHz mono int16 PCM chunks from the mic.

    Starts yielding on speech onset (with ~0.5s pre-roll) and stops after
    `silence_timeout` seconds of trailing silence, or at `max_seconds`.
    """

    def __init__(self, sample_rate: int = MIC_RATE, chunk_ms: int = CHUNK_MS,
                 silence_timeout: float = 0.8, energy_threshold: float = 400.0,
                 max_seconds: float = 30.0):
        self.sample_rate = sample_rate
        self.chunk_frames = sample_rate * chunk_ms // 1000
        self.silence_timeout = silence_timeout
        self.energy_threshold = energy_threshold
        self.max_seconds = max_seconds

    async def stream(self, gate: Optional[asyncio.Event] = None
                     ) -> AsyncIterator[bytes]:
        """Yield mic chunks.

        gate=None  -> VAD mode: start on speech onset, stop after trailing silence.
        gate=Event -> push-to-talk: wait for gate (key down), yield while held,
                      stop when the gate clears (key up). max_seconds still caps.
        """
        import numpy as np
        import sounddevice as sd

        loop = asyncio.get_running_loop()
        q: queue.Queue = queue.Queue()
        stream = sd.InputStream(
            samplerate=self.sample_rate, channels=1, dtype="int16",
            blocksize=self.chunk_frames,
            callback=lambda indata, frames, t, status:
                loop.call_soon_threadsafe(q.put, indata.copy()))
        stream.start()
        try:
            if gate is not None:
                await gate.wait()  # key pressed
                while not q.empty():  # discard pre-key backlog
                    try:
                        q.get_nowait()
                    except queue.Empty:
                        break
                max_chunks = int(self.max_seconds * 1000 / CHUNK_MS)
                for _ in range(max_chunks):
                    if not gate.is_set():
                        return  # key released
                    chunk = await asyncio.to_thread(q.get)
                    yield chunk.tobytes()
                return
            pre_roll: List[bytes] = []
            speaking = False
            silence_chunks = 0
            max_chunks = int(self.max_seconds * 1000 / CHUNK_MS)
            for _ in range(max_chunks):
                chunk = await asyncio.to_thread(q.get)
                pcm = chunk.tobytes()
                rms = float(np.sqrt(np.mean(np.square(chunk.astype("float32")))))
                if not speaking:
                    pre_roll.append(pcm)
                    if len(pre_roll) > 5:
                        pre_roll.pop(0)
                    if rms > self.energy_threshold:
                        speaking = True
                        silence_chunks = 0
                        for p in pre_roll:
                            yield p
                        pre_roll = []
                        yield pcm
                else:
                    yield pcm
                    if rms < self.energy_threshold:
                        silence_chunks += 1
                        if silence_chunks * CHUNK_MS / 1000 >= self.silence_timeout:
                            return
                    else:
                        silence_chunks = 0
        finally:
            stream.stop()
            stream.close()


# --------------------------------------------------------------------------
# Realtime STT via the SDK: Voxtral Mini Transcribe Realtime 3
# --------------------------------------------------------------------------

class SdkRealtimeSTT:
    """Streaming speech-to-text using client.audio.realtime (Voxtral Realtime 3)."""

    def __init__(self, model: str = STT_RT_MODEL,
                 target_streaming_delay_ms: int = 480,
                 client=None):
        self.model = model
        self.target_streaming_delay_ms = target_streaming_delay_ms
        self._client = client

    async def transcribe_stream(
        self, chunks: AsyncIterator[bytes],
        language: Optional[str] = None,
        on_delta: Optional[Callable[[str], None]] = None,
    ) -> Transcript:
        from mistralai.client.models import (AudioFormat,
                                             TranscriptionStreamDone,
                                             TranscriptionStreamTextDelta)
        client = self._client or _client()
        audio_format = AudioFormat(encoding="pcm_s16le", sample_rate=MIC_RATE)

        async def gen():
            async for pcm in chunks:
                yield pcm

        parts: List[str] = []
        async for event in client.audio.realtime.transcribe_stream(
                gen(), model=self.model, audio_format=audio_format,
                target_streaming_delay_ms=self.target_streaming_delay_ms,
                language=language):
            if isinstance(event, TranscriptionStreamTextDelta):
                parts.append(event.text)
                if on_delta:
                    on_delta(event.text)
            elif isinstance(event, TranscriptionStreamDone):
                return Transcript(text=event.text or "".join(parts),
                                  language=event.language, raw=event.model_dump())
        return Transcript(text="".join(parts))


# --------------------------------------------------------------------------
# Realtime TTS via the SDK: Voxtral TTS 3 over one reusable WS session
# --------------------------------------------------------------------------

class SdkRealtimeTTS:
    """Streaming text-to-speech via client.audio.realtime_speech.

    Feed text deltas (e.g. LLM tokens) with send_text; PCM chunks arrive as
    SpeechStreamAudioDelta events and can be played immediately.
    """

    def __init__(self, model: str = TTS_MODEL,
                 voice_id: Optional[str] = DEFAULT_VOICE_ID, client=None):
        self.model = model
        self.voice_id = voice_id
        self._client = client
        self._conn = None

    async def connect(self) -> None:
        if self._conn is None or self._conn.is_closed:
            client = self._client or _client()
            self._conn = await client.audio.realtime_speech.connect(
                model=self.model, voice_id=self.voice_id,
                response_format="pcm", streaming_mode="responsive")

    async def speak_stream(
        self, text_deltas: AsyncIterator[str],
        on_chunk: Optional[Callable[[bytes], None]] = None,
    ) -> Utterance:
        from mistralai.client.models import (SpeechStreamAudioDelta,
                                             SpeechStreamDone)
        from mistralai.extra.realtime import audio_bytes
        await self.connect()
        conn = self._conn
        reply_parts: List[str] = []
        audio_chunks: List[bytes] = []

        async def pump_text():
            async for delta in text_deltas:
                if delta:
                    reply_parts.append(delta)
                    await conn.send_text(delta)
            await conn.flush()

        pump = asyncio.create_task(pump_text())
        try:
            async for event in conn.events():
                if isinstance(event, SpeechStreamAudioDelta):
                    pcm = audio_bytes(event)
                    audio_chunks.append(pcm)
                    if on_chunk:
                        on_chunk(pcm)
                elif isinstance(event, SpeechStreamDone):
                    break
        finally:
            await pump
        reply = "".join(reply_parts)
        return Utterance(text=reply, audio=b"".join(audio_chunks),
                         content_type="audio/pcm", voice_id=self.voice_id)

    async def close(self) -> None:
        if self._conn is not None and not self._conn.is_closed:
            await self._conn.close()
        self._conn = None


# --------------------------------------------------------------------------
# Streaming chat responder via the SDK (SSE under the hood)
# --------------------------------------------------------------------------

async def streaming_chat_deltas(prompt: str, model: str = CHAT_MODEL,
                                system: Optional[str] = None,
                                history: Optional[List[dict]] = None,
                                client=None) -> AsyncIterator[str]:
    """Yield LLM token deltas from client.chat.stream_async."""
    client = client or _client()
    messages: List[dict] = []
    if system:
        messages.append({"role": "system", "content": system})
    if history:
        messages.extend(history)
    messages.append({"role": "user", "content": prompt})
    stream = await client.chat.stream_async(model=model, messages=messages)
    async for chunk in stream:
        delta = chunk.data.choices[0].delta
        if delta and delta.content:
            yield delta.content


# --------------------------------------------------------------------------
# Gapless PCM player
# --------------------------------------------------------------------------

class StreamingPlayer:
    """Plays realtime-TTS PCM chunks (24 kHz mono float32) gaplessly.

    Chunks go into one contiguous buffer (no reordering, partial samples are
    carried over). Playback prebuffers ~120 ms, rebuffers on underrun instead
    of stuttering, and ramps in/out so starts and underruns don't click.
    """

    def __init__(self, sample_rate: int = TTS_RATE, prebuffer_ms: int = 120,
                 fade_samples: int = 96):
        import threading
        self.sample_rate = sample_rate
        self.prebuffer = sample_rate * prebuffer_ms // 1000
        self.fade = fade_samples
        self._lock = threading.Lock()
        self._buf = bytearray()
        self._stream = None
        self._playing = False
        self._ended = False
        self._gain = 0.0
        self._last = 0.0

    def start(self) -> None:
        import sounddevice as sd
        with self._lock:
            self._buf.clear()
            self._playing = self._ended = False
            self._gain = self._last = 0.0
        self._stream = sd.OutputStream(samplerate=self.sample_rate, channels=1,
                                       dtype="float32", latency="low",
                                       callback=self._callback)
        self._stream.start()

    def _callback(self, outdata, frames, t, status):
        import numpy as np
        out = outdata[:, 0]
        with self._lock:
            avail = len(self._buf) // 4
            if not self._playing and avail and (avail >= self.prebuffer or self._ended):
                self._playing = True
            n = min(frames, avail) if self._playing else 0
            if n:
                data = np.frombuffer(bytes(self._buf[:n * 4]), dtype=TTS_DTYPE)
                del self._buf[:n * 4]
            if self._playing and n < frames:
                self._playing = False  # underrun -> rebuffer
        if n:
            ramp = np.minimum(1.0, self._gain + np.arange(1, n + 1) / self.fade)
            out[:n] = data * ramp
            self._gain = float(ramp[-1])
            if n < frames:  # ran dry mid-block: fade the tail out
                k = min(self.fade, n)
                out[n - k:n] *= np.linspace(1.0, 0.0, k, dtype="float32")
                self._gain = 0.0
            self._last = float(out[n - 1])
        if n < frames:
            tail = self._last * 0.98 ** np.arange(1, frames - n + 1)
            out[n:] = tail
            self._last = float(tail[-1]) if len(tail) else self._last
            self._gain = 0.0

    def write(self, pcm: bytes) -> None:
        with self._lock:
            self._buf += pcm

    def finish(self) -> None:
        with self._lock:
            self._ended = True
            self._buf = self._buf[:len(self._buf) // 4 * 4]  # drop partial sample
        while True:
            with self._lock:
                if not self._buf:
                    break
            time.sleep(0.01)
        time.sleep(0.05)  # let the last block + fade reach the speaker
        if self._stream is not None:
            self._stream.stop()
            self._stream.close()
            self._stream = None


# --------------------------------------------------------------------------
# The near-real-time agent
# --------------------------------------------------------------------------

class RealtimeVoiceAgent:
    """One voice turn, streamed end to end. Plug in any `responder_deltas`."""

    def __init__(self,
                 responder_deltas: Optional[Callable[..., AsyncIterator[str]]] = None,
                 stt: Optional[SdkRealtimeSTT] = None,
                 tts: Optional[SdkRealtimeTTS] = None,
                 recorder: Optional[StreamingRecorder] = None,
                 player: Optional[StreamingPlayer] = None,
                 system: Optional[str] = None,
                 client=None):
        from .agent import DEFAULT_SYSTEM_PROMPT
        self._client = client
        self.stt = stt or SdkRealtimeSTT(client=client)
        self.tts = tts or SdkRealtimeTTS(client=client)
        self.recorder = recorder or StreamingRecorder()
        self.player = player or StreamingPlayer()
        self.system = system or DEFAULT_SYSTEM_PROMPT
        self.history: List[dict] = []
        self.last_timings: Dict[str, float] = {}
        if responder_deltas is None:
            def responder_deltas(text: str) -> AsyncIterator[str]:
                return streaming_chat_deltas(text, system=self.system,
                                             history=self.history,
                                             client=self._client)
        self.responder_deltas = responder_deltas

    async def turn(self, language: Optional[str] = None, verbose: bool = True,
                   gate: Optional[asyncio.Event] = None) -> ConversationTurn:
        t0 = time.perf_counter()
        timings: Dict[str, float] = {}

        heard = await self.stt.transcribe_stream(
            self.recorder.stream(gate=gate), language=language,
            on_delta=(lambda d: print(d, end="", flush=True)) if verbose else None)
        if verbose:
            print()
        timings["transcript_done_s"] = time.perf_counter() - t0

        self.player.start()

        def on_chunk(pcm: bytes) -> None:
            if "first_audio_s" not in timings:
                timings["first_audio_s"] = time.perf_counter() - t0
            self.player.write(pcm)

        reply = await self.tts.speak_stream(
            self.responder_deltas(heard.text), on_chunk=on_chunk)
        self.player.finish()
        timings["total_s"] = time.perf_counter() - t0
        self.last_timings = timings

        self.history.append({"role": "user", "content": heard.text})
        self.history.append({"role": "assistant", "content": reply.text})
        if verbose:
            print(f"[timings] transcript {timings['transcript_done_s']:.2f}s | "
                  f"first audio {timings.get('first_audio_s', 0):.2f}s | "
                  f"total {timings['total_s']:.2f}s")
        return ConversationTurn(heard=heard, reply_text=reply.text, reply_audio=reply)

    async def converse(self, turns: int = 3, verbose: bool = True,
                       gate: Optional[asyncio.Event] = None
                       ) -> List[ConversationTurn]:
        out = []
        try:
            for i in range(turns):
                if gate is not None and verbose:
                    print(f"[turn {i + 1}/{turns}] hold RIGHT OPTION to talk, release to send...")
                out.append(await self.turn(verbose=verbose, gate=gate))
        finally:
            await self.tts.close()
        return out
