"""Parse a witness statement PDF into numbered units (paragraphs and sub-paragraphs).

Nothing here is specific to one witness or statement. The steps:

1. Page text. Each page's text layer is used (in layout mode where paragraph numbers sit
   in a column of their own, so they stay on the line they number) unless it is short (ocr_min_chars) or
   garbled (at least ocr_max_junk_ratio of its tokens are junk: stray symbols, digits inside
   words, capitals mid-word; document IDs excepted). Such pages are rendered and re-read
   with Tesseract, and the OCR text is kept only if it has less junk. On clean pages the
   junk share is typically under 0.02; on badly scanned ones 0.05 and up. Only pages up
   to the statement of truth are considered, since exhibit indexes after it are all IDs.
2. Page furniture. Lines that repeat within parser_edge_lines of the top or bottom of many pages (running headers
   such as the statement ID, footers) and page numbers ("Page 3 of 24", "3") are removed.
   Repetition is detected, not pattern-matched, so other layouts work too.
3. Numbering. Units start at a line beginning with a number, accepted only in sequence
   (a jump is accepted if the next two numbers continue from it; numbers then still
   missing are looked for by re-reading the pages around the gap with OCR):
   - top-level paragraphs "12." or "12" (some statements omit the full stop);
   - dotted sub-paragraphs "12.1", "12.1.", "2.6.1", which become units "12.1" etc.;
   - lettered or roman items "(a)", "b)", "(iv)" within a unit, which become "12(a)" etc.
   A top-level number may skip ahead by up to parser_max_para_gap when it ends with a full
   stop (OCR sometimes loses a number). A number skipped by exactly one, where the missing
   number's line was garbled by OCR, is recovered by splitting the previous paragraph at
   its most likely start. OCR misreads of "I" as "1" or "|" at the start of a paragraph
   ("11.1 also" for "11. I also", "5. 1 have") are corrected.
4. Headings. A short line before a numbered unit, after a finished sentence (or in
   capitals), is a heading: it becomes the "section" of the units that follow, not text.

The body ends at "Statement of Truth" (OCR variants tolerated). Each unit is
{"para_no", "text", "page", "section"}: para_no is an int for top-level paragraphs and a
string ("12.1", "12(a)") for sub-units.
"""
import hashlib
import io
import json
import re
import subprocess
import tempfile
from collections import Counter
from collections.abc import Callable
from pathlib import Path
from pypdf import PdfReader
from crossexam.config import get_settings

PARSER_VERSION = 2

# The body ends at the statement of truth: its heading (OCR variants: "StateTent of Tnnth")
# or its sentence ("I believe the content of this statement to be true"), or else at the
# exhibit index.
END_RE = re.compile(r"^\W*(stat\w*\s+of\s+t\w{3,}\W*$|index(\s+(to|of)\b.{0,80})?\W*$|"
                    r"i\s+believe\s+(that\s+)?the\s+(facts|contents?)\b.{0,60}\btrue)", re.IGNORECASE)
PAGE_NUMBER_RE = re.compile(r"^\W*(page\s*\S{1,4}\s*of\s*\S{1,4}|\d{1,3}(\s*/\s*\d{1,3})?)\W*$", re.IGNORECASE)
DETACHED_NUMBER_RE = re.compile(r"^\d{1,3}(\.\d{1,2}){0,2}\.?$")  # a number with its text elsewhere
SUB_RE = re.compile(r"^(\d{1,3})\.(\d{1,2})(?:\.(\d{1,2}))?\.?\s+(\S.*)$")
# "12. Text", "12 Text" (no full stop), "12.Text" (no space) and "12- Text" / "12_ Text"
# (OCR misreading the full stop)
TOP_RE = re.compile(r"^(\d{1,3})(?:([.,:;_-])(?:\s+|(?=[A-Z|\"'\u2018\u201c(]))|\s+)(\S.*)$")
ITEM_RE = re.compile(r"^\(?([a-z]|[ivx]{1,5})\)\s+(\S.*)$")
SENTENCE_END_RE = re.compile(r"[.:;?!\"')”]$")
WORD_RE = re.compile(r"^[A-Za-z][a-z']{1,}[.,;:)]?$")
JUNK_RE = re.compile(r"[~°`^¢€¬•►✓|{}\\]|[a-z][A-Z]|[A-Za-z]\d|\d[a-z]")
DOC_ID_TOKEN_RE = re.compile(r"^\W*[A-Z]{2,5}[0-9O]{5,}[_\-0-9A-Z]*\W*$")
LOST_NUMBER_RE = re.compile(r"^[.,]\s+([A-Z|].*)$")
LEADING_JUNK_RE = re.compile(r"^[.|•·,'`~_\-\s]{1,4}(?=\d{1,3}\W)")  # ". 14.To", "| 15. During"
DECLARATION_RE = re.compile(r"will say as follows", re.IGNORECASE)      # "I, NAME, will say as follows:"
LETTERS = "abcdefghijklmnopqrstuvwxyz"
ROMANS = ["i", "ii", "iii", "iv", "v", "vi", "vii", "viii", "ix", "x", "xi", "xii"]


