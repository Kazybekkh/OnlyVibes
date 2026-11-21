"""How to plug the voice interface into ANY app.

Three levels of integration, from easiest to most custom:

  1. One-liner facade      -- VoiceInterface() with defaults (Voxtral + mic)
  2. Custom brain          -- keep Voxtral for speech, plug in YOUR logic
  3. Fully custom          -- your own STT/TTS/audio backends, any responder

Run:  python examples/plug_into_app.py --text "Say hi"
"""

from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


def level_1_one_liner(text: str) -> None:
    """Easiest: default everything. Talk (or pass text) -> spoken reply."""
    from voice import VoiceInterface

    voice = VoiceInterface()  # reads MISTRAL_API_KEY from the environment
    turn = voice.reply_to(text)  # or voice.listen_and_reply() to use the mic
    print(f"[level 1] you: {turn.heard.text}")
    print(f"[level 1] assistant: {turn.reply_text}")


def level_2_custom_brain(text: str) -> None:
    """Same Voxtral speech, but YOUR app logic answers instead of a raw LLM."""
    from voice import (SoundDevicePlayer, SoundDeviceRecorder, VoiceAgent,
                       VoxtralSTT, VoxtralTTS)

    def my_app_logic(user_text: str) -> str:
        # Replace with anything: your agent, a command router, a RAG chain...
        if "time" in user_text.lower():
            import datetime
            return f"It is {datetime.datetime.now():%H:%M}."
        return f"You said: {user_text}. I am your app's own voice brain."

    agent = VoiceAgent(
        responder=my_app_logic,
        stt=VoxtralSTT(),                 # voxtral-mini-transcribe-3
        tts=VoxtralTTS(),                 # voxtral-mini-tts-3-0
        recorder=SoundDeviceRecorder(),
        player=SoundDevicePlayer(),
    )
    turn = agent.turn_from_text(text)
    print(f"[level 2] assistant: {turn.reply_text}")


def level_3_fully_custom(text: str, out_dir: str = ".") -> None:
    """No mic/speaker, no Mistral at all: prove every piece is swappable.

    Uses a fake STT/TTS and file-based audio I/O so it runs offline.
    """
    from voice import (FilePlayer, FileRecorder, NullPlayer, VoiceAgent)
    from voice.interface import Transcript, Utterance

    class EchoSTT:
        def transcribe(self, audio: bytes, content_type: str = "audio/wav",
                       language=None) -> Transcript:
            return Transcript(text="(offline echo) " + text)

    class BeepTTS:
        def synthesize(self, text: str, voice_id=None) -> Utterance:
            # 0.2s of silence as a stand-in for real speech audio
            from voice.audio_io import encode_wav
            return Utterance(text=text, audio=encode_wav(b"\x00\x00" * 3200))

    agent = VoiceAgent(
        responder=lambda t: f"Replying to: {t}",
        stt=EchoSTT(),
        tts=BeepTTS(),
        recorder=FileRecorder(__file__),  # any file stands in for the mic
        player=FilePlayer(out_dir),
    )
    turn = agent.turn_from_text(text, play=True)
    print(f"[level 3] heard: {turn.heard.text}")
    print(f"[level 3] assistant: {turn.reply_text}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--text", default="What time is it?")
    ap.add_argument("--level", type=int, default=3,
                    help="1=facade, 2=custom brain, 3=fully custom (offline)")
    args = ap.parse_args()
    {1: level_1_one_liner, 2: level_2_custom_brain,
     3: level_3_fully_custom}[args.level](args.text)
