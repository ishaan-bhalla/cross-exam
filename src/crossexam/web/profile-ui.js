// Pure helpers for the confirm step (no DOM), tested with node --test (web/tests).

const SMALL = new Set(["of", "and", "the", "de", "van", "von", "der", "da", "du", "la", "le"]);

// "STEPHEN ROBERT GRAYSTON" -> "Stephen Robert Grayston"; names with any lower case are kept.
// Capitalises after spaces, hyphens and apostrophes ("O'BRIEN-SMITH" -> "O'Brien-Smith"),
// and the letter after "Mc" ("MCNIVEN" -> "McNiven").
export function titleCase(name) {
  const text = String(name ?? "").trim();
  if (!/[A-Z]/.test(text) || text !== text.toUpperCase()) return text;
  return text.toLowerCase().split(/(\s+)/).map((word, i) => {
    if (/^\s+$/.test(word) || (i > 0 && SMALL.has(word))) return word;
    return word.replace(/(^|[-'’])([a-z])/g, (_, sep, c) => sep + c.toUpperCase())
               .replace(/^Mc([a-z])/, (_, c) => "Mc" + c.toUpperCase());
  }).join("");
}

// Same as witness.profile.role_label: "Title, Organisation (from–to)".
export function roleLabel(roles, organisation) {
  if (!roles.length) return organisation || "";
  const r = roles[0];
  const org = r.organisation || organisation;
  const dates = [r.from, r.to].filter(Boolean).join("–");
  return sentenceStart(`${r.title}${org ? ", " + org : ""}${dates ? ` (${dates})` : ""}`);
}

// A label shown as a heading starts with a capital, even when the statement's wording does not.
export function sentenceStart(text) {
  return text ? text[0].toUpperCase() + text.slice(1) : text;
}

const escape = (s) => s.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");

// A case-insensitive pattern for value that ignores spacing and the difference between
// straight and curly quotes and between dashes, like the server's profile check.
export function valuePattern(value) {
  const v = String(value ?? "").trim().replace(/^[\s.,;:()"']+|[\s.,;:()"']+$/g, "");
  if (!v) return null;
  const body = v.split(/\s+/).map((w) => escape(w)
    .replace(/['‘’]/g, "['‘’]").replace(/["“”]/g, "[\"“”]").replace(/[-–—]/g, "[-–—]")).join("\\s+");
  return new RegExp(body, "i");
}

// Where value first appears: {key, start, end} with key "preamble" or a unit's para_no,
// searching the title page first, then the paragraphs in order. null if nowhere.
export function findSource(value, preamble, units) {
  const re = valuePattern(value);
  if (!re) return null;
  const places = [["preamble", preamble || ""], ...units.map((u) => [u.para_no, u.text])];
  for (const [key, text] of places) {
    const m = re.exec(text);
    if (m) return { key, start: m.index, end: m.index + m[0].length };
  }
  return null;
}

// Plain words for an upload that the server refused.
export function uploadError(status, detail, maxMb) {
  if (status === 413) return `That file is larger than the ${maxMb} MB limit. Try a smaller PDF, for example the statement without its exhibits.`;
  if (status === 415) return "That file is not a PDF. Upload the witness statement as a PDF file.";
  if (status === 429) return detail || "You have too many statements open. Remove one from the list below, then try again.";
  if (status === 0) return "The upload did not reach the server. Check your connection and try again.";
  return detail ? `The statement could not be uploaded: ${detail}` : "The statement could not be uploaded. Please try again.";
}
