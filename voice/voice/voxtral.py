"""Voxtral (Mistral AI) speech backends over the documented REST API.

Uses only the Python standard library, so this module drops into any app
without extra dependencies. Endpoints (docs.mistral.ai):

  STT: POST /v1/audio/transcriptions   model: voxtral-mini-transcribe-3
  TTS: POST /v1/audio/speech           model: voxtral-mini-tts-3-0
  LLM: POST /v1/chat/completions       (used by the default responder)

Auth: MISTRAL_API_KEY environment variable (get one at https://console.mistral.ai).
"""

from __future__ import annotations

import json
import os
import uuid
from typing import Any, Dict, Optional
from urllib import request as urlrequest
from urllib.error import HTTPError, URLError

from .interface import Transcript, Utterance

API_BASE = os.environ.get("MISTRAL_API_BASE", "https://api.mistral.ai/v1")

STT_MODEL = os.environ.get("VOXTRAL_STT_MODEL", "voxtral-mini-transcribe-3")
TTS_MODEL = os.environ.get("VOXTRAL_TTS_MODEL", "voxtral-mini-tts-3-0")
CHAT_MODEL = os.environ.get("VOICE_CHAT_MODEL", "mistral-small-latest")
DEFAULT_VOICE_ID = os.environ.get("VOXTRAL_VOICE_ID", "voice_592")


class MistralAPIError(RuntimeError):
    """Raised when a Mistral API call fails."""

    def __init__(self, status: Optional[int], body: str) -> None:
        self.status = status
        self.body = body
        super().__init__(f"Mistral API error (status={status}): {body[:500]}")


def _api_key() -> str:
    key = os.environ.get("MISTRAL_API_KEY")
    if not key:
        raise MistralAPIError(
            None,
            "MISTRAL_API_KEY is not set. Create a key at "
            "https://console.mistral.ai and export it.",
        )
    return key


def _post_json(path: str, payload: Dict[str, Any],
               timeout: float = 120.0) -> Dict[str, Any]:
    req = urlrequest.Request(
        f"{API_BASE}{path}",
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {_api_key()}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urlrequest.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except HTTPError as e:
        raise MistralAPIError(e.code, e.read().decode("utf-8", "replace")) from e
    except URLError as e:
        raise MistralAPIError(None, str(e)) from e


def _post_multipart(path: str, fields: Dict[str, str],
                    file_field: str, file_bytes: bytes,
                    file_name: str, file_type: str,
                    timeout: float = 120.0) -> Dict[str, Any]:
    boundary = f"----voxtral{uuid.uuid4().hex}"
    body = bytearray()
    for name, value in fields.items():
        body += f"--{boundary}\r\n".encode()
        body += f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode()
        body += value.encode("utf-8") + b"\r\n"
    body += f"--{boundary}\r\n".encode()
    body += (
        f'Content-Disposition: form-data; name="{file_field}"; '
        f'filename="{file_name}"\r\n'
    ).encode()
    body += f"Content-Type: {file_type}\r\n\r\n".encode()
    body += file_bytes + b"\r\n"
    body += f"--{boundary}--\r\n".encode()

    req = urlrequest.Request(
        f"{API_BASE}{path}",
        data=bytes(body),
        headers={
            "Authorization": f"Bearer {_api_key()}",
            "Content-Type": f"multipart/form-data; boundary={boundary}",
        },
        method="POST",
    )
    try:
        with urlrequest.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except HTTPError as e:
        raise MistralAPIError(e.code, e.read().decode("utf-8", "replace")) from e
    except URLError as e:
        raise MistralAPIError(None, str(e)) from e


def _post_audio(path: str, payload: Dict[str, Any],
                timeout: float = 120.0) -> tuple[bytes, str]:
    """POST JSON, expect binary audio back. Returns (bytes, content_type)."""
    req = urlrequest.Request(
        f"{API_BASE}{path}",
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {_api_key()}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urlrequest.urlopen(req, timeout=timeout) as resp:
            return resp.read(), resp.headers.get("Content-Type", "audio/wav")
    except HTTPError as e:
        raise MistralAPIError(e.code, e.read().decode("utf-8", "replace")) from e
    except URLError as e:
        raise MistralAPIError(None, str(e)) from e


def _ext_for(content_type: str) -> str:
    return {
        "audio/wav": "wav", "audio/x-wav": "wav", "audio/wave": "wav",
        "audio/mpeg": "mp3", "audio/mp3": "mp3",
        "audio/ogg": "ogg", "audio/flac": "flac",
        "audio/pcm": "pcm", "audio/L16": "pcm",
    }.get(content_type.split(";")[0].strip(), "bin")


class VoxtralSTT:
    """Speech-to-text via Voxtral (`POST /v1/audio/transcriptions`)."""

    def __init__(self, model: str = STT_MODEL) -> None:
        self.model = model

    def transcribe(self, audio: bytes, content_type: str = "audio/wav",
                   language: Optional[str] = None) -> Transcript:
        fields: Dict[str, str] = {"model": self.model}
        if language:
            fields["language"] = language
        data = _post_multipart(
            "/audio/transcriptions",
            fields=fields,
            file_field="file",
            file_bytes=audio,
            file_name=f"audio.{_ext_for(content_type)}",
            file_type=content_type,
        )
        return Transcript(
            text=data.get("text", ""),
            language=data.get("language", language),
            raw=data,
        )


class VoxtralTTS:
    """Text-to-speech via Voxtral TTS (`POST /v1/audio/speech`).

    `voice_id` selects a preset voice; omit it (or pass None) to use the
    API default. Zero-shot cloning voices can also be used as `voice_id`.
    """

    def __init__(self, model: str = TTS_MODEL,
                 voice_id: Optional[str] = DEFAULT_VOICE_ID) -> None:
        self.model = model
        self.voice_id = voice_id

    def synthesize(self, text: str,
                   voice_id: Optional[str] = None) -> Utterance:
        payload: Dict[str, Any] = {"model": self.model, "input": text}
        vid = voice_id if voice_id is not None else self.voice_id
        if vid:
            payload["voice_id"] = vid
        audio, content_type = _post_audio("/audio/speech", payload)
        return Utterance(
            text=text,
            audio=audio,
            content_type=content_type.split(";")[0].strip() or "audio/wav",
            voice_id=vid,
        )


def mistral_chat_reply(prompt: str, model: str = CHAT_MODEL,
                       system: Optional[str] = None,
                       history: Optional[list] = None) -> str:
    """Default responder: a Mistral chat completion. Swap in any callable."""
    messages: list = []
    if system:
        messages.append({"role": "system", "content": system})
    if history:
        messages.extend(history)
    messages.append({"role": "user", "content": prompt})
    data = _post_json(
        "/chat/completions",
        {"model": model, "messages": messages},
    )
    return data["choices"][0]["message"]["content"]
