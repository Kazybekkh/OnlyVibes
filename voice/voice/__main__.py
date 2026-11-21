"""CLI demo: `python -m voice` -- press Enter, talk, hear the reply.

Options:
  --text "..."      skip the mic, send text directly
  --turns N         number of voice turns (default 1)
  --language XX     pin the transcription language (e.g. en, fr)
  --no-play         don't play audio, just print
  --realtime        near-real-time streaming loop (Voxtral Realtime 3 STT +
                    streaming chat + realtime TTS); needs mic/speaker
"""

from __future__ import annotations

import argparse
import asyncio
import sys


def main() -> int:
    parser = argparse.ArgumentParser(prog="python -m voice")
    parser.add_argument("--text", help="send text instead of recording")
    parser.add_argument("--turns", type=int, default=1)
    parser.add_argument("--language", default=None)
    parser.add_argument("--no-play", action="store_true")
    parser.add_argument("--max-seconds", type=float, default=30.0)
    parser.add_argument("--realtime", action="store_true",
                        help="streaming near-real-time voice loop")
    args = parser.parse_args()

    if args.realtime:
        from .realtime import RealtimeVoiceAgent
        agent = RealtimeVoiceAgent()

        async def run() -> int:
            if args.text:
                # feed text straight into the streaming LLM -> realtime TTS
                from .realtime import streaming_chat_deltas
                agent.player.start()
                reply = await agent.tts.speak_stream(
                    streaming_chat_deltas(args.text, system=agent.system),
                    on_chunk=agent.player.write)
                agent.player.finish()
                print(f"assistant: {reply.text}")
                print(f"[timings] {agent.last_timings or 'n/a'}")
                return 0
            await agent.converse(turns=args.turns)
            return 0
        return asyncio.run(run())

    from .interface import VoiceInterface

    voice = VoiceInterface()
    play = not args.no_play

    if args.text:
        turn = voice.reply_to(args.text, play=play)
        print(f"you:    {turn.heard.text}")
        print(f"assistant: {turn.reply_text}")
        return 0

    for i in range(args.turns):
        input(f"[turn {i + 1}/{args.turns}] Press Enter, then talk...")
        turn = voice.listen_and_reply(max_seconds=args.max_seconds, play=play)
        print(f"you:       {turn.heard.text}")
        print(f"assistant: {turn.reply_text}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
