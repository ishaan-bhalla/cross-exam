import { activeWordIndex, mergeWords, quotePieces } from "./sync.js";
import { prefetchSttSession, startMic } from "./mic.js";

const $ = (id) => document.getElementById(id);
const params = new URLSearchParams(location.search);

let config = null; // from /api/config
let paragraphs = new Map(); // para_no -> text
let ws = null;
let audioCtx = null;
let current = null; // state of the answer being played
let witnessId = null;
let mic = null; // { stop } while listening
let spoken = null; // timing for a question asked by voice, shown under it
let witnesses = []; // this visitor's witnesses, for the switcher

function el(tag, cls, text) {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (text !== undefined) e.textContent = text;
  return e;
}

// The page is replaced by a notice when the witness cannot be examined (expired, unknown, unconfirmed).
function showGone(title, text) {
  $("exam").hidden = true;
  $("gone").hidden = false;
  $("gone-title").textContent = title;
  $("gone-text").textContent = text;
  document.title = `${title} · Cross-examine`;
}

async function init() {
  config = await (await fetch("/api/config")).json();
  const list = await (await fetch("/api/witnesses")).json();
  const wanted = params.get("witness");
  const witness = wanted ? list.find((w) => w.id === wanted) : list[0];
  const res = await fetch(`/api/witnesses/${encodeURIComponent(witness?.id ?? wanted)}/statement`);
  if (res.status === 410) return showGone("This witness is no longer available", (await res.json()).detail);
  if (!witness || !res.ok) return showGone("Witness not found", "This witness is not available. Upload the statement again, or try the example witness.");
  if (witness.uploaded && !witness.profile_confirmed) location.replace(`/?upload=${encodeURIComponent(witness.id)}`);
  witnessId = witness.id;
  witnesses = list;
  renderSwitcher();
  prefetchSttSession(witnessId); // so the mic does not wait for a token
  // Role first, then name: the values the user confirmed for an upload.
  $("witness-role").textContent = witness.role_label;
  $("witness-name").textContent = witness.name;
  document.title = `${witness.name} · Cross-examine`;

  const statement = await res.json();
  const box = $("statement");
  let section = null;
  for (const p of statement) {
    if (p.section && p.section !== section) {
      box.append(el("div", "section-heading", p.section)); // headings are labels, not paragraphs
      section = p.section;
    }
    paragraphs.set(p.para_no, p.text);
    const row = el("div", "para");
    row.id = `para-${p.para_no}`; // sub-units too: para-12.1, para-7(a)
    if (typeof p.para_no === "string") row.classList.add("sub");
    row.append(el("span", "no", p.para_no), el("span", "body", p.text));
    box.append(row);
  }
  await restoreConversation();
  connect(witness.id);
}

// ---------------------------------------------------------------- witness switcher

// A disclosure menu of links: the example witness and confirmed uploads, role then name, with
// each one's number of questions; the current one is marked. Choosing one navigates to it.
function renderSwitcher() {
  const open = witnesses.filter((w) => !w.uploaded || w.profile_confirmed);
  const current = open.find((w) => w.id === witnessId);
  $("switch-current").textContent = current ? current.name : "Witness";
  $("switch-menu").replaceChildren(...open.map((w) => {
    const a = el("a", "switch-item");
    a.href = `examine.html?witness=${encodeURIComponent(w.id)}`;
    if (w.id === witnessId) a.setAttribute("aria-current", "page");
    const n = w.questions;
    a.append(el("span", "role", w.role_label), el("span", "name", w.name),
             el("span", "count", `${n} ${n === 1 ? "question" : "questions"}${w.id === witnessId ? " · current" : ""}`));
    const li = el("li");
    li.append(a);
    return li;
  }), (() => {
    const li = el("li", "upload-item");
    const a = el("a", "switch-item", "Upload another statement");
    a.href = "/#upload";
    li.append(a);
    return li;
  })());
}

