"""Map TTS word timings onto character spans of the quoted paragraph.

TTS words are the spoken text split on whitespace, punctuation attached ("12:",
"(P&BA)", "2006…"), with bare symbols such as "&" dropped. Words are compared by
a key (lower-cased, letters and digits only), so punctuation and quote styles
do not matter. Words that cannot be matched get span None and are simply not
highlighted.
"""
import re
from crossexam.witness.answer import quote_segments

TOKEN_RE = re.compile(r"\S+")
KEY_RE = re.compile(r"[^a-z0-9]")
LOOKAHEAD = 3  # paragraph tokens to skip when a spoken word has no exact counterpart


def _key(word: str) -> str:
    return KEY_RE.sub("", word.lower())


def _tokens(text: str) -> list[tuple[str, int, int]]:
    """(key, start, end) for each whitespace token with a non-empty key."""
    return [(k, m.start(), m.end()) for m in TOKEN_RE.finditer(text) if (k := _key(m.group()))]


def quote_spans(quote: str, paragraph: str) -> list[tuple[str, int, int]]:
    """Paragraph tokens covered by the quote's segments, in order, as (key, start, end)."""
    para = _tokens(paragraph)
    keys = [k for k, _, _ in para]
    out, pos = [], 0
    for seg in quote_segments(quote):
        seg_keys = [k for k, _, _ in _tokens(seg)]
        if not seg_keys:
            continue
        for i in range(pos, len(keys) - len(seg_keys) + 1):
            if keys[i:i + len(seg_keys)] == seg_keys:
                out.extend(para[i:i + len(seg_keys)])
                pos = i + len(seg_keys)
                break
    return out


def key_count(text: str) -> int:
    """Number of words in text that alignment considers (those with letters or digits)."""
    return len(_tokens(text))


def align_words(words: list[dict], quote: str, paragraph: str, start: int = 0,
                spans: list[tuple[str, int, int]] | None = None) -> list[dict]:
    """words: TTS words spoken for the quote, or for a chunk of it beginning at quote word
    `start` (see key_count). Returns them with "span": [start, end] or None. spans, for an
    OCR-corrected quote, gives each quote word's paragraph span (witness.ocr_match.ocr_align)
    in place of looking the quote up verbatim."""
    targets = (spans if spans is not None else quote_spans(quote, paragraph))[start:]
    j, out = 0, []
    for w in words:
        k, span = _key(w["word"]), None
        for i in range(j, min(j + LOOKAHEAD + 1, len(targets))):
            if k and targets[i][0] == k:
                span, j = [targets[i][1], targets[i][2]], i + 1
                break
        out.append({**w, "span": span})
    return out
