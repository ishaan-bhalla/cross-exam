"""Streaming TTS client.

Protocol (confirmed via scripts/probe_tts.py): connect with the API key in the
Authorization header and the voice as a query param. The server sends Begin
(with configuration.sample_rate). Each Generate + Flush streams Audio (base64
PCM16) for that flush, then FlushDone and WordBoundaries for it in either order.
Word start/end times are absolute within the session, not relative to the
flush's audio_start_ms. Terminate is answered with Termination. Opening a
session while the account is at its concurrent-session limit fails with error
3009; Session.open() retries that.

Each flush's audio starts with several hundred ms of silence. SilenceTrimmer
removes it from a stream as it arrives.
"""
import asyncio
import base64
import json
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass
from urllib.parse import urlencode
import numpy as np
import websockets
from crossexam.config import get_settings

SESSION_LIMIT_ERROR = 3009


class TTSError(RuntimeError):
    def __init__(self, message: str, code: int | None = None):
        super().__init__(message)
        self.code = code


@dataclass
class Speech:
    audio: bytes
    sample_rate: int
    words: list[dict]
    duration_ms: int
    time_to_first_audio_ms: float


class SilenceTrimmer:
    """Drops leading silence from a PCM16 mono stream fed in chunks.

    Audio is held back until the first sample louder than tts_silence_threshold, then
    released from tts_trim_preroll_ms before it. trimmed_ms is how much was dropped,
    which callers subtract from word timings.
    """

    def __init__(self, sample_rate: int, threshold: int | None = None, preroll_ms: int | None = None):
        if threshold is None or preroll_ms is None:
            s = get_settings()
            threshold = s.tts_silence_threshold if threshold is None else threshold
            preroll_ms = s.tts_trim_preroll_ms if preroll_ms is None else preroll_ms
        self.sample_rate = sample_rate
        self.threshold = threshold
        self.preroll = int(sample_rate * preroll_ms / 1000)
        self.pending = np.zeros(0, dtype=np.int16)
        self.started = False
        self.trimmed_samples = 0

    @property
    def trimmed_ms(self) -> float:
        return self.trimmed_samples * 1000 / self.sample_rate

    def feed(self, pcm: bytes) -> bytes:
        if self.started:
            return pcm
        self.pending = np.concatenate([self.pending, np.frombuffer(pcm, dtype=np.int16)])
        loud = np.flatnonzero(np.abs(self.pending.astype(np.int32)) > self.threshold)
        if not len(loud):
            # Keep only the pre-roll's worth of silence; everything older is trimmed.
            drop = max(0, len(self.pending) - self.preroll)
            self.trimmed_samples += drop
            self.pending = self.pending[drop:]
            return b""
        start = max(0, loud[0] - self.preroll)
        self.trimmed_samples += start
        out, self.pending, self.started = self.pending[start:].tobytes(), None, True
        return out


def trim_leading_silence(pcm: bytes, sample_rate: int) -> tuple[bytes, float]:
    """(trimmed pcm, ms removed) for a complete clip."""
    t = SilenceTrimmer(sample_rate)
    return t.feed(pcm), t.trimmed_ms


class FlushOrder:
    """Maps server flush IDs to part indexes by order of first appearance."""

    def __init__(self, n_parts: int):
        self.n_parts = n_parts
        self.parts: dict[int, int] = {}
        self.last: int | None = None

    def part(self, flush_id: int | None) -> int:
        if flush_id is None:  # an Audio message without a flush_id belongs to the current part
            if self.last is None:
                raise TTSError("audio received before any flush was identified")
            return self.last
        if flush_id not in self.parts:
            if len(self.parts) == self.n_parts:
                raise TTSError(f"unexpected flush_id {flush_id}: all {self.n_parts} parts already seen")
            self.parts[flush_id] = len(self.parts)
        self.last = self.parts[flush_id]
        return self.last


async def _recv(ws, timeout: float) -> dict:
    msg = await asyncio.wait_for(ws.recv(), timeout=timeout)
    data = json.loads(msg)
    if data.get("type") == "Error":
        raise TTSError(f"TTS error {data.get('error_code')}: {data.get('error')}", data.get("error_code"))
    return data