function setSwitcherOpen(open) {
  $("switch-button").setAttribute("aria-expanded", String(open));
  $("switch-menu").hidden = !open;
  if (open) ($("switch-menu").querySelector("[aria-current]") || $("switch-menu").querySelector("a"))?.focus();
}

$("switch-button").addEventListener("click", () => setSwitcherOpen($("switch-menu").hidden));
$("switcher").addEventListener("keydown", (e) => {
  const links = [...$("switch-menu").querySelectorAll("a")];
  const i = links.indexOf(document.activeElement);
  if (e.key === "Escape" && !$("switch-menu").hidden) { setSwitcherOpen(false); $("switch-button").focus(); }
  else if (e.key === "ArrowDown" && i > -1) { e.preventDefault(); links[(i + 1) % links.length].focus(); }
  else if (e.key === "ArrowUp" && i > -1) { e.preventDefault(); links[(i - 1 + links.length) % links.length].focus(); }
  else if (e.key === "ArrowDown" && document.activeElement === $("switch-button")) { e.preventDefault(); setSwitcherOpen(true); }
});
document.addEventListener("click", (e) => { if (!$("switcher").contains(e.target)) setSwitcherOpen(false); });
$("switcher").addEventListener("focusout", (e) => { if (!$("switcher").contains(e.relatedTarget)) setSwitcherOpen(false); });

function bumpQuestionCount(delta) {
  const w = witnesses.find((x) => x.id === witnessId);
  if (w) { w.questions = delta === null ? 0 : w.questions + delta; renderSwitcher(); }
}

// ---------------------------------------------------------------- conversation

// The server keeps this visitor's conversation with the witness; draw it in order, scrolled to
// the latest answer, with the last cited unit marked in the statement.
async function restoreConversation() {
  const res = await fetch(`/api/witnesses/${encodeURIComponent(witnessId)}/conversation`);
  const turns = res.ok ? await res.json() : [];
  let last = null;
  for (const t of turns) {
    const counsel = addTurn("counsel", "Counsel", t.question, { scroll: false, asked: t.asked });
    if (params.has("debug") && t.timing?.voice?.end_of_speech_to_submit_ms != null)
      counsel.append(el("div", "timing", `asked by voice · end of speech to submitted ${t.timing.voice.end_of_speech_to_submit_ms} ms`));
    const turn = renderWitnessTurn(t.answer, { scroll: false });
    addReplay(turn, t.id);
    if (params.has("debug") && t.timing) turn.append(el("div", "timing", `answer ${t.timing.answer_latency_ms} ms · audio ${(t.timing.duration_ms / 1000).toFixed(1)} s · ${describeUsage(t.timing.usage)}`));
    last = t.answer;
  }
  $("clear-conversation").hidden = !turns.length;
  if (!turns.length) return;
  $("transcript").lastElementChild.scrollIntoView({ block: "end" });
  if (last && !last.refused) {
    // A minimal current answer, so the next question clears this mark as usual.
    startAnswer();
    current.answer = last;
    markUnit(last, "auto");
  }
}

function addReplay(turn, id) {
  turn.dataset.turn = id;
  const replay = el("button", "replay");
  replay.type = "button";
  replay.innerHTML = '<svg width="10" height="10" viewBox="0 0 12 12" aria-hidden="true"><path d="M3 1.5v9l7-4.5z" fill="currentColor"/></svg><span>Replay</span>';
  replay.setAttribute("aria-label", "Replay this answer");
  replay.addEventListener("click", () => {
    if (!ws || ws.readyState !== WebSocket.OPEN || $("ask-button").disabled) return;
    ensureAudio();
    if (mic) stopMic();
    startAnswer();
    current.replayTurn = turn;
    setBusy(true);
    setStatus("Replaying…");
    ws.send(JSON.stringify({ type: "replay", turn: id }));
  });
  turn.querySelector(".who").append(" ", replay);
}

