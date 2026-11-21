"""VoiceAgent: wires Recorder -> STT -> Responder -> TTS -> Player."""

from __future__ import annotations

from typing import Callable, List, Optional

from .interface import (ConversationTurn, Player, Recorder, Responder, STT,
                        Transcript, TTS, Utterance)
from .voxtral import mistral_chat_reply

DEFAULT_SYSTEM_PROMPT = (
    "You are a helpful voice assistant. The user speaks to you and hears your "
    "reply spoken aloud, so keep answers short, natural, and conversational."
)


def default_responder(
    system: Optional[str] = DEFAULT_SYSTEM_PROMPT,
    model: Optional[str] = None,
) -> Responder:
    """Build the default responder: a Mistral chat completion (Voxtral-family)."""

    def respond(text: str) -> str:
        return mistral_chat_reply(text, model=model or "mistral-small-latest",
                                  system=system)

    return respond


class VoiceAgent:
    """A full voice conversation loop with every piece pluggable.

    Parameters
    ----------
    responder: callable str -> str. This is where YOUR app plugs in: pass any
        LLM chain, command handler, or agent. Defaults to Mistral chat.
    stt, tts: speech backends (default: Voxtral).
    recorder, player: audio I/O backends (default: sounddevice mic/speaker).
    history: optional list of {"role", "content"} dicts threaded into the
        default responder for multi-turn context. Custom responders can keep
        their own state.
    """

    def __init__(
        self,
        responder: Responder,
        stt: STT,
        tts: TTS,
        recorder: Recorder,
        player: Player,
        history: Optional[List[dict]] = None,
    ) -> None:
        self.responder = responder
        self.stt = stt
        self.tts = tts
        self.recorder = recorder
        self.player = player
        self.history = history if history is not None else []

    def turn_from_text(self, text: str, play: bool = True) -> ConversationTurn:
        """One turn from text input (no mic): respond and speak the reply."""
        heard = Transcript(text=text)
        reply_text = self.responder(text)
        reply_audio = self.tts.synthesize(reply_text)
        if play:
            self.player.play(reply_audio.audio, reply_audio.content_type)
        if isinstance(self.responder, Callable) and self.history is not None:
            self.history.append({"role": "user", "content": text})
            self.history.append({"role": "assistant", "content": reply_text})
        return ConversationTurn(heard=heard, reply_text=reply_text,
                                reply_audio=reply_audio)

    def turn(self, max_seconds: float = 30.0, play: bool = True,
             language: Optional[str] = None) -> ConversationTurn:
        """One full turn: record -> transcribe -> respond -> speak."""
        audio = self.recorder.record(max_seconds=max_seconds)
        heard = self.stt.transcribe(audio, language=language)
        reply_text = self.responder(heard.text)
        reply_audio = self.tts.synthesize(reply_text)
        if play:
            self.player.play(reply_audio.audio, reply_audio.content_type)
        self.history.append({"role": "user", "content": heard.text})
        self.history.append({"role": "assistant", "content": reply_text})
        return ConversationTurn(heard=heard, reply_text=reply_text,
                                reply_audio=reply_audio)

    def converse(self, turns: int = 3, max_seconds: float = 30.0) -> List[ConversationTurn]:
        """Run `turns` back-to-back voice turns."""
        return [self.turn(max_seconds=max_seconds) for _ in range(turns)]
