"""Pluggable audio I/O backends.

Two interchangeable pieces:

  Recorder.record() -> bytes   (mic -> encoded audio)
  Player.play(bytes)           (encoded audio -> speaker / app)

Backends provided:
  SoundDeviceRecorder / SoundDevicePlayer  -- real mic + speaker (needs
      `pip install sounddevice numpy`; PortAudio system lib on macOS/Linux)
  FileRecorder / FilePlayer                -- file-based, for tests and for
      apps that want to handle audio bytes themselves (web frontends, etc.)

Write your own by implementing the two methods -- e.g. a WebSocket bridge that
streams mic audio from a browser and plays the reply back in the browser.
"""

from __future__ import annotations

import io
import os
import wave
from typing import Optional

SAMPLE_RATE = int(os.environ.get("VOICE_SAMPLE_RATE", "16000"))
CHANNELS = 1


def encode_wav(pcm: bytes, sample_rate: int = SAMPLE_RATE,
               channels: int = CHANNELS, sample_width: int = 2) -> bytes:
    """Wrap raw little-endian PCM in a WAV container (stdlib only)."""
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(channels)
        w.setsampwidth(sample_width)
        w.setframerate(sample_rate)
        w.writeframes(pcm)
    return buf.getvalue()


def _require_sounddevice():
    try:
        import sounddevice as sd  # noqa: F401
        import numpy as np  # noqa: F401
    except ImportError as e:
        raise ImportError(
            "SoundDeviceRecorder/Player need `pip install sounddevice numpy` "
            "(plus the PortAudio system library). For apps that handle audio "
            "themselves, pass FileRecorder/FilePlayer or your own backend."
        ) from e
    return sd, np


class SoundDeviceRecorder:
    """Records from the default microphone via sounddevice."""

    def __init__(self, sample_rate: int = SAMPLE_RATE, channels: int = CHANNELS):
        self.sample_rate = sample_rate
        self.channels = channels

    def record(self, max_seconds: float = 30.0) -> bytes:
        sd, np = _require_sounddevice()
        frames = int(self.sample_rate * max_seconds)
        pcm = sd.rec(frames, samplerate=self.sample_rate, channels=self.channels,
                     dtype="int16")
        sd.wait()
        return encode_wav(np.asarray(pcm).tobytes(), self.sample_rate,
                          self.channels)


class SoundDevicePlayer:
    """Plays audio bytes through the default speaker via sounddevice."""

    def play(self, audio: bytes, content_type: str = "audio/wav") -> None:
        sd, np = _require_sounddevice()
        if "wav" in content_type:
            with wave.open(io.BytesIO(audio), "rb") as w:
                rate, width, channels = (w.getframerate(), w.getsampwidth(),
                                         w.getnchannels())
                raw = w.readframes(w.getnframes())
            dtype = {1: "int8", 2: "int16", 4: "int32"}[width]
            data = np.frombuffer(raw, dtype=dtype)
            if channels > 1:
                data = data.reshape(-1, channels)
        else:
            # mp3/ogg/etc: decode with the `soundfile` package if available.
            try:
                import soundfile as sf
            except ImportError as e:
                raise ImportError(
                    f"Playing {content_type} needs `pip install soundfile`."
                ) from e
            data, rate = sf.read(io.BytesIO(audio), dtype="int16")
        sd.play(data, rate)
        sd.wait()


class FileRecorder:
    """Reads 'recorded' audio from a file -- handy for tests and headless use."""

    def __init__(self, path: str):
        self.path = path

    def record(self, max_seconds: float = 30.0) -> bytes:
        with open(self.path, "rb") as f:
            return f.read()


class FilePlayer:
    """Writes played audio to a file instead of a speaker."""

    def __init__(self, directory: str = "."):
        self.directory = directory
        self.count = 0

    def play(self, audio: bytes, content_type: str = "audio/wav") -> str:
        self.count += 1
        ext = content_type.split("/")[-1].replace("x-", "").replace("wave", "wav")
        path = os.path.join(self.directory, f"reply_{self.count}.{ext}")
        with open(path, "wb") as f:
            f.write(audio)
        return path


class NullPlayer:
    """Discards audio but returns it -- for apps that only want the bytes."""

    def play(self, audio: bytes, content_type: str = "audio/wav") -> bytes:
        return audio
