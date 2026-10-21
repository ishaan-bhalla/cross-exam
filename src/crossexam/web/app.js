import { activeWordIndex, mergeWords, quotePieces } from "./sync.js";
import { startMic } from "./mic.js";

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

function el(tag, cls, text) {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (text !== undefined) e.textContent = text;
  return e;
}

async function init() {
  config = await (await fetch("/api/config")).json();
  const list = await (await fetch("/api/witnesses")).json();
  const witness = list.find((w) => w.id === params.get("witness")) || list[0];
  witnessId = witness.id;
  $("witness-role").textContent = witness.role_label;
  $("witness-name").textContent = witness.name;
  document.title = `Cross-examination: ${witness.name}`;

  const statement = await (await fetch(`/api/witnesses/${witness.id}/statement`)).json();
  const box = $("statement");
  for (const p of statement) {
    paragraphs.set(p.para_no, p.text);
    const row = el("div", "para");
    row.id = `para-${p.para_no}`;
    row.append(el("span", "no", p.para_no), el("span", "body", p.text));
    box.append(row);
  }
  connect(witness.id);
}

function connect(witnessId) {
  ws = new WebSocket(`${location.protocol === "https:" ? "wss" : "ws"}://${location.host}/ws/examine?witness=${encodeURIComponent(witnessId)}`);
  ws.onopen = () => setStatus("Ready.");
  ws.onclose = () => { setStatus("Disconnected. Reload to reconnect."); setBusy(true); };
  ws.onmessage = (e) => handle(JSON.parse(e.data));
}

function setStatus(text) { $("status").textContent = text; }
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
  $("mic-button").textContent = "🎤 Speak";
}

$("mic-button").addEventListener("click", async () => {
  if (mic) { stopMic(); setStatus("Ready."); return; }
  ensureAudio();
  $("mic-button").setAttribute("aria-pressed", "true");
  $("mic-button").textContent = "■ Stop";
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
  const turn = addTurn("counsel", "Counsel", text);
  const voice = spoken;
  spoken = null;
  if (voice) {
    turn.append(el("div", "timing", `asked by voice · end of speech to submitted ${voice.end_of_speech_to_submit_ms ?? "?"} ms`));
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
  ws.send(JSON.stringify({ type: "question", text, spoken: Boolean(voice) }));
});

function addTurn(cls, who, text) {
  const turn = el("div", `turn ${cls}`);
  const head = el("div", "who", who);
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
  turn.scrollIntoView({ block: "end" });
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
  const turn = addTurn("witness", "Witness");
  const body = turn.querySelector(".text");
  if (msg.lead_in) body.append(el("span", "lead-in", msg.lead_in + " "));
  body.append(el("span", "quote", msg.refused ? msg.spoken : msg.quote));
  const badges = el("div", "badges");
  if (msg.refused) badges.append(el("span", "badge", "Not in statement"));
  else {
    badges.append(el("span", "badge", `¶ ${msg.para_no}`));
    if (!msg.verified) badges.append(el("span", "badge warn", "Quote not verified"));
    if (msg.routed === "doc_id") badges.append(el("span", "badge", "Document reference"));
  }
  turn.append(badges);
  current.turn = turn;
  setStatus("Speaking…");

  if (!msg.refused) {
    current.row = $(`para-${msg.para_no}`);
    current.row.classList.add("cited");
    current.row.scrollIntoView({ behavior: "smooth", block: "center" });
  }
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
    (current.lateWordsMs > 0 ? ` · word timings arrived up to ${Math.round(current.lateWordsMs)} ms late` : "");
  current.turn?.append(el("div", "timing", timing));
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
    current.row.classList.remove("cited");
    current.row.querySelector(".body").textContent = paragraphs.get(current.answer.para_no);
  }
}

init().catch((e) => setStatus(`Failed to load: ${e.message}`));
