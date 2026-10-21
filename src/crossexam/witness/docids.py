"""Normalise spoken document IDs in a transcript, then snap them to IDs the statement cites.

Speech-to-text renders "POL00055418" in many ways: "POL 00055418", "P O L 000 55418",
"POLOO055418", "POL double oh oh 55418", "P O L zero zero zero five five four one eight".
normalise_ids() finds spans that start with a known ID prefix (the letters of the IDs the
statement cites, e.g. POL, FUJ, NFSP) and rebuilds the ID:
- letters spoken separately are joined ("P O L" -> "POL");
- the numeric part may be split across words, and may use digit words ("zero", "oh",
  "one" ... "nine", with "double"/"triple" repeating the next digit);
- inside the numeric part, the letter O reads as 0 and I or l as 1.
The rebuilt ID is snapped to a cited ID only if exactly one cited ID is within
max_distance edits; otherwise it is left as heard, so routing reports it as not recognised.
"""
import re

DIGIT_WORDS = {"zero": "0", "oh": "0", "o": "0", "nought": "0", "one": "1", "two": "2", "three": "3",
               "four": "4", "five": "5", "six": "6", "seven": "7", "eight": "8", "nine": "9"}
REPEAT_WORDS = {"double": 2, "triple": 3}
NUMERIC_LOOKALIKES = str.maketrans({"O": "0", "o": "0", "I": "1", "l": "1"})
TOKEN_RE = re.compile(r"\S+")
EDGE_PUNCT = ".,;:?!\"'()"
NUMERIC_CHUNK_RE = re.compile(r"^[0-9OoIl]+$")


def _edit_distance(a: str, b: str) -> int:
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]


def snap(candidate: str, known: list[str], max_distance: int) -> str | None:
    """The unique known ID within max_distance of candidate, else None."""
    if candidate in known:
        return candidate
    close = [k for k in known if _edit_distance(candidate, k) <= max_distance]
    return close[0] if len(close) == 1 else None


def _numeric(tokens: list[str], i: int) -> tuple[str, int]:
    """Digits spoken from tokens[i:], and the index after the last token used."""
    digits, j = "", i
    while j < len(tokens):
        word = tokens[j].strip(EDGE_PUNCT)
        low = word.lower()
        if low in REPEAT_WORDS and j + 1 < len(tokens) and tokens[j + 1].strip(EDGE_PUNCT).lower() in DIGIT_WORDS:
            digits += DIGIT_WORDS[tokens[j + 1].strip(EDGE_PUNCT).lower()] * REPEAT_WORDS[low]
            j += 2
        elif low in DIGIT_WORDS:
            digits += DIGIT_WORDS[low]
            j += 1
        elif word and NUMERIC_CHUNK_RE.match(word) and any(c.isdigit() for c in word) or word in ("OO", "OOO"):
            digits += word.translate(NUMERIC_LOOKALIKES)
            j += 1
        else:
            break
        if tokens[j - 1][-1:] in ".,;:?!":  # punctuation ends the ID
            break
    return digits, j


def _prefix(tokens: list[str], i: int, prefixes: set[str]) -> tuple[str, int, str] | None:
    """(prefix, index after it, rest of the token glued to it) if tokens[i:] starts with one."""
    word = tokens[i].strip(EDGE_PUNCT)
    for p in sorted(prefixes, key=len, reverse=True):
        if word.upper().startswith(p) and (len(word) == len(p) or NUMERIC_CHUNK_RE.match(word[len(p):])):
            if word[:len(p)].isupper():
                return p, i + 1, word[len(p):]
    letters, j = "", i
    while j < len(tokens) and len(tokens[j].strip(EDGE_PUNCT)) == 1 and tokens[j].strip(EDGE_PUNCT).isalpha():
        letters += tokens[j].strip(EDGE_PUNCT).upper()
        j += 1
        if letters in prefixes:
            return letters, j, ""
    return None


def normalise_ids(text: str, known: list[str], max_distance: int) -> tuple[str, list[dict]]:
    """(text with ID-like spans rewritten, [{"heard", "id", "snapped"}]) for each span found."""
    prefixes = {re.match(r"[A-Z]+", k).group() for k in known if re.match(r"[A-Z]+", k)}
    if not prefixes:
        return text, []
    matches = list(TOKEN_RE.finditer(text))
    tokens = [m.group() for m in matches]
    out, changes, i, last_end = [], [], 0, 0
    while i < len(tokens):
        found = _prefix(tokens, i, prefixes)
        if found:
            prefix, j, glued = found
            digits = glued.translate(NUMERIC_LOOKALIKES)
            more, k = _numeric(tokens, j) if not tokens[j - 1][-1:] in ".,;:?!" else ("", j)
            digits += more
            if digits.isdigit() and len(digits) >= 4:
                candidate = prefix + digits
                ident = snap(candidate, known, max_distance) or candidate
                start, end = matches[i].start(), matches[k - 1].end()
                heard = text[start:end]
                trailing = heard[len(heard.rstrip(EDGE_PUNCT)):]
                out.append(text[last_end:start] + ident + trailing)
                last_end = end
                if heard.rstrip(EDGE_PUNCT) != ident:
                    # snapped: the rebuilt ID was corrected to a different, cited ID
                    changes.append({"heard": heard.rstrip(EDGE_PUNCT), "id": ident, "snapped": ident != candidate})
                i = k
                continue
        i += 1
    out.append(text[last_end:])
    return "".join(out), changes
