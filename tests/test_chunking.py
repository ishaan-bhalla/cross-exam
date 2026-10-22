import numpy as np
from crossexam.clients.tts import GapLimiter, trim_trailing_silence
from crossexam.witness.chunking import chunk_quote

PARA_5 = ("I joined Post Office Ltd in September 2003 as Head of Risk and Control, in the Finance "
          "Directorate, having previously worked for Ernst & Young.")
PARA_64 = ("Regarding POL00055418 (Emails: The Horizon Trial), given that the vast majority of post offices "
           "were understood not to have significant concerns about Horizon, and because of reassurance from "
           "audit risk modelling, feedback from audit, training, support and the perspective of internal IT "
           "specialists, I did not have serious concerns about the system myself, but I would have been "
           "equally keen to be aware of new concerns arising in case this would enable me and my team to "
           "positively support the resolution of something if it became a confirmed issue.")


def _words(chunk):
    return len(chunk.split())


def test_chunks_rejoin_to_the_original_text():
    for text in (PARA_5, PARA_64):
        assert " ".join(chunk_quote(text, 12)) == " ".join(text.split())


def test_splits_at_sentence_ends():
    assert chunk_quote("I left Post Office Ltd in March 2016. I did not design the training.", 12) == [
        "I left Post Office Ltd in March 2016.", "I did not design the training."]


def test_long_sentence_splits_at_clause_breaks_within_the_limit():
    chunks = chunk_quote(PARA_64, 12)
    assert len(chunks) > 4
    assert chunks[0] == "Regarding POL00055418 (Emails: The Horizon Trial),"
    assert all(c[-1] in ",;:." for c in chunks)  # every break is at punctuation
    for c in chunks:
        words = c.split()
        # Over the limit only when there was no clause break within it.
        assert len(words) <= 12 or not any(w[-1] in ",;:" for w in words[:12])


def test_without_a_break_inside_the_limit_splits_at_the_first_break_after_it():
    # PARA_5's first clause break comes after word 14
    assert chunk_quote(PARA_5, 12) == [
        "I joined Post Office Ltd in September 2003 as Head of Risk and Control,",
        "in the Finance Directorate, having previously worked for Ernst & Young."]


def test_sentence_without_clause_breaks_stays_whole():
    s = "I cannot remember how I found working with Second Sight at the time with the passage of years"
    assert chunk_quote(s, 5) == [s]


def test_abbreviations_are_not_sentence_ends():
    text = "I spoke to Mr. Smith at Post Office Ltd. About the losses I said nothing."
    assert chunk_quote(text, 30) == [text]


def test_numbers_and_document_ids_are_never_split():
    text = "The total was £2.8 million, about 1,000 audits, see POL00055418, and FUJ00084797: the summary."
    chunks = chunk_quote(text, 3)
    joined = " | ".join(chunks)
    for token in ("£2.8", "1,000", "POL00055418,", "FUJ00084797:"):
        assert token in joined
    assert all(not c.startswith((",", ".", ":")) for c in chunks)
    assert "1, 000" not in joined and "£2. 8" not in joined


def test_elision_pause_is_a_break():
    assert chunk_quote("As Post Office Ltd evolved… I was asked to take over the role", 4) == [
        "As Post Office Ltd evolved…", "I was asked to take over the role"]


SR = 1000  # 1 sample per ms keeps the arithmetic readable


def _pcm(*runs):
    return np.concatenate([np.full(n, v, dtype=np.int16) for n, v in runs]).tobytes()


def test_gap_limiter_caps_silence_between_parts_and_reports_shifts():
    g = GapLimiter(SR, max_gaps_ms=[40, 150], tail_ms=100, threshold=500)
    out = g.feed(0, _pcm((600, 0), (200, 9000), (300, 0)))        # lead silence, speech, tail
    out += g.feed(1, _pcm((500, 0), (100, 9000), (50, 0), (100, 9000), (400, 0)))
    out += g.finish()
    s = np.frombuffer(out, dtype=np.int16)
    assert g.shift_ms(0) == 560                                   # 600 ms lead cut to 40
    assert g.shift_ms(1) == 560 + (300 + 500 - 150)               # 800 ms between parts cut to 150
    # 40 + 200 speech + 150 gap + 100 + 50 (internal pause kept) + 100 + 100 tail
    assert len(s) == 40 + 200 + 150 + 100 + 50 + 100 + 100


def test_trailing_silence_trim_keeps_a_short_tail():
    pcm = _pcm((50, 0), (200, 9000), (700, 0))
    assert len(trim_trailing_silence(pcm, SR, threshold=500, tail_ms=80)) == 2 * (50 + 200 + 80)


def test_full_stop_inside_parentheses_is_not_a_sentence_end():
    from crossexam.witness.chunking import clauses, sentences
    text = ("Regarding POL00001697 (Royal Mail Internal Information. Back Office Efficiency Programme-Stock "
            "Adjustments), and the work related to stock adjustments, my recollection is not that it was "
            "suggesting Horizon Online functionality was causing discrepancies. It was something else.")
    assert len(sentences(text)) == 2
    parts = clauses(text)
    assert parts[0] == "Regarding POL00001697 (Royal Mail Internal Information. Back Office Efficiency Programme-Stock Adjustments),"
    assert parts[-1] == "It was something else."
    # an unmatched ")" does not stop later sentence ends from counting
    assert len(sentences("See note 2). Then I left. Then I came back.")) == 3