def unit_key(value) -> int | str | None:
    """A unit ID in its canonical form: an int for a top-level paragraph (12, "12", 12.0), a
    string for a sub-unit ("12.1", 12.1, "7(a)"). For matching IDs given by an LLM or a user."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value) if value.is_integer() else str(value)
    text = str(value).strip().lstrip("¶").strip()
    return int(text) if text.isdigit() else (text or None)


# ---------------------------------------------------------------- page text and OCR

def word_ratio(text: str) -> float:
    toks = text.split()
    return sum(bool(WORD_RE.match(t)) for t in toks) / len(toks) if toks else 0.0


def junk_ratio(text: str) -> float:
    """Share of tokens that look like OCR junk (document IDs such as POL00055418 excepted)."""
    toks = text.split()[4:]  # skip running ID lines
    if len(toks) < 15:
        return 0.0
    return sum(bool(JUNK_RE.search(t)) and not DOC_ID_TOKEN_RE.match(t) for t in toks) / len(toks)


def _needs_ocr(text: str) -> bool:
    s = get_settings()
    return len(text.strip()) < s.ocr_min_chars or junk_ratio(text) >= s.ocr_max_junk_ratio


Source = Path | str | bytes       # a PDF on disk, or its bytes (an upload held in memory)
Progress = Callable[..., None]     # progress(stage, **details); stages: "reading", "ocr", "parsing"


def _reader(source: Source) -> PdfReader:
    return PdfReader(io.BytesIO(source) if isinstance(source, bytes) else source)


def ocr_available() -> bool:
    """Whether page OCR can run: pypdfium2 importable and the Tesseract command found."""
    import shutil
    try:
        import pypdfium2  # noqa: F401
    except ImportError:
        return False
    return shutil.which(get_settings().tesseract_cmd) is not None


def _ocr_page(source: Source, index: int) -> str | None:
    """Tesseract text for one page, or None if OCR is unavailable."""
    s = get_settings()
    try:
        import pypdfium2 as pdfium
    except ImportError:
        return None
    with tempfile.TemporaryDirectory() as tmp:  # removed afterwards: nothing is kept
        png = Path(tmp) / "page.png"
        doc = pdfium.PdfDocument(source if isinstance(source, bytes) else str(source))
        doc[index].render(scale=s.ocr_dpi / 72).to_pil().save(png)
        try:
            run = subprocess.run([s.tesseract_cmd, str(png), "-", "--psm", "6"],
                                 capture_output=True, text=True, timeout=120)
        except (OSError, subprocess.TimeoutExpired):
            return None
    return run.stdout if run.returncode == 0 else None


def page_texts(source: Source, progress: Progress | None = None) -> tuple[list[str], list[int]]:
    """(text per page, 1-based numbers of pages whose text came from OCR)."""
    s = get_settings()
    progress = progress or (lambda *a, **k: None)
    reader = _reader(source)
    pages = len(reader.pages)
    texts = []
    for i, page in enumerate(reader.pages):
        progress("reading", page=i + 1, pages=pages)
        texts.append(page.extract_text() or "")
    # Where paragraph numbers sit in their own column, plain extraction puts them on lines
    # of their own, away from their text; layout extraction keeps each number on its line.
    for i, t in enumerate(texts):
        if any(DETACHED_NUMBER_RE.match(l.strip()) for l in t.splitlines()):
            layout = reader.pages[i].extract_text(extraction_mode="layout") or ""
            texts[i] = "\n".join(" ".join(l.split()) for l in layout.splitlines() if l.strip())
    last = next((i for i, t in enumerate(texts) if any(END_RE.match(l.strip()) for l in t.splitlines())), len(texts) - 1)
    ocr_pages = []
    if s.ocr_enabled:
        for i in range(last + 1):
            if _needs_ocr(texts[i]):
                progress("ocr", page=i + 1, pages=pages)
                ocr = _ocr_page(source, i)
                if ocr and (len(texts[i].strip()) < s.ocr_min_chars or junk_ratio(ocr) < junk_ratio(texts[i])):
                    texts[i] = ocr
                    ocr_pages.append(i + 1)
    return texts, ocr_pages


# ---------------------------------------------------------------- page furniture

def _furniture_key(line: str) -> str:
    """Lines compared on letters and digits only, ignoring case, numbers and O/0 confusion
    (OCR varies the punctuation and spacing of the same footer from page to page)."""
    return re.sub(r"\d+", "#", re.sub(r"[^A-Z0-9]", "", line.upper()).replace("O", "0"))


def strip_furniture(texts: list[str]) -> list[list[str]]:
    """Lines per page with running headers, footers and page numbers removed."""
    s = get_settings()
    edge = s.parser_edge_lines
    pages = [[l.strip() for l in t.splitlines() if l.strip()] for t in texts]
    counts = Counter()
    for lines in pages:
        edges = {_furniture_key(l) for l in lines[:edge] + lines[-edge:]}
        counts.update(edges - {""})
    threshold = max(2, s.parser_running_line_min_fraction * len(pages))
    running = {k for k, n in counts.items() if n >= threshold}
    # Long running lines are also removed elsewhere on the page and with a few junk characters
    # around them (OCR noise, or layouts that put the footer mid-page).
    long_running = [k for k in running if len(k) >= 15]

    def is_running(key: str) -> bool:
        return key in running or any(k in key and len(key) <= len(k) + 12 for k in long_running)

    out = []
    for lines in pages:
        n = len(lines)
        keep = [l for i, l in enumerate(lines)
                if not (is_running(_furniture_key(l)) and ((i < edge or i >= n - edge) or len(_furniture_key(l)) >= 15)
                        or (i < edge or i >= n - edge) and PAGE_NUMBER_RE.match(l))]
        out.append(keep)
    return out


# ---------------------------------------------------------------- numbering

def _fix_leading_i(text: str) -> str:
    """'1 have' / '| had' at the start of a paragraph is OCR for 'I have' / 'I had'."""
    return re.sub(r"^[1|l](?=\s+[a-z])", "I", text)


def _is_heading(line: str, prev: str | None, max_words: int) -> bool:
    if len(line.split()) > max_words or SENTENCE_END_RE.search(line) or not line[0].isalpha():
        return False
    letters = [c for c in line if c.isalpha()]
    caps = letters and sum(c.isupper() for c in letters) / len(letters) > 0.8
    return bool(caps) or prev is None or bool(SENTENCE_END_RE.search(prev))


def _split_missing(lines: list[tuple[int, str]], missing_no: int) -> int:
    """Index where an OCR-garbled paragraph start most likely is: a line after a
    sentence end, preferring one that begins with the missing number's first digit."""
    candidates = [i for i in range(1, len(lines)) if SENTENCE_END_RE.search(lines[i - 1][1])]
    by_digit = [i for i in candidates if lines[i][1].startswith(str(missing_no)[0])]
    return (by_digit or candidates or [len(lines)])[-1]