$("clear-conversation").addEventListener("click", async () => {
  if (!confirm("Clear this conversation? The questions and answers with this witness will be deleted.")) return;
  const res = await fetch(`/api/witnesses/${encodeURIComponent(witnessId)}/conversation`, { method: "DELETE" });
  if (!res.ok) { setStatus("The conversation could not be cleared."); return; }
  clearHighlight();
  current = null;
  $("transcript").replaceChildren();
  $("clear-conversation").hidden = true;
  bumpQuestionCount(null);
  setStatus("Conversation cleared.");
  $("question").focus();
});

function connect(witnessId) {
  ws = new WebSocket(`${location.protocol === "https:" ? "wss" : "ws"}://${location.host}/ws/examine?witness=${encodeURIComponent(witnessId)}`);
  ws.onopen = () => setStatus("Ready.");
  ws.onclose = () => { setStatus("Disconnected. Reload to reconnect."); setBusy(true); };
  ws.onmessage = (e) => handle(JSON.parse(e.data));
}

function setStatus(text) { $("status").textContent = text; }
function setMicLabel(text) { $("mic-button").querySelector(".label").textContent = text; }
function setBusy(busy) {
  $("ask-button").disabled = busy;
  $("question").disabled = busy;
  $("mic-button").disabled = busy;
}

function ensureAudio() {
  // Playback must be started from a user gesture (the Ask or Speak click).
  audioCtx = audioCtx || new AudioContext();
  audioCtx.resume();
}

function stopMic() {
  mic?.stop();
  mic = null;
  $("mic-button").setAttribute("aria-pressed", "false");
  setMicLabel("Speak");
}

$("mic-button").addEventListener("click", async () => {
  if (mic) { stopMic(); setStatus("Ready."); return; }
  ensureAudio();
  $("mic-button").setAttribute("aria-pressed", "true");
  setMicLabel("Stop");
  setStatus("Starting microphone…");
  try {
    mic = await startMic(witnessId, {
      onListening: () => setStatus("Listening… ask your question."),
      onText: (text) => { $("question").value = text; },
      onSubmit: (text, info) => {
        stopMic();
        if (!text) { setStatus("Didn't catch that. Try again or type."); return; }
        $("question").value = text;
        spoken = info;
        $("ask-form").requestSubmit();
      },
      onError: (message) => { stopMic(); setStatus(`Microphone: ${message}. You can type instead.`); },
    });
  } catch (e) {
    stopMic();
    setStatus(`Microphone unavailable (${e.message}). You can type instead.`);
  }
});

$("ask-form").addEventListener("submit", (e) => {
  e.preventDefault();
  const text = $("question").value.trim();
  if (!text || !ws || ws.readyState !== WebSocket.OPEN) return;
  ensureAudio();
  if (mic) stopMic(); // typed while listening: the typed text wins
  const voice = spoken;
  spoken = null;
  const turn = addTurn("counsel", "Counsel", text, { asked: voice ? "voice" : "typed" });
  if (voice) {
    if (params.has("debug")) turn.append(el("div", "timing", `asked by voice · end of speech to submitted ${voice.end_of_speech_to_submit_ms ?? "?"} ms`));
    window.__lastSpoken = { end_of_speech_to_submit_ms: voice.end_of_speech_to_submit_ms, log: voice.log };
    if (params.has("debug")) {
      const details = el("details", "timing");
      details.append(el("summary", null, "voice timeline"),
        el("pre", null, voice.log.map((e) => `${String(e.t_ms).padStart(6)} ms  ${e.event}${e.detail ? "  " + e.detail : ""}`).join("\n")));
      turn.append(details);
    }
  }
  $("question").value = "";
  setBusy(true);
  setStatus("Witness is considering the question…");
  startAnswer();
  current.counselTurn = turn;
  ws.send(JSON.stringify({ type: "question", text, spoken: Boolean(voice),
                           voice_timing: voice ? { end_of_speech_to_submit_ms: voice.end_of_speech_to_submit_ms,
                                                   stt_audio_seconds: voice.stt_audio_seconds } : undefined }));
});

