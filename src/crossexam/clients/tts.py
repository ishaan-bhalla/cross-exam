"""Streaming TTS client.

Protocol (confirmed via scripts/probe_tts.py): connect with the API key in the
Authorization header and the voice as a query param. The server sends Begin
(with configuration.sample_rate). After Generate + Flush it streams Audio
(base64 PCM), then FlushDone and WordBoundaries for that flush in either order.
Terminate is answered with Termination.
"""
import asyncio
import base64
import json
import time
from dataclasses import dataclass
from urllib.parse import urlencode
import websockets
from crossexam.config import get_settings


class TTSError(RuntimeError):
    pass


@dataclass
class Speech:
    audio: bytes
    sample_rate: int
    words: list[dict]
    duration_ms: int
    time_to_first_audio_ms: float


async def _recv(ws, timeout: float) -> dict:
    msg = await asyncio.wait_for(ws.recv(), timeout=timeout)
    data = json.loads(msg)
    if data.get("type") == "Error":
        raise TTSError(f"TTS error {data.get('error_code')}: {data.get('error')}")
    return data


async def synthesize(text: str, voice: str | None = None) -> Speech:
    s = get_settings()
    url = f"{s.tts_ws_url}?{urlencode({'voice': voice or s.tts_default_voice})}"
    timeout = s.tts_timeout_seconds

    async with websockets.connect(url, additional_headers={"Authorization": s.assemblyai_api_key},
                                  open_timeout=timeout) as ws:
        begin = await _recv(ws, timeout)
        if begin.get("type") != "Begin":
            raise TTSError(f"expected Begin, got {begin.get('type')!r}")
        sample_rate = begin["configuration"]["sample_rate"]

        await ws.send(json.dumps({"type": "Generate", "text": text}))
        sent_at = time.perf_counter()
        await ws.send(json.dumps({"type": "Flush"}))

        audio = bytearray()
        words: list[dict] = []
        time_to_first_audio_ms = None
        duration_ms = None
        got_words = False
        while duration_ms is None or not got_words:
            data = await _recv(ws, timeout)
            t = data.get("type")
            if t == "Audio":
                if time_to_first_audio_ms is None:
                    time_to_first_audio_ms = (time.perf_counter() - sent_at) * 1000
                audio.extend(base64.b64decode(data["audio"]))
            elif t == "FlushDone":
                duration_ms = data["audio_duration_ms"]
            elif t == "WordBoundaries":
                words.extend(data["words"])
                got_words = True

        await ws.send(json.dumps({"type": "Terminate"}))
        while (await _recv(ws, timeout)).get("type") != "Termination":
            pass

    if time_to_first_audio_ms is None:
        raise TTSError("flush completed without any audio")
    return Speech(bytes(audio), sample_rate, words, duration_ms, time_to_first_audio_ms)