class Session:
    """One TTS WebSocket session. open() can be started early (e.g. while an LLM call
    runs) and generate() used once the text is known; close() always terminates."""

    def __init__(self, voice: str | None = None):
        self.voice = voice or get_settings().tts_default_voice
        self.ws = None
        self.sample_rate: int | None = None

    async def open(self) -> "Session":
        s = get_settings()
        url = f"{s.tts_ws_url}?{urlencode({'voice': self.voice})}"
        for attempt in range(s.tts_session_retries + 1):
            ws = await websockets.connect(url, additional_headers={"Authorization": s.assemblyai_api_key},
                                          open_timeout=s.tts_timeout_seconds)
            try:
                begin = await _recv(ws, s.tts_timeout_seconds)
            except TTSError as e:
                await ws.close()
                if e.code != SESSION_LIMIT_ERROR or attempt == s.tts_session_retries:
                    raise
                await asyncio.sleep(s.tts_session_retry_backoff_seconds * 2 ** attempt)
                continue
            if begin.get("type") != "Begin":
                await ws.close()
                raise TTSError(f"expected Begin, got {begin.get('type')!r}")
            self.ws, self.sample_rate = ws, begin["configuration"]["sample_rate"]
            return self

    async def generate(self, parts: str | list[str]) -> AsyncIterator[dict]:
        """Yields, in order received:
        {"type": "audio", "part", "flush_id", "pcm": bytes}
        {"type": "words", "part", "flush_id", "audio_start_ms", "words": [{word, start, end, confidence}]}
        {"type": "flush_done", "part", "flush_id", "audio_duration_ms"}
        {"type": "done", "duration_ms", "time_to_first_audio_ms"}  (after every part has both its
        FlushDone and WordBoundaries; time measured from sending the first Generate)

        "part" is the index into parts. The server's flush IDs are not assumed to start at 0
        (or to be consecutive): parts are flushed in order, so each new flush_id is mapped to
        the next part in the order it is first seen.
        """
        timeout = get_settings().tts_timeout_seconds
        parts = [p.strip() for p in ([parts] if isinstance(parts, str) else parts) if p and p.strip()]
        if not parts:
            raise TTSError("nothing to synthesize")
        sent_at = time.perf_counter()
        for part in parts:
            await self.ws.send(json.dumps({"type": "Generate", "text": part}))
            await self.ws.send(json.dumps({"type": "Flush"}))

        order = FlushOrder(len(parts))
        time_to_first_audio_ms = None
        flush_done: dict[int, int] = {}
        words_seen: set[int] = set()
        while len(flush_done) < len(parts) or len(words_seen) < len(parts):
            data = await _recv(self.ws, timeout)
            t = data.get("type")
            if t == "Audio":
                if time_to_first_audio_ms is None:
                    time_to_first_audio_ms = (time.perf_counter() - sent_at) * 1000
                fid = data.get("flush_id")
                yield {"type": "audio", "part": order.part(fid), "flush_id": fid,
                       "pcm": base64.b64decode(data["audio"])}
            elif t == "FlushDone":
                flush_done[data["flush_id"]] = data["audio_duration_ms"]
                yield {"type": "flush_done", "part": order.part(data["flush_id"]), "flush_id": data["flush_id"],
                       "audio_duration_ms": data["audio_duration_ms"]}
            elif t == "WordBoundaries":
                words_seen.add(data["flush_id"])
                yield {"type": "words", "part": order.part(data["flush_id"]), "flush_id": data["flush_id"],
                       "audio_start_ms": data.get("audio_start_ms", 0), "words": data["words"]}
        if time_to_first_audio_ms is None:
            raise TTSError("flushes completed without any audio")
        yield {"type": "done", "duration_ms": sum(flush_done.values()),
               "time_to_first_audio_ms": time_to_first_audio_ms}

    async def close(self):
        if self.ws is None:
            return
        ws, self.ws = self.ws, None
        try:
            await ws.send(json.dumps({"type": "Terminate"}))
            timeout = get_settings().tts_timeout_seconds
            while (await _recv(ws, timeout)).get("type") != "Termination":
                pass
        except Exception:
            pass  # the session is being abandoned either way
        finally:
            await ws.close()

    async def __aenter__(self):
        return await self.open()

    async def __aexit__(self, *exc):
        await self.close()


async def stream(parts: str | list[str], voice: str | None = None) -> AsyncIterator[dict]:
    """A whole session: {"type": "begin", "sample_rate"} followed by Session.generate()'s events."""
    async with Session(voice) as session:
        yield {"type": "begin", "sample_rate": session.sample_rate}
        async for event in session.generate(parts):
            yield event


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