function addTurn(cls, who, text, { scroll = true, asked = null } = {}) {
  const turn = el("div", `turn ${cls}`);
  const head = el("div", "who", who);
  if (asked === "voice") head.append(" ", el("span", "asked", "by voice"));
  turn.append(head);
  const body = el("div", "text");
  if (text) body.textContent = text;
  turn.append(body);
  if (cls === "counsel") {
    const edit = el("button", "edit", "edit");
    edit.type = "button";
    edit.title = "Fix this question and ask it again";
    edit.addEventListener("click", () => {
      if (mic) stopMic();
      $("question").value = body.textContent;
      $("question").focus();
      setStatus("Edit the question, then press Ask.");
    });
    head.append(" ", edit);
  }
  $("transcript").append(turn);
  if (scroll) turn.scrollIntoView({ block: "end" });
  $("clear-conversation").hidden = false;
  return turn;
}

function startAnswer() {
  clearHighlight();
  current = {
    answer: null, words: [], startTime: null, nextTime: null, sources: [], underruns: 0,
    lateWordsMs: 0, done: null, rafId: null, row: null, bandEls: [], spokenUpTo: 0, lastActive: -1,
    quoteWords: false, heldQuote: [], holdTimer: null, holdReleased: false, quoteHoldMs: 0,
  };
}

function handle(msg) {
  if (msg.type === "error") {
    if (msg.status === 410) return showGone("This witness is no longer available", msg.message);
    setStatus(`Error: ${msg.message}`);
    setBusy(false);
    return;
  }
  if (!current) return;
  if (msg.type === "question") onQuestionUnderstood(msg);
  else if (msg.type === "answer") onAnswer(msg);
  else if (msg.type === "audio") onAudio(msg);
  else if (msg.type === "words") onWords(msg);
  else if (msg.type === "done") onDone(msg);
  else if (msg.type === "replay") onReplay(msg);
  else if (msg.type === "turn") onTurnStored(msg);
}

// A stored answer being spoken again: it plays into its existing turn.
function onReplay(msg) {
  current.answer = msg;
  current.turn = current.replayTurn;
  markUnit(msg, "smooth");
}

function onTurnStored(msg) {
  if (current?.turn && !current.turn.dataset.turn) addReplay(current.turn, msg.turn);
  bumpQuestionCount(1);
}

// A spoken question as the server understood it, with document IDs rebuilt.
function onQuestionUnderstood(msg) {
  const turn = current.counselTurn;
  if (!turn) return;
  turn.querySelector(".text").textContent = msg.text;
  for (const c of msg.id_changes || []) {
    const note = c.snapped ? `heard “${c.heard}”, corrected to ${c.id}` : `heard “${c.heard}” as ${c.id}`;
    turn.append(el("div", "timing", note));
  }
}

function onAnswer(msg) {
  current.answer = msg;
  current.turn = renderWitnessTurn(msg);
  setStatus("Speaking…");
  markUnit(msg, "smooth");
}

function renderWitnessTurn(msg, options) {
  const turn = addTurn("witness", "Witness", undefined, options);
  const body = turn.querySelector(".text");
  if (msg.lead_in) body.append(el("span", "lead-in", msg.lead_in + " "));
  body.append(el("span", msg.referral ? "quote referral" : "quote", msg.refused || msg.referral ? msg.spoken : msg.quote));
  const badges = el("div", "badges");
  if (msg.refused) badges.append(el("span", "badge", "Not in statement"));
  else {
    badges.append(el("span", "badge", `¶ ${msg.para_no}`));
    if (msg.referral) badges.append(el("span", "badge referral", "Referred, not quoted"));
    if (msg.ocr_corrected) {
      const badge = el("span", "badge ocr", "OCR-corrected");
      badge.title = "The statement's scanned text has OCR errors; the witness reads it corrected, and the original words are highlighted.";
      badges.append(badge);
    }
    if (msg.routed === "doc_id") badges.append(el("span", "badge", "Document reference"));
  }
  turn.append(badges);
  return turn;
}

