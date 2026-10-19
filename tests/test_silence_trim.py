import numpy as np
from crossexam.clients.tts import SilenceTrimmer

SR = 24000


def _pcm(samples):
    return np.asarray(samples, dtype=np.int16).tobytes()


def _chunks(pcm: bytes, size: int):
    return [pcm[i:i + size] for i in range(0, len(pcm), size)]


def test_trims_silence_across_chunks_and_keeps_preroll():
    silence = np.zeros(int(SR * 0.7), dtype=np.int16)            # 700 ms
    noise = (np.random.default_rng(0).integers(-50, 50, int(SR * 0.1))).astype(np.int16)  # quiet, 100 ms
    speech = np.full(int(SR * 0.3), 8000, dtype=np.int16)        # loud, 300 ms
    pcm = _pcm(np.concatenate([silence, noise, speech]))
    t = SilenceTrimmer(SR, threshold=500, preroll_ms=40)
    out = b"".join(t.feed(c) for c in _chunks(pcm, 3840))
    assert abs(t.trimmed_ms - 760) < 1                             # 800 ms of quiet minus 40 ms pre-roll
    kept = np.frombuffer(out, dtype=np.int16)
    assert len(kept) == len(np.frombuffer(pcm, dtype=np.int16)) - t.trimmed_samples
    assert np.all(np.abs(kept[:int(SR * 0.04)]) <= 500)           # the pre-roll is the quiet lead-up
    assert kept[int(SR * 0.04)] == 8000                           # then the onset, unclipped


def test_passes_audio_through_unchanged_after_onset():
    t = SilenceTrimmer(SR, threshold=500, preroll_ms=0)
    assert t.feed(_pcm([0, 0, 900, 1])) == _pcm([900, 1])
    later = _pcm([0, 0, 0, 0])
    assert t.feed(later) == later                                 # silence after speech is kept


def test_all_silence_yields_nothing():
    t = SilenceTrimmer(SR, threshold=500, preroll_ms=40)
    assert t.feed(_pcm(np.zeros(SR, dtype=np.int16))) == b""
