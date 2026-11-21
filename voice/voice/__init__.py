"""Pluggable voice interface powered by Mistral Voxtral.

Drop into any app:

    from voice import VoiceInterface

    voice = VoiceInterface()                 # uses MISTRAL_API_KEY + Voxtral
    turn = voice.listen_and_reply()          # talk -> spoken reply
    print(turn.heard.text, turn.reply_text)

Or wire the pieces yourself (any responder / STT / TTS / audio I/O):

    from voice import VoiceAgent, VoxtralSTT, VoxtralTTS, default_responder
    from voice import SoundDeviceRecorder, SoundDevicePlayer

    agent = VoiceAgent(
        responder=my_app_logic,              # any str -> str callable
        stt=VoxtralSTT(), tts=VoxtralTTS(),
        recorder=SoundDeviceRecorder(), player=SoundDevicePlayer(),
    )
    agent.turn()
"""

from .interface import (ConversationTurn, Player, Recorder, Responder, STT,
                        TTS, Transcript, Utterance, VoiceInterface)
from .agent import VoiceAgent, default_responder
from .voxtral import (MistralAPIError, VoxtralSTT, VoxtralTTS,
                      mistral_chat_reply)
from .audio_io import (FilePlayer, FileRecorder, NullPlayer,
                       SoundDevicePlayer, SoundDeviceRecorder, encode_wav)
from .realtime import (RealtimeVoiceAgent, SdkRealtimeSTT, SdkRealtimeTTS,
                        StreamingPlayer, StreamingRecorder,
                        streaming_chat_deltas)

__all__ = [
    "RealtimeVoiceAgent", "SdkRealtimeSTT", "SdkRealtimeTTS",
    "StreamingRecorder", "StreamingPlayer", "streaming_chat_deltas",
    "VoiceInterface", "VoiceAgent", "default_responder",
    "Recorder", "Player", "STT", "TTS", "Responder",
    "Transcript", "Utterance", "ConversationTurn",
    "VoxtralSTT", "VoxtralTTS", "mistral_chat_reply", "MistralAPIError",
    "SoundDeviceRecorder", "SoundDevicePlayer",
    "FileRecorder", "FilePlayer", "NullPlayer", "encode_wav",
]

__version__ = "0.1.0"
