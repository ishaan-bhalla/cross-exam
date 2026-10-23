"""OCR-tolerant quote verification, a second pass used only after exact verification fails.

The model sometimes quotes a scanned statement with its OCR errors corrected ("I am a" for
the text layer's "lama", "2nd" for '2"'), so the quote is not a verbatim extract. This pass
aligns the quote's words to a contiguous run of the paragraph's words, segment by segment
(segments split at "…" as for exact matching), and accepts only if:

- every quote word and every paragraph word in the run is used: no inserted or missing words;
- each aligned group matches after normalisation (letters and digits only, lower case, an
  ordinal suffix after digits dropped, since OCR reads superscript "nd" as junk) within a
  small edit distance: ocr_verify_max_edit_ratio of the word's length, at most
  ocr_verify_max_edits. Substituting OCR-confusable characters (ocr_verify_confusable,
  e.g. "l" for "i") is free;
- edits other than confusable substitutions are allowed only on paragraph words that show
  OCR damage (stray symbols, or letters and digits mixed as in a damaged document ID), never
  touch a digit, and so cannot turn a clean word into a different one ("awake" for "aware");
- a group may split or merge words ("lama" <-> "I am a", up to ocr_verify_max_merge words)
  only if the joined letters match with confusable substitutions alone, so a merge cannot
  hide an inserted word ("no evidence" <-> "evidence");
- at least ocr_verify_min_exact_fraction of the quote's words match exactly.

The result is one (key, start, end) per quote word, as witness.align.quote_spans returns, so
spoken words can be highlighted on the original text.
"""
import re
from crossexam.config import get_settings

TOKEN_RE = re.compile(r"\S+")
KEY_RE = re.compile(r"[^a-z0-9]")
ORDINAL_RE = re.compile(r"^(\d+)(st|nd|rd|th)$")
CLEAN_RE = re.compile(r"^[A-Za-z0-9'’‘\"“”.,;:!?()\[\]/&%£$…–—\-]*$")
ELLIPSIS_RE = re.compile(r"\.\.\.|…")
INF = float("inf")


def _key(word: str) -> str:
    return KEY_RE.sub("", word.lower())


def _tokens(text: str) -> list[tuple[str, int, int, str]]:
    """(key, start, end, raw) for each whitespace token with a non-empty key."""
    return [(k, m.start(), m.end(), m.group()) for m in TOKEN_RE.finditer(text) if (k := _key(m.group()))]


def _compare_key(key: str) -> str:
    m = ORDINAL_RE.match(key)
    return m.group(1) if m else key


def _damaged(raw: str) -> bool:
    """The paragraph word shows OCR damage: stray symbols, or letters and digits mixed."""
    key = _key(raw)
    return not CLEAN_RE.match(raw) or (re.search(r"[a-z]", key) is not None and re.search(r"\d", key) is not None)


class _Rules:
    def __init__(self, s):
        self.ratio, self.max_edits = s.ocr_verify_max_edit_ratio, s.ocr_verify_max_edits
        self.max_merge, self.min_exact = s.ocr_verify_max_merge, s.ocr_verify_min_exact_fraction
        self.confusable = {}
        for group in s.ocr_verify_confusable.split(","):
            for c in group.strip():
                self.confusable[c] = set(group.strip())

    def same(self, a: str, b: str) -> bool:
        return a == b or b in self.confusable.get(a, ())

    def distance(self, a: str, b: str, free_only: bool) -> float:
        """Edit distance with confusable substitutions free; edits involving a digit, or
        any paid edit when free_only, are not allowed (INF)."""
        prev = [0.0] + [INF] * len(b)
        for j in range(1, len(b) + 1):
            prev[j] = INF if free_only or b[j - 1].isdigit() else prev[j - 1] + 1
        for i in range(1, len(a) + 1):
            cur = [INF if free_only or a[i - 1].isdigit() else prev[0] + 1] + [INF] * len(b)
            for j in range(1, len(b) + 1):
                x, y = a[i - 1], b[j - 1]
                sub = prev[j - 1] if self.same(x, y) else (
                    INF if free_only or x.isdigit() or y.isdigit() else prev[j - 1] + 1)
                dele = INF if free_only or x.isdigit() else prev[j] + 1
                ins = INF if free_only or y.isdigit() else cur[j - 1] + 1
                cur[j] = min(sub, dele, ins)
            prev = cur
        return prev[len(b)]

    def group_ok(self, q: list[str], p: list[tuple]) -> tuple[bool, bool]:
        """(accepted, exact) for quote keys q aligned to paragraph tokens p."""
        a = "".join(_compare_key(k) for k in q)
        b = "".join(_compare_key(t[0]) for t in p)
        if len(q) == 1 and len(p) == 1 and a == b:
            return True, True
        if len(q) > 1 or len(p) > 1:  # split or merged words: spacing only
            return self.distance(a, b, free_only=True) == 0, False
        budget = min(self.max_edits, int(len(a) * self.ratio))
        free_only = not _damaged(p[0][3])
        return self.distance(a, b, free_only) <= (0 if free_only else budget), False


def _align_segment(q: list[str], para: list[tuple], start: int, rules: _Rules):
    """Best alignment of all of q to para[start:end] for some end, as (groups, exact count),
    or None. A group is (quote index range, paragraph index range)."""
    n, m = len(q), len(para)
    best = {(0, start): (0, None)}  # (i, j) -> (exact count, back pointer)
    for i in range(n):
        for j in range(start, m):
            if (i, j) not in best:
                continue
            exact_so_far = best[(i, j)][0]
            for a in range(1, rules.max_merge + 1):
                for b in range(1, rules.max_merge + 1):
                    if (a > 1 and b > 1) or i + a > n or j + b > m:
                        continue
                    ok, exact = rules.group_ok(q[i:i + a], para[j:j + b])
                    if ok:
                        score = exact_so_far + (a if exact else 0)
                        if score > best.get((i + a, j + b), (-1, None))[0]:
                            best[(i + a, j + b)] = (score, (i, j, a, b))
    ends = [(v[0], j) for (i, j), v in best.items() if i == n]
    if not ends:
        return None
    score, j = max(ends, key=lambda e: (e[0], -e[1]))
    groups, i = [], n
    while (i, j) != (0, start):
        pi, pj, a, b = best[(i, j)][1]
        groups.append(((pi, pi + a), (pj, pj + b)))
        i, j = pi, pj
    return groups[::-1], score


def ocr_align(quote: str, text: str) -> list[tuple[str, int, int]] | None:
    """One (quote word key, start, end) per quote word, mapping it to the paragraph span it
    stands for, if the quote matches text under the rules above; else None."""
    rules = _Rules(get_settings())
    para = _tokens(text)
    segments = [s.strip().strip("\"'‘’“”") for s in ELLIPSIS_RE.split(quote.strip().strip("\"'‘’“”"))]
    out, pos, exact, total = [], 0, 0, 0
    for seg in segments:
        q = [t[0] for t in _tokens(seg)]
        if not q:
            continue
        found = None
        for start in range(pos, len(para)):
            found = _align_segment(q, para, start, rules)
            if found:
                break
        if not found:
            return None
        groups, seg_exact = found
        for (qa, qb), (pa, pb) in groups:
            span_start, span_end = para[pa][1], para[pb - 1][2]
            out.extend((q[k], span_start, span_end) for k in range(qa, qb))
        pos = groups[-1][1][1]
        exact, total = exact + seg_exact, total + len(q)
    if not total or exact / total < rules.min_exact:
        return None
    return out
