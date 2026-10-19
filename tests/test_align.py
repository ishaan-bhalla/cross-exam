from crossexam.witness.align import align_words

PARA_12 = ("As Post Office Ltd evolved and as teams changed, my work above led to a natural progression towards "
           "the P&BA team. I was asked to take over the role of Head of Product & Branch Accounting in June 2006, "
           "ceasing my previous role.")


def _words(*tokens):
    return [{"word": t, "start": i * 100, "end": i * 100 + 80} for i, t in enumerate(tokens)]


def _highlighted(aligned, text):
    return [text[w["span"][0]:w["span"][1]] if w["span"] else None for w in aligned]


def test_words_map_to_paragraph_tokens_ignoring_punctuation_and_dropped_ampersand():
    quote = "I was asked to take over the role of Head of Product & Branch Accounting in June 2006, ceasing my previous role."
    spoken = quote.replace("& ", "").split()  # TTS drops the bare "&"
    out = _highlighted(align_words(_words(*spoken), quote, PARA_12), PARA_12)
    assert out[0] == "I" and out[-1] == "role."
    assert "Product" in out and "Branch" in out and "2006," in out
    assert None not in out


def test_elided_quote_skips_the_gap():
    quote = "As Post Office Ltd evolved ... I was asked to take over the role"
    spoken = ["As", "Post", "Office", "Ltd", "evolved…", "I", "was", "asked", "to", "take", "over", "the", "role"]
    out = _highlighted(align_words(_words(*spoken), quote, PARA_12), PARA_12)
    assert out[:5] == ["As", "Post", "Office", "Ltd", "evolved"]
    assert out[5:] == ["I", "was", "asked", "to", "take", "over", "the", "role"]


def test_unmatched_word_gets_no_span_and_alignment_continues():
    quote = "I was asked to take over the role"
    spoken = ["I", "was", "umm", "asked", "to", "take", "over", "the", "role"]
    out = _highlighted(align_words(_words(*spoken), quote, PARA_12), PARA_12)
    assert out == ["I", "was", None, "asked", "to", "take", "over", "the", "role"]


def test_quote_not_in_paragraph_highlights_nothing():
    out = align_words(_words("Something", "else"), "Something else", PARA_12)
    assert all(w["span"] is None for w in out)