class _Builder:
    def __init__(self, lines: list[tuple[int, str]]):
        s = get_settings()
        self.all_lines = lines   # [(page, line)], for confirming jumps by looking ahead
        self.pos = 0
        self.gaps = []           # (first missing, last missing, page before, page after)
        self.max_gap = s.parser_max_para_gap
        self.lookahead = s.parser_jump_lookahead_lines
        self.heading_words = s.parser_heading_max_words
        self.units = []          # {"para_no", "lines": [(page, text)], "section"}
        self.top = 0             # current top-level number
        self.sub = ()            # current dotted sub path, e.g. (6, 2)
        self.items = {}          # unit id -> last lettered/roman item index
        self.section = None
        self.preamble = []       # lines before paragraph 1
        self.warnings = []

    @property
    def current(self):
        return self.units[-1] if self.units else None

    def _take_heading(self):
        """Move heading lines at the end of the current unit (or preamble) into the section."""
        lines = self.current["lines"] if self.current else [(0, l) for l in self.preamble]
        heading = []
        while len(lines) > (1 if self.current else 0):
            text = lines[-1][1]
            prev = lines[-2][1] if len(lines) > 1 else None
            if not _is_heading(text, prev, self.heading_words):
                break
            heading.insert(0, lines.pop()[1])
        if heading:
            self.section = " ".join(heading)

    def start(self, para_no, page, text):
        self._take_heading()
        self.units.append({"para_no": para_no, "lines": [(page, text)] if text else [], "section": self.section})

    def add(self, page, line):
        if self.current:
            self.current["lines"].append((page, line))
        else:
            self.preamble.append(line)

    def _recover_missing(self, missing):
        prev = self.current["lines"]
        i = _split_missing(prev, missing)
        if i < len(prev):
            self.warnings.append(f"paragraph {missing} number not found (OCR?); recovered from text starting {prev[i][1][:40]!r}")
            self.units.append({"para_no": missing, "lines": prev[i:], "section": self.current["section"]})
            del prev[i:]
        else:
            self.warnings.append(f"paragraph {missing} not found")

    def line(self, page, line):
        if not self.units and DECLARATION_RE.search(line):
            return self.add(page, line)  # the opening declaration, even if OCR reads "I," as "1,"
        line = LEADING_JUNK_RE.sub("", line)
        line = re.sub(r"(?<!\S)\|(?!\S)", "I", line)  # OCR: a lone "|" is "I"
        expected = self.top + 1
        m = re.match(r"^(\d{2,4}(?:\.\d{1,2})?)\.?\s+(\S.*)$", line)
        if m and (path := self._as_lost_dot_sub(m.group(1).replace(".", ""))):
            self.sub = path
            return self.start(".".join(map(str, path)), page, _fix_leading_i(m.group(2)))
        m = SUB_RE.match(line)
        if m:
            parent, a, b, rest = int(m.group(1)), int(m.group(2)), m.group(3), m.group(4)
            if parent == expected and a == 1 and b is None and rest[0].islower():
                return self._new_top(expected, page, _fix_leading_i("1 " + rest))  # "11.1 also" = "11. I also"
            path = (parent, a) + ((int(b),) if b else ())
            if parent == self.top and path > ((self.top,) + self.sub[1:] if self.sub else (self.top,)):
                self.sub = path
                return self.start(".".join(map(str, path)), page, _fix_leading_i(rest))
            if parent == expected and a > 1 and self.sub and self.sub[0] == self.top and \
                    path[1:] == (self.sub[1] + 1,) + path[2:]:
                # "16.3" right after 15.2: a new paragraph's first sub-paragraph would be .1,
                # so this is 15.3 with the paragraph number misread.
                path = (self.top,) + path[1:]
                self.sub = path
                return self.start(".".join(map(str, path)), page, _fix_leading_i(rest))
            if parent == expected:
                self._new_top(expected, page, "")  # implicit parent: its text is all in sub-paragraphs
                self.sub = path
                return self.start(".".join(map(str, path)), page, _fix_leading_i(rest))
        m = LOST_NUMBER_RE.match(line)
        if m and self.current and self.current["lines"] and SENTENCE_END_RE.search(self.current["lines"][-1][1]):
            self.warnings.append(f"paragraph {expected} had no number (only a full stop)")
            return self._new_top(expected, page, _fix_leading_i(m.group(1)))
        m = TOP_RE.match(line)
        if m:
            num, dot, rest = int(m.group(1)), m.group(2), m.group(3)
            if num == expected or (num == expected + 1 and self.units) or \
                    (num > expected and ((dot and num <= expected + self.max_gap) or
                                         (self.units and self._confirmed(num)))):
                if num == expected + 1 and self.units:
                    self._recover_missing(expected)
                elif num > expected:
                    self.warnings.append(f"paragraphs {expected}-{num - 1} not found")
                    # The previous unit has absorbed the unnumbered text, so the gap spans
                    # from the page where it starts to here.
                    before = self.current["lines"][0][0] if self.current and self.current["lines"] else 1
                    self.gaps.append((expected, num - 1, before, page))
                return self._new_top(num, page, _fix_leading_i(rest))
        m = ITEM_RE.match(line)
        if m and self.current:
            base = str(self.current["para_no"]).split("(")[0]
            label, rest = m.group(1), m.group(2)
            last = self.items.get(base, -1)
            kind = "letter" if label in LETTERS else None
            if kind and LETTERS.index(label) == last + 1:
                self.items[base] = last + 1
                return self.start(f"{base}({label})", page, rest)
            if label in ROMANS and ROMANS.index(label) == self.items.get(base + "#roman", -1) + 1:
                self.items[base + "#roman"] = ROMANS.index(label)
                return self.start(f"{base}({label})", page, rest)
        self.add(page, line)

    def _confirmed(self, num: int) -> bool:
        """A jump to num is real if the next numbered lines continue num+1, num+2 (sub-paragraphs
        of those allowed) with no smaller number first, which would show the jump was a misread."""
        want, found = num + 1, 0
        for _, line in self.all_lines[self.pos + 1:self.pos + self.lookahead]:
            m = SUB_RE.match(line) or TOP_RE.match(line)
            if not m:
                continue
            n = int(m.group(1))
            if n < num:
                return False
            if n == want:
                want, found = want + 1, found + 1
                if found == 2:
                    return True
            elif n > want:
                return False
        return False

    def _as_lost_dot_sub(self, digits: str) -> tuple | None:
        """OCR sometimes drops the dot in a sub-paragraph number ("24" for 2.4, "26.3" for 2.6.3).
        If digits match the next sub-paragraph of the current paragraph with the dots removed,
        return that path."""
        if not self.sub:
            return None
        nxt = [self.sub[:-1] + (self.sub[-1] + 1,), self.sub + (1,)]
        if len(self.sub) > 2:
            nxt.append(self.sub[:-2] + (self.sub[-2] + 1,))
        for path in nxt:
            if "".join(map(str, path)) == digits:
                return path
        return None

    def _new_top(self, num, page, text):
        self.top, self.sub = num, ()
        self.start(num, page, text)

    def finish(self) -> list[dict]:
        out = []
        for u in self.units:
            if not u["lines"]:
                continue  # an implicit parent with no text of its own
            out.append({"para_no": u["para_no"], "text": " ".join(t for _, t in u["lines"]),
                        "page": u["lines"][0][0], "section": u["section"]})
        return out


