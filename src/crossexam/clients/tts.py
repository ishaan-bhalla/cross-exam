"""Streaming TTS client.

Protocol (confirmed via scripts/probe_tts.py): connect with the API key in the
Authorization header and the voice as a query param. The server sends Begin
(with configuration.sample_rate). Each Generate + Flush streams Audio (base64
PCM) for that flush, then FlushDone and WordBoundaries for it in either order.
Word start/end times are absolute within the session, not relative to the
flush's audio_start_ms. Terminate is answered with Termination.

stream() sends one Generate + Flush per text part, so word timings for an early
part (e.g. a short lead-in) arrive before later parts finish generating.
"""
import asyncio
import base64
import json
import time
from collections.abc import AsyncIterator
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


async def stream(parts: str | list[str], voice: str | None = None) -> AsyncIterator[dict]:
    """Yields, in order received:
    {"type": "begin", "sample_rate"}
    {"type": "audio", "flush_id", "pcm": bytes}
    {"type": "words", "flush_id", "audio_start_ms", "words": [{word, start, end, confidence}]}
    {"type": "flush_done", "flush_id", "audio_duration_ms"}
    {"type": "done", "duration_ms", "time_to_first_audio_ms"}  (after every part has both
    its FlushDone and WordBoundaries; time measured from sending the first Generate)
    """
    s = get_settings()
    parts = [p.strip() for p in ([parts] if isinstance(parts, str) else parts) if p and p.strip()]
    if not parts:
        raise TTSError("nothing to synthesize")
    url = f"{s.tts_ws_url}?{urlencode({'voice': voice or s.tts_default_voice})}"
    timeout = s.tts_timeout_seconds

    async with websockets.connect(url, additional_headers={"Authorization": s.assemblyai_api_key},
                                  open_timeout=timeout) as ws:
        begin = await _recv(ws, timeout)
        if begin.get("type") != "Begin":
            raise TTSError(f"expected Begin, got {begin.get('type')!r}")
        yield {"type": "begin", "sample_rate": begin["configuration"]["sample_rate"]}

        sent_at = time.perf_counter()
        for part in parts:
            await ws.send(json.dumps({"type": "Generate", "text": part}))
            await ws.send(json.dumps({"type": "Flush"}))

        time_to_first_audio_ms = None
        flush_done: dict[int, int] = {}
        words_seen: set[int] = set()
        while len(flush_done) < len(parts) or len(words_seen) < len(parts):
            data = await _recv(ws, timeout)
            t = data.get("type")
            if t == "Audio":
                if time_to_first_audio_ms is None:
                    time_to_first_audio_ms = (time.perf_counter() - sent_at) * 1000
                yield {"type": "audio", "flush_id": data.get("flush_id"), "pcm": base64.b64decode(data["audio"])}
            elif t == "FlushDone":
                flush_done[data["flush_id"]] = data["audio_duration_ms"]
                yield {"type": "flush_done", "flush_id": data["flush_id"],
                       "audio_duration_ms": data["audio_duration_ms"]}
            elif t == "WordBoundaries":
                words_seen.add(data["flush_id"])
                yield {"type": "words", "flush_id": data["flush_id"],
                       "audio_start_ms": data.get("audio_start_ms", 0), "words": data["words"]}

        await ws.send(json.dumps({"type": "Terminate"}))
        while (await _recv(ws, timeout)).get("type") != "Termination":
            pass

    if time_to_first_audio_ms is None:
        raise TTSError("flushes completed without any audio")
    yield {"type": "done", "duration_ms": sum(flush_done.values()),
           "time_to_first_audio_ms": time_to_first_audio_ms}


async def synthesize(text: str | list[str], voice: str | None = None) -> Speech:
    audio = bytearray()
    words: list[dict] = []
    sample_rate = duration_ms = time_to_first_audio_ms = None
    async for event in stream(text, voice):
        if event["type"] == "begin":
            sample_rate = event["sample_rate"]
        elif event["type"] == "audio":
            audio.extend(event["pcm"])
        elif event["type"] == "words":
            words.extend(event["words"])
        elif event["type"] == "done":
            duration_ms, time_to_first_audio_ms = event["duration_ms"], event["time_to_first_audio_ms"]
    return Speech(bytes(audio), sample_rate, words, duration_ms, time_to_first_audio_ms)
