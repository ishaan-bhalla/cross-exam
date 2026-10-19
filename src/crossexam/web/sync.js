// Pure helpers for word-synced highlighting (no DOM), so they can be tested in Node.

// Index of the word being spoken at audio time tMs: a word is active from its start
// until the next word's start (end times are ignored). -1 before the first word.
export function activeWordIndex(words, tMs) {
  let active = -1;
  for (let i = 0; i < words.length; i++) {
    if (words[i].start <= tMs) active = i;
    else break;
  }
  return active;
}

// Split paragraph text into pieces, marking the characters covered by each word's span.
// Returns [{text, word}] where word is the index into `words` or null.
export function splitBySpans(text, words) {
  const spans = words
    .map((w, i) => (w.span ? { start: w.span[0], end: w.span[1], word: i } : null))
    .filter(Boolean)
    .sort((a, b) => a.start - b.start);
  const out = [];
  let pos = 0;
  for (const s of spans) {
    if (s.start < pos) continue; // overlapping span: keep the first
    if (s.start > pos) out.push({ text: text.slice(pos, s.start), word: null });
    out.push({ text: text.slice(s.start, s.end), word: s.word });
    pos = s.end;
  }
  if (pos < text.length) out.push({ text: text.slice(pos), word: null });
  return out;
}

// splitBySpans, plus a `band` index on every piece that belongs to the quote's highlight:
// a word's own index, or for the text between two quoted words (spaces, "&", punctuation)
// the index of the word after it, so the band grows without gaps. Gaps that contain
// letters or digits (e.g. text left out by an elision) are not part of the band.
export function quotePieces(text, words) {
  const pieces = splitBySpans(text, words).map((p) => ({ ...p, band: p.word }));
  for (let i = 1; i < pieces.length - 1; i++) {
    const p = pieces[i];
    if (p.word === null && pieces[i - 1].word !== null && pieces[i + 1].word !== null && !/[\p{L}\p{N}]/u.test(p.text)) {
      p.band = pieces[i + 1].word;
    }
  }
  return pieces;
}

// Insert words into a list kept sorted by start time.
export function mergeWords(existing, incoming) {
  return [...existing, ...incoming].sort((a, b) => a.start - b.start);
}