def _parse_texts(texts: list[str]) -> "_Builder":
    lines = [(page_no, line) for page_no, page in enumerate(strip_furniture(texts), start=1) for line in page]
    b = _Builder(lines)
    for b.pos, (page_no, line) in enumerate(lines):
        # Some statements number the statement of truth ("140. I believe the facts ... true").
        if END_RE.match(line) or END_RE.match(re.sub(r"^\d{1,3}[.,]?\s+", "", line)):
            break
        b.line(page_no, line)
    return b


def _missing(b: "_Builder") -> int:
    return sum(last - first + 1 for first, last, _, _ in b.gaps)


def parse_statement(source: Source, progress: Progress | None = None) -> dict:
    """{"paragraphs", "preamble" (text before paragraph 1: witness name, statement title),
    "pages", "ocr_pages", "warnings"} for a PDF path or its bytes. Nothing is written to disk."""
    s = get_settings()
    report = progress or (lambda *a, **k: None)
    texts, ocr_pages = page_texts(source, progress)
    report("parsing", pages=len(texts))
    b = _parse_texts(texts)
    # Paragraph numbers missing from the text layer: re-read the pages around each gap with
    # OCR and parse again, keeping whichever parse is missing fewer numbers.
    gap_pages = sorted({p for _, _, a, z in b.gaps for p in range(a, z + 1)} - set(ocr_pages))
    gap_pages = gap_pages[:s.parser_gap_ocr_max_pages]
    if s.ocr_enabled and gap_pages:
        retry = list(texts)
        for p in gap_pages:
            report("ocr", page=p, pages=len(texts))
            ocr = _ocr_page(source, p - 1)
            if ocr:
                retry[p - 1] = ocr
        b2 = _parse_texts(retry)
        if _missing(b2) < _missing(b):
            b, ocr_pages = b2, sorted(set(ocr_pages) | set(gap_pages))
    return {"paragraphs": b.finish(), "preamble": " ".join(b.preamble), "pages": len(texts),
            "ocr_pages": ocr_pages, "warnings": b.warnings}