function markUnit(msg, behavior) {
  if (msg.refused) return;
  current.row = $(`para-${msg.para_no}`);
  if (!current.row) return;
  // A referral quotes nothing, so the whole unit is banded instead of word by word.
  current.row.classList.add(msg.referral ? "referred" : "cited");
  current.row.scrollIntoView({ behavior, block: "center" });
}

function onAudio(msg) {
  const bytes = Uint8Array.from(atob(msg.data), (c) => c.charCodeAt(0));
  const pcm = new Int16Array(bytes.buffer, 0, bytes.length >> 1);
  const buffer = audioCtx.createBuffer(1, pcm.length, msg.sample_rate);
  const ch = buffer.getChannelData(0);
  for (let i = 0; i < pcm.length; i++) ch[i] = pcm[i] / 32768;

  // Hold the quote's audio until its word timings arrive (at most quote_timings_max_wait_ms
  // beyond the end of what is already queued), so the highlight starts with the voice.
  if (msg.part === "quote" && !current.quoteWords && !current.holdReleased) {
    current.heldQuote.push(buffer);
    if (current.holdTimer === null) {
      const queuedFor = current.nextTime === null ? 0 : Math.max(0, current.nextTime - audioCtx.currentTime);
      current.holdTimer = setTimeout(releaseQuote, queuedFor * 1000 + config.quote_timings_max_wait_ms);
    }
    return;
  }
  schedule(buffer);
}

function releaseQuote() {
  if (!current || current.holdReleased) return;
  current.holdReleased = true;
  clearTimeout(current.holdTimer);
  const held = current.heldQuote;
  current.heldQuote = [];
  if (!held.length) return;
  const startAt = Math.max(current.nextTime ?? 0, audioCtx.currentTime + 0.02);
  if (current.nextTime !== null && startAt > current.nextTime) {
    // The pause added after the lead-in; shift the clock so word times still line up.
    const hold = startAt - current.nextTime;
    current.startTime += hold;
    current.nextTime += hold;
    current.quoteHoldMs = hold * 1000;
  }
  held.forEach(schedule);
}

function schedule(buffer) {
  const now = audioCtx.currentTime;
  if (current.startTime === null) {
    current.startTime = now + config.audio_buffer_ms / 1000;
    current.nextTime = current.startTime;
    current.rafId = requestAnimationFrame(tick);
  } else if (current.nextTime < now) {
    // Underrun: the chunk arrived after the previous one finished. Shift the clock so
    // word times still line up with the audio that is actually playing.
    const gap = now - current.nextTime + 0.02;
    current.startTime += gap;
    current.nextTime += gap;
    current.underruns++;
  }
  const src = audioCtx.createBufferSource();
  src.buffer = buffer;
  src.connect(audioCtx.destination);
  src.start(current.nextTime);
  current.nextTime += buffer.duration;
  current.sources.push(src);
}

// Audio time (ms since this answer's audio began) that is reaching the speakers now.
function audioTimeMs() {
  const latency = audioCtx.outputLatency || audioCtx.baseLatency || 0;
  return (audioCtx.currentTime - current.startTime - latency) * 1000;
}

function onWords(msg) {
  current.words = mergeWords(current.words, msg.words);
  if (msg.part === "quote") {
    current.quoteWords = true;
    releaseQuote(); // shifts the clock if the quote was held, so measure lateness after it
  }
  // Words that should already have started are late: record by how much.
  if (current.startTime !== null && msg.words.length) {
    const late = audioTimeMs() - msg.words[0].start;
    if (late > current.lateWordsMs) current.lateWordsMs = late;
  }
  if (msg.part === "quote" && current.row) renderQuote();
}

// The quote is drawn as pieces (words and the gaps between them) that join a continuous
// band as they are spoken; bandEls is kept in band order so tick() can advance through it.
function renderQuote() {
  const text = paragraphs.get(current.answer.para_no);
  const body = current.row.querySelector(".body");
  body.replaceChildren();
  current.bandEls = [];
  for (const piece of quotePieces(text, current.words)) {
    if (piece.band === null) body.append(document.createTextNode(piece.text));
    else {
      const span = el("span", "q pending", piece.text);
      current.bandEls.push({ el: span, band: piece.band });
      body.append(span);
    }
  }
  current.bandEls.sort((a, b) => a.band - b.band);
  current.spokenUpTo = 0;
  current.lastActive = -1;
}

