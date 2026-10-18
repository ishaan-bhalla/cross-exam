"""Parse an Inquiry witness statement PDF into numbered paragraphs.

Body paragraphs are numbered "1.", "2." ... at line start. Each page carries
statement ID header lines (OCR'd, e.g. "W I TNO4630100") and a "Page X of Y"
footer. The body ends at "Statement of Truth"; the exhibit index after it is
numbered from 1 again and is not part of the evidence.
"""
import json
import re
from pathlib import Path
from pypdf import PdfReader
from crossexam.config import get_settings

HEADER_RE = re.compile(r"^W\s*I\s*T\s*N\s*[\dO\s]+$")
FOOTER_RE = re.compile(r"^Page\s+\d+\s+of\s+\d+$", re.IGNORECASE)
END_RE = re.compile(r"^Statement of Truth$", re.IGNORECASE)
PARA_RE = re.compile(r"^(\d{1,3})[.,]\s+(.*)$")
SENTENCE_END_RE = re.compile(r"[.:;?!\"')”]$")
HEADING_MAX_WORDS = 8


def _is_heading(line: str, prev: str) -> bool:
    return (len(line.split()) <= HEADING_MAX_WORDS
            and not SENTENCE_END_RE.search(line)
            and bool(SENTENCE_END_RE.search(prev)))


def _lines(pdf_path: Path):
    for page_no, page in enumerate(PdfReader(pdf_path).pages, start=1):
        for raw in page.extract_text().splitlines():
            line = raw.strip()
            if line and not HEADER_RE.match(line) and not FOOTER_RE.match(line):
                yield page_no, line


def _split_missing(lines: list[tuple[int, str]], missing_no: int) -> int:
    """Index where an OCR-garbled paragraph start most likely is: a line after a
    sentence end, preferring one that begins with the missing number's first digit."""
    candidates = [i for i in range(1, len(lines)) if SENTENCE_END_RE.search(lines[i - 1][1])]
    by_digit = [i for i in candidates if lines[i][1].startswith(str(missing_no)[0])]
    return (by_digit or candidates or [len(lines)])[-1]


def parse_pdf(pdf_path: Path) -> list[dict]:
    paragraphs = []  # each: {"para_no", "lines": [(page, text)]}
    for page_no, line in _lines(pdf_path):
        if END_RE.match(line):
            break
        m = PARA_RE.match(line)
        expected = len(paragraphs) + 1
        num = int(m.group(1)) if m else None
        if num == expected + 1 and paragraphs:
            prev = paragraphs[-1]["lines"]
            i = _split_missing(prev, expected)
            print(f"warning: paragraph {expected} number not found (OCR?); "
                  f"recovered from text starting {prev[i][1][:40]!r}" if i < len(prev) else
                  f"warning: paragraph {expected} not found; left empty")
            paragraphs.append({"para_no": expected, "lines": prev[i:]})
            del prev[i:]
            expected += 1
        if num == expected:
            if paragraphs:
                prev = paragraphs[-1]["lines"]
                if len(prev) > 1 and _is_heading(prev[-1][1], prev[-2][1]):
                    prev.pop()
            paragraphs.append({"para_no": num, "lines": [(page_no, m.group(2))]})
        elif paragraphs:
            paragraphs[-1]["lines"].append((page_no, line))
    return [{"para_no": p["para_no"],
             "text": " ".join(text for _, text in p["lines"]),
             "page": p["lines"][0][0] if p["lines"] else None}
            for p in paragraphs]


def load_statement(pdf_path: Path) -> list[dict]:
    """Parse a statement, using the cached JSON under processed_dir if it is newer than the PDF."""
    s = get_settings()
    pdf_path = Path(pdf_path)
    cache = s.resolve(s.processed_dir) / f"{pdf_path.stem}.json"
    if cache.exists() and cache.stat().st_mtime >= pdf_path.stat().st_mtime:
        return json.loads(cache.read_text())
    paragraphs = parse_pdf(pdf_path)
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text(json.dumps(paragraphs, indent=2))
    return paragraphs