def parse_pdf(pdf_path: Source) -> list[dict]:
    result = parse_statement(pdf_path)
    if result["ocr_pages"]:
        print(f"OCR used for pages {result['ocr_pages']}")
    for w in result["warnings"]:
        print(f"warning: {w}")
    return result["paragraphs"]


def _cache_key() -> str:
    s = get_settings()
    parts = [PARSER_VERSION, s.ocr_enabled, s.ocr_min_chars, s.ocr_max_junk_ratio, s.ocr_dpi,
             s.parser_running_line_min_fraction, s.parser_edge_lines, s.parser_gap_ocr_max_pages,
             s.parser_jump_lookahead_lines, s.parser_max_para_gap, s.parser_heading_max_words]
    return hashlib.sha256(json.dumps(parts).encode()).hexdigest()[:12]


def load_statement(pdf_path: Path) -> list[dict]:
    """Parse a statement, using the cached JSON under processed_dir if it is newer than the
    PDF and was made by this parser with the same settings."""
    s = get_settings()
    pdf_path = Path(pdf_path)
    cache = s.resolve(s.processed_dir) / f"{pdf_path.stem}.json"
    if cache.exists() and cache.stat().st_mtime >= pdf_path.stat().st_mtime:
        data = json.loads(cache.read_text())
        if isinstance(data, dict) and data.get("parser") == _cache_key():
            return data["paragraphs"]
    paragraphs = parse_pdf(pdf_path)
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text(json.dumps({"parser": _cache_key(), "paragraphs": paragraphs}, indent=2))
    return paragraphs