function advanceBand(idx) {
  while (current.spokenUpTo < current.bandEls.length && current.bandEls[current.spokenUpTo].band <= idx) {
    current.bandEls[current.spokenUpTo].el.classList.replace("pending", "spoken");
    current.spokenUpTo++;
  }
}

function settleQuote() {
  for (const { el: e } of current.bandEls) {
    e.classList.remove("pending", "spoken");
    e.classList.add("quoted");
  }
}

function tick() {
  if (!current || current.startTime === null) return;
  const t = audioTimeMs();
  const ended = current.done && audioCtx.currentTime >= current.nextTime;
  const idx = ended ? -1 : activeWordIndex(current.words, t);
  if (idx !== current.lastActive) {
    advanceBand(idx);
    current.lastActive = idx;
  }
  if (ended) finish();
  else current.rafId = requestAnimationFrame(tick);
}

// Same wording as crossexam.costs.describe.
function describeUsage(u) {
  if (!u) return "cost not measured";
  const tts = `TTS ${u.tts_chars} chars` + (u.tts_audio_seconds != null ? `, ${u.tts_audio_seconds.toFixed(1)} s` : "");
  const stt = u.stt_audio_seconds != null ? ` · STT ${u.stt_audio_seconds.toFixed(1)} s` : "";
  const total = u.cost_usd?.total;
  return `LLM ${u.llm_calls} call(s), ${u.llm_input_tokens} in / ${u.llm_output_tokens} out tokens · ${tts}${stt}` +
    ` · cost ${total != null ? "$" + total.toFixed(5) : "not measured"}`;
}

function onDone(msg) {
  current.done = msg;
  if (current.startTime === null) finish(); // no audio arrived
}

function finish() {
  const d = current.done;
  settleQuote();
  const timing = `answer ${d.answer_latency_ms} ms · first audio ${d.question_to_first_audio_ms} ms after question` +
    (d.question_to_quote_audio_ms && d.question_to_quote_audio_ms !== d.question_to_first_audio_ms
      ? ` · quote audio ${d.question_to_quote_audio_ms} ms` : "") +
    (d.cached?.length ? ` (${d.cached.join(", ")} cached)` : "") +
    (current.quoteHoldMs > 0 ? ` · quote held ${Math.round(current.quoteHoldMs)} ms for timings` : "") +
    ` · audio ${(d.duration_ms / 1000).toFixed(1)} s` +
    (current.underruns ? ` · ${current.underruns} audio underrun(s)` : "") +
    (d.missing_words?.length ? ` · no word timings for ${d.missing_words.join(", ")}` : "") +
    (current.lateWordsMs > 0 ? ` · word timings arrived up to ${Math.round(current.lateWordsMs)} ms late` : "") +
    ` · ${describeUsage(d.usage)}`;
  if (params.has("debug")) current.turn?.append(el("div", "timing", timing)); // latency, for development
  window.__lastAnswer = { ...d, underruns: current.underruns, late_words_ms: Math.round(current.lateWordsMs),
    quote_hold_ms: Math.round(current.quoteHoldMs) };
  setStatus("Ready.");
  setBusy(false);
  $("question").focus();
}

function clearHighlight() {
  if (!current) return;
  if (current.rafId) cancelAnimationFrame(current.rafId);
  clearTimeout(current.holdTimer);
  current.sources.forEach((s) => { try { s.stop(); } catch {} });
  if (current.row) {
    current.row.classList.remove("cited", "referred");
    current.row.querySelector(".body").textContent = paragraphs.get(current.answer.para_no);
  }
}

init().catch((e) => setStatus(`Failed to load: ${e.message}`));
