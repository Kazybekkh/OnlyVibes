"""Core pluggable voice interface.

Everything in this package is built around three small protocols. Any app can
implement them (or use the Voxtral defaults) and get a full talk-and-listen
loop:

    Recorder  --records mic-->  bytes (wav)
    STT       --transcribes-->  Transcript
    Responder --text in/out-->  str            (your app's brain: any LLM/agent)
    TTS       --synthesizes-->  bytes (audio)
    Player    --plays-->        speaker

`VoiceAgent` wires them together. Swap any piece without touching the others.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Optional, Protocol, runtime_checkable


@dataclass
class Transcript:
    """Result of speech-to-text."""

    text: str
    language: Optional[str] = None
    raw: Dict[str, Any] = field(default_factory=dict)


@dataclass
class Utterance:
    """Result of text-to-speech: playable audio plus the text that produced it."""

    text: str
    audio: bytes
    content_type: str = "audio/wav"
    voice_id: Optional[str] = None
    raw: Dict[str, Any] = field(default_factory=dict)


@dataclass
class ConversationTurn:
    """One full turn of a voice conversation."""

    heard: Transcript
    reply_text: str
    reply_audio: Utterance


@runtime_checkable
class Recorder(Protocol):
    """Captures audio from a microphone (or any source) into bytes."""

    def record(self, max_seconds: float = 30.0) -> bytes:
        """Record up to `max_seconds` and return encoded audio (wav by default)."""
        ...


@runtime_checkable
class Player(Protocol):
    """Plays encoded audio bytes out loud (or hands them to the app)."""

    def play(self, audio: bytes, content_type: str = "audio/wav") -> None:
        ...


@runtime_checkable
class STT(Protocol):
    """Speech-to-text."""

    def transcribe(self, audio: bytes, content_type: str = "audio/wav",
                   language: Optional[str] = None) -> Transcript:
        ...


@runtime_checkable
class TTS(Protocol):
    """Text-to-speech."""

    def synthesize(self, text: str, voice_id: Optional[str] = None) -> Utterance:
        ...


# A responder is any callable mapping the heard text to a reply string.
# Plug in any app logic here: an LLM chat, a command handler, a RAG pipeline...
Responder = Callable[[str], str]


class VoiceInterface:
    """The drop-in facade apps actually use.

    Parameters
    ----------
    responder:
        Callable ``str -> str`` that produces the reply text. Defaults to a
        Mistral chat completion (see `default_responder`).
    stt / tts:
        Speech-to-text and text-to-speech backends. Default to Voxtral.
    recorder / player:
        Audio I/O backends. Default to sounddevice mic/speaker; pass
        `FileRecorder`/`FilePlayer` (or your own) in apps that handle audio
        themselves (e.g. a web frontend streaming over WebSocket).
    """

    def __init__(
        self,
        responder: Optional[Responder] = None,
        stt: Optional[STT] = None,
        tts: Optional[TTS] = None,
        recorder: Optional[Recorder] = None,
        player: Optional[Player] = None,
    ) -> None:
        from .agent import VoiceAgent, default_responder
        from .audio_io import SoundDeviceRecorder, SoundDevicePlayer
        from .voxtral import VoxtralSTT, VoxtralTTS

        self.agent = VoiceAgent(
            responder=responder or default_responder(),
            stt=stt or VoxtralSTT(),
            tts=tts or VoxtralTTS(),
            recorder=recorder or SoundDeviceRecorder(),
            player=player or SoundDevicePlayer(),
        )

    def listen_and_reply(self, max_seconds: float = 30.0,
                         play: bool = True) -> ConversationTurn:
        """Record from the mic, transcribe, get a reply, speak it. One call."""
        return self.agent.turn(max_seconds=max_seconds, play=play)

    def reply_to(self, text: str, play: bool = True) -> ConversationTurn:
        """Skip the mic: give text in, get spoken reply out."""
        return self.agent.turn_from_text(text, play=play)
