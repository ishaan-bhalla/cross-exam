"""Pre-synthesised fixed phrases (lead-ins and refusals), per voice.

Phrases are synthesised in a single TTS session, one flush per phrase: the account
allows only about 5 session opens per 30 s, so one session per phrase would hit the
limit at startup. Each phrase's leading silence is trimmed (word timings shifted
to match) and its trailing silence cut to tts_trim_tail_ms, then it is kept in memory. If phrase_cache_dir is set, the
untrimmed audio and word timings are also stored there, keyed by voice and a hash
of the text, so restarts skip synthesis and changing the trim settings needs no purge.
"""
import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from crossexam.clients import tts
from crossexam.config import get_settings


@dataclass
class Phrase:
    text: str
    pcm: bytes
    sample_rate: int
    words: list[dict]  # times in ms from the start of the trimmed audio
    duration_ms: float


_cache: dict[tuple[str, str], Phrase] = {}


def _disk_paths(voice: str, text: str) -> tuple[Path, Path] | None:
    s = get_settings()
    if not s.phrase_cache_dir:
        return None
    base = s.resolve(Path(s.phrase_cache_dir)) / f"{voice}-{hashlib.sha256(text.encode()).hexdigest()[:16]}"
    return base.with_suffix(".pcm"), base.with_suffix(".json")


def _read_disk(voice: str, text: str) -> tuple[bytes, int, list[dict]] | None:
    paths = _disk_paths(voice, text)
    if not (paths and paths[0].exists() and paths[1].exists()):
        return None
    meta = json.loads(paths[1].read_text())
    return (paths[0].read_bytes(), meta["sample_rate"], meta["words"]) if meta["text"] == text else None


def _write_disk(voice: str, text: str, pcm: bytes, sample_rate: int, words: list[dict]):
    paths = _disk_paths(voice, text)
    if paths:
        paths[0].parent.mkdir(parents=True, exist_ok=True)
        paths[0].write_bytes(pcm)
        paths[1].write_text(json.dumps({"text": text, "voice": voice, "sample_rate": sample_rate, "words": words}))


async def _synthesize_all(voice: str, texts: list[str]) -> dict[str, tuple[bytes, int, list[dict]]]:
    """{text: (pcm, sample_rate, words relative to the phrase's own audio)} from one session."""
    pcm = {i: bytearray() for i in range(len(texts))}
    words, starts = {}, {}
    async with tts.Session(voice) as session:
        async for event in session.generate(texts):
            if event["type"] == "audio":
                pcm[event["part"]].extend(event["pcm"])
            elif event["type"] == "words":
                words[event["part"]] = event["words"]
                starts[event["part"]] = event["audio_start_ms"]
        sample_rate = session.sample_rate
    # Word times are absolute within the session; make them relative to each phrase.
    return {text: (bytes(pcm[i]), sample_rate,
                   [{**w, "start": w["start"] - starts[i], "end": w["end"] - starts[i]} for w in words[i]])
            for i, text in enumerate(texts)}


async def warm(voice: str, texts: list[str]) -> dict[str, int]:
    """Make every text available for voice. Returns counts by source."""
    counts = {"memory": 0, "disk": 0, "tts": 0}
    raw = {}
    for text in texts:
        if (voice, text) in _cache:
            counts["memory"] += 1
        elif (hit := _read_disk(voice, text)) is not None:
            raw[text] = hit
            counts["disk"] += 1
    missing = [t for t in texts if (voice, t) not in _cache and t not in raw]
    if missing:
        for text, (pcm, sample_rate, words) in (await _synthesize_all(voice, missing)).items():
            _write_disk(voice, text, pcm, sample_rate, words)
            raw[text] = (pcm, sample_rate, words)
            counts["tts"] += 1
    for text, (pcm, sample_rate, words) in raw.items():
        trimmed, trimmed_ms = tts.trim_leading_silence(pcm, sample_rate)
        trimmed = tts.trim_trailing_silence(trimmed, sample_rate)
        shifted = [{**w, "start": max(0, round(w["start"] - trimmed_ms)), "end": max(0, round(w["end"] - trimmed_ms))}
                   for w in words]
        _cache[(voice, text)] = Phrase(text, trimmed, sample_rate, shifted, len(trimmed) / 2 / sample_rate * 1000)
    return counts


def get(voice: str, text: str) -> Phrase | None:
    return _cache.get((voice, text))
