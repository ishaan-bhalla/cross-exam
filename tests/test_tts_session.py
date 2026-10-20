import asyncio
import base64
import json
from types import SimpleNamespace
import pytest
from websockets.protocol import State
from crossexam.clients import tts

SETTINGS = SimpleNamespace(tts_timeout_seconds=1, tts_keepalive_seconds=3600, tts_words_wait_seconds=0.05)


class FakeWS:
    def __init__(self, messages, fail_after=None):
        self.incoming = [json.dumps(m) for m in messages]
        self.sent, self.state, self.fail_after = [], State.OPEN, fail_after

    async def send(self, msg):
        self.sent.append(json.loads(msg))

    async def recv(self):
        if self.fail_after is not None and len(self.incoming) <= self.fail_after:
            self.state = State.CLOSED
            raise ConnectionResetError("dropped")
        if not self.incoming:
            await asyncio.sleep(3600)  # a quiet server: nothing more is sent
        return self.incoming.pop(0)

    async def close(self):
        self.state = State.CLOSED


def _flush(fid, start_ms, duration_ms, word_at):
    return [
        {"type": "Audio", "flush_id": fid, "audio": base64.b64encode(bytes(4)).decode()},
        {"type": "FlushDone", "flush_id": fid, "audio_duration_ms": duration_ms},
        {"type": "WordBoundaries", "flush_id": fid, "audio_start_ms": start_ms,
         "words": [{"word": "w", "start": word_at, "end": word_at + 50}]},
    ]


@pytest.fixture(autouse=True)
def fake_settings(monkeypatch):
    monkeypatch.setattr(tts, "get_settings", lambda: SETTINGS)


async def _collect(gen):
    return [e async for e in gen]


def test_reused_session_reports_times_relative_to_each_call():
    session = tts.Session("charles")
    # Server times are absolute within the session: the second answer starts at 1520 ms.
    session.ws = FakeWS(_flush(0, 0, 1520, 624) + _flush(1, 1520, 3000, 2135))
    first = asyncio.run(_collect(session.generate("To be clear:")))
    second = asyncio.run(_collect(session.generate("I did not.")))
    w1 = next(e for e in first if e["type"] == "words")
    w2 = next(e for e in second if e["type"] == "words")
    assert w1["words"][0]["start"] == 624 and w1["audio_start_ms"] == 0
    assert w2["words"][0]["start"] == 2135 - 1520 and w2["audio_start_ms"] == 0
    assert w2["part"] == 0  # each call numbers its own parts
    assert session.position_ms == 4520 and session.clean


def _fake_session_class(scripts):
    """Each open() takes the next FakeWS from scripts."""
    class FakeSession(tts.Session):
        async def open(self):
            self.ws, self.sample_rate = scripts.pop(0), 24000
            return self
    return FakeSession


def test_persistent_session_reconnects_when_generate_fails_before_any_output(monkeypatch):
    dropped = FakeWS(_flush(0, 0, 1000, 100), fail_after=3)  # fails on the first recv
    fresh = FakeWS(_flush(0, 0, 1000, 100))
    monkeypatch.setattr(tts, "Session", _fake_session_class([dropped, fresh]))

    async def run():
        conn = tts.PersistentSession("charles")
        events = await _collect(conn.generate("To be clear:"))
        await conn.close()
        return conn, events
    conn, events = asyncio.run(run())
    assert conn.opens == 2
    assert [e["type"] for e in events] == ["audio", "flush_done", "words", "done"]


def test_persistent_session_reuses_one_session_and_replaces_it_after_a_mid_answer_drop(monkeypatch):
    first = FakeWS(_flush(0, 0, 1000, 100) + _flush(1, 1000, 1000, 1100) + _flush(2, 2000, 1000, 2100),
                   fail_after=4)  # 2nd answer's audio arrives, then the connection drops
    second = FakeWS(_flush(0, 0, 1000, 100))
    monkeypatch.setattr(tts, "Session", _fake_session_class([first, second]))

    async def run():
        conn = tts.PersistentSession("charles")
        await _collect(conn.generate("one"))
        with pytest.raises(ConnectionResetError):
            await _collect(conn.generate("two"))  # failed after yielding audio: not retried
        events = await _collect(conn.generate("three"))
        await conn.close()
        return conn, events
    conn, events = asyncio.run(run())
    assert conn.opens == 2  # answers one and two shared a session; three got a fresh one
    assert events[-1]["type"] == "done"


def test_missing_word_timings_finish_after_the_short_wait_and_late_ones_are_ignored():
    session = tts.Session("charles")
    no_words = [m for m in _flush(0, 0, 32000, 100) if m["type"] != "WordBoundaries"]
    late_words = [m for m in _flush(0, 0, 32000, 100) if m["type"] == "WordBoundaries"]
    session.ws = FakeWS(no_words)
    first = asyncio.run(_collect(session.generate("a very long quote")))
    assert first[-1]["type"] == "done" and first[-1]["missing_words"] == [0]
    assert session.clean
    # The old flush's WordBoundaries turn up during the next answer: ignored, not part 0.
    session.ws.incoming = [json.dumps(m) for m in late_words + _flush(1, 32000, 1000, 32100)]
    second = asyncio.run(_collect(session.generate("next answer")))
    words = [e for e in second if e["type"] == "words"]
    assert len(words) == 1 and words[0]["flush_id"] == 1 and words[0]["words"][0]["start"] == 100
    assert second[-1]["missing_words"] == []
