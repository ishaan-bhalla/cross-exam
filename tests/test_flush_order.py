import asyncio
import base64
import json
from types import SimpleNamespace
import pytest
from crossexam.api import phrases
from crossexam.clients import tts

SETTINGS = SimpleNamespace(tts_timeout_seconds=1, tts_silence_threshold=500, tts_trim_preroll_ms=0)


class FakeWS:
    def __init__(self, messages):
        self.incoming = [json.dumps(m) for m in messages]
        self.sent = []

    async def send(self, msg):
        self.sent.append(json.loads(msg))

    async def recv(self):
        return self.incoming.pop(0)


def _audio(fid, samples):
    return {"type": "Audio", "flush_id": fid, "audio": base64.b64encode(bytes(samples)).decode()}


# Flush IDs start at 1, and flush 1's word timings arrive after flush 2's audio has started.
MESSAGES = [
    _audio(1, [1, 0, 1, 0]),
    _audio(1, [2, 0]),
    {"type": "FlushDone", "flush_id": 1, "audio_duration_ms": 100},
    _audio(2, [3, 0]),
    {"type": "WordBoundaries", "flush_id": 1, "audio_start_ms": 0,
     "words": [{"word": "To", "start": 10, "end": 50}]},
    _audio(2, [4, 0]),
    {"type": "FlushDone", "flush_id": 2, "audio_duration_ms": 200},
    {"type": "WordBoundaries", "flush_id": 2, "audio_start_ms": 100,
     "words": [{"word": "I", "start": 150, "end": 170}]},
]


@pytest.fixture(autouse=True)
def fake_settings(monkeypatch):
    monkeypatch.setattr(tts, "get_settings", lambda: SETTINGS)


def _collect(session, parts):
    async def run():
        return [e async for e in session.generate(parts)]
    return asyncio.run(run())


def test_parts_follow_first_seen_flush_order_not_raw_ids():
    session = tts.Session("charles")
    session.ws = FakeWS(MESSAGES)
    events = _collect(session, ["To be clear:", "I did not."])

    assert [m["type"] for m in session.ws.sent] == ["Generate", "Flush", "Generate", "Flush"]
    by_type = lambda t: [(e["part"], e["flush_id"]) for e in events if e["type"] == t]
    assert by_type("audio") == [(0, 1), (0, 1), (1, 2), (1, 2)]
    assert by_type("words") == [(0, 1), (1, 2)]
    assert by_type("flush_done") == [(0, 1), (1, 2)]
    assert events[-1]["type"] == "done" and events[-1]["duration_ms"] == 300


def test_more_flush_ids_than_parts_is_an_error():
    session = tts.Session("charles")
    session.ws = FakeWS([_audio(5, [1, 0]), _audio(6, [1, 0])])
    with pytest.raises(tts.TTSError, match="unexpected flush_id 6"):
        _collect(session, ["only one part"])


def test_phrase_warmup_assigns_audio_and_words_by_part(monkeypatch):
    class FakeSession(tts.Session):
        async def open(self):
            self.ws, self.sample_rate = FakeWS(MESSAGES), 24000
            return self

        async def close(self):
            self.ws = None

    monkeypatch.setattr(phrases.tts, "Session", FakeSession)
    out = asyncio.run(phrases._synthesize_all("charles", ["To be clear:", "I did not."]))
    pcm0, _, words0 = out["To be clear:"]
    pcm1, _, words1 = out["I did not."]
    assert pcm0 == bytes([1, 0, 1, 0, 2, 0]) and pcm1 == bytes([3, 0, 4, 0])
    assert words0 == [{"word": "To", "start": 10, "end": 50}]
    assert words1 == [{"word": "I", "start": 50, "end": 70}]  # relative to the phrase's own audio
