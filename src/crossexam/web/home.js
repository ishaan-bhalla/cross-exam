// Front page, upload progress and the confirm step. Views are sections of index.html;
// ?upload=<id> resumes an upload (progress or confirm) after a reload.
import { findSource, roleLabel, sentenceStart, titleCase, uploadError } from "./profile-ui.js";
import { demoLimitText, fetchConfig, showNotice } from "./notice.js";

const $ = (id) => document.getElementById(id);
const POLL_MS = 400;
// Progress rows, in order; "ocr" only appears if a page needed it.
const STEPS = [
  { stage: "uploading", label: "Uploading" },
  { stage: "reading", label: "Reading the pages" },
  { stage: "ocr", label: "Reading scanned pages (OCR)", optional: true },
  { stage: "parsing", label: "Finding the numbered paragraphs" },
  { stage: "indexing", label: "Indexing the statement" },
  { stage: "profiling", label: "Drafting the witness profile" },
];
const VOICE_STEP = { stage: "voice", label: "Preparing the witness's voice" };

let config = null;
let upload = null; // the upload record being shown
let units = [];
let labelEdited = false;
let sawOcr = false;

function el(tag, cls, text) {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (text !== undefined) e.textContent = text;
  return e;
}

function show(view) {
  for (const v of ["home", "progress", "confirm"]) $(`view-${v}`).hidden = v !== view;
  const title = { progress: "progress-title", confirm: "confirm-title" }[view];
  if (title) $(title).focus();
  document.title = { home: "Cross-examine a witness statement", progress: "Reading the statement · Cross-examine",
                     confirm: "Check the witness · Cross-examine" }[view];
}

function setUrl(id) {
  history.replaceState(null, "", id ? `/?upload=${encodeURIComponent(id)}` : "/");
}

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

// ---------------------------------------------------------------- front page

async function initHome() {
  $("max-mb").textContent = config.upload_max_mb;
  $("ttl-note").textContent = `An upload you leave unused for ${config.upload_ttl_minutes} minutes is discarded.`;
  const witnesses = await (await fetch("/api/witnesses")).json();
  const example = witnesses.find((w) => !w.uploaded);
  if (example) {
    $("example-role").textContent = example.role_label;
    $("example-name").textContent = example.name;
    for (const a of [$("example-link"), $("nav-example")]) a.href = `examine.html?witness=${encodeURIComponent(example.id)}`;
  }
  await renderOpenUploads();
  startSpecimen();
}

// The specimen's quote is marked word by word, as the examination marks a spoken quote; each
// word carries the space before it, so the band is continuous.
function startSpecimen() {
  const quote = $("specimen-quote");
  const words = quote.textContent.trim().split(/\s+/);
  quote.replaceChildren(...words.map((w, i) => {
    const span = el("span", "sweep", i ? ` ${w}` : w);
    span.style.setProperty("--i", String(i));
    return span;
  }));
}

async function renderOpenUploads() {
  const mine = await (await fetch("/api/uploads")).json();
  $("open-uploads").hidden = !mine.length;
  $("open-list").replaceChildren(...mine.map((u) => {
    const li = el("li");
    const what = el("div", "what");
    what.append(el("div", null, titleCase(u.name)), el("div", "file", `${u.filename} · ${u.stage === "confirmed" ? "confirmed" : "not yet confirmed"}`));
    const go = el("a", "btn small", u.stage === "confirmed" ? "Examine" : "Continue");
    go.href = u.stage === "confirmed" ? `examine.html?witness=${encodeURIComponent(u.id)}` : `/?upload=${encodeURIComponent(u.id)}`;
    go.setAttribute("aria-label", `${go.textContent} ${titleCase(u.name)}`);
    const remove = el("button", "btn small", "Remove");
    remove.type = "button";
    remove.setAttribute("aria-label", `Remove ${titleCase(u.name)}`);
    remove.addEventListener("click", async () => {
      await fetch(`/api/uploads/${encodeURIComponent(u.id)}`, { method: "DELETE" });
      await renderOpenUploads();
      $("file").focus();
    });
    li.append(what, go, remove);
    return li;
  }));
}

function showUploadError(text) {
  $("upload-error").hidden = !text;
  $("upload-error").textContent = text || "";
}

function pickFile(file) {
  showUploadError("");
  if (!file) return;
  if (file.size > config.upload_max_mb * 1024 * 1024) return showUploadError(uploadError(413, null, config.upload_max_mb));
  if (!/\.pdf$/i.test(file.name) && file.type !== "application/pdf") return showUploadError(uploadError(415));
  send(file);
}

// XMLHttpRequest rather than fetch, for upload progress.
function send(file) {
  sawOcr = false;
  upload = { filename: file.name, stage: "uploading" };
  renderProgress(upload);
  show("progress");
  const xhr = new XMLHttpRequest();
  xhr.open("POST", "/api/witnesses/upload");
  xhr.upload.onprogress = (e) => {
    if (e.lengthComputable) renderProgress({ ...upload, page: Math.round(e.loaded / 1024), pages: Math.round(e.total / 1024), uploadBytes: true });
  };
  xhr.onload = () => {
    let body = {};
    try { body = JSON.parse(xhr.responseText); } catch {}
    if (xhr.status !== 202) {
      show("home");
      showUploadError(uploadError(xhr.status, body.detail, config.upload_max_mb));
      $("file").value = "";
      return;
    }
    setUrl(body.id);
    follow(body.id);
  };
  xhr.onerror = () => { show("home"); showUploadError(uploadError(0)); };
  const form = new FormData();
  form.append("file", file);
  xhr.send(form);
}

// ---------------------------------------------------------------- progress

function renderProgress(u) {
  $("progress-title").textContent = u.stage === "voice" ? "Preparing the examination" : "Reading the statement";
  $("progress-file").textContent = u.filename || "";
  if (u.stage === "ocr") sawOcr = true;
  const steps = u.stage === "voice" ? [...STEPS, VOICE_STEP] : STEPS;
  const visible = steps.filter((s) => !s.optional || sawOcr || (u.ocr_pages || []).length);
  const done = u.stage === "voice" ? visible.length - 1 : visible.findIndex((s) => s.stage === u.stage);
  $("stages").replaceChildren(...visible.map((s, i) => {
    const li = el("li", i < done || u.stage === "ready" ? "done" : i === done ? "now" : "");
    if (i === done) li.setAttribute("aria-current", "step");
    li.append(el("span", "dot"), el("span", null, s.label));
    let detail = "";
    if (i === done && u.uploadBytes) detail = `${u.page} of ${u.pages} KB`;
    else if (i === done && u.message && u.page) detail = u.message;
    li.append(el("span", "detail", detail));
    return li;
  }));
  const fraction = u.page && u.pages ? u.page / u.pages : 0;
  $("meter").style.width = `${Math.round(((Math.max(done, 0) + fraction) / visible.length) * 100)}%`;
  $("progress-live").textContent = u.uploadBytes ? "Uploading" : (u.message || "");
}

function progressFailed(text) {
  $("progress-error").hidden = false;
  $("progress-error").textContent = text;
  $("progress-actions").hidden = false;
}

async function follow(id) {
  $("progress-error").hidden = true;
  $("progress-actions").hidden = true;
  show("progress");
  for (;;) {
    const res = await fetch(`/api/uploads/${encodeURIComponent(id)}`);
    if (!res.ok) { setUrl(null); return progressFailed("This upload is no longer available. Please upload the statement again."); }
    upload = await res.json();
    if (upload.stage === "expired") { setUrl(null); return progressFailed(upload.message); }
    if (upload.stage === "error") return progressFailed(upload.message);
    if (upload.stage === "confirmed") { location.href = `examine.html?witness=${encodeURIComponent(upload.witness_id)}`; return; }
    if (upload.stage === "ready") return showConfirm();
    renderProgress(upload);
    await sleep(POLL_MS);
  }
}

// ---------------------------------------------------------------- confirm

async function showConfirm() {
  const res = await fetch(`/api/witnesses/${encodeURIComponent(upload.witness_id)}/statement`);
  if (!res.ok) return progressFailed(res.status === 410 ? (await res.json()).detail : "The statement could not be loaded.");
  units = await res.json();
  renderPreview();
  const p = upload.profile || {};
  $("f-name").value = titleCase(p.name || upload.filename.replace(/\.pdf$/i, ""));
  $("roles").replaceChildren();
  for (const r of p.roles || []) addRole(r);
  labelEdited = false;
  $("f-label").value = sentenceStart(p.role_label || roleLabel(currentRoles(), p.organisation) || "Witness");
  renderDropped(p.dropped || []);
  await renderVoices();
  refreshSources();
  show("confirm");
}

function renderPreview() {
  const doc = $("preview-doc");
  doc.replaceChildren();
  if (upload.preamble) {
    const t = el("div", "titlepage", upload.preamble.trim());
    t.id = "pv-preamble";
    doc.append(t);
  }
  let section = null;
  for (const u of units) {
    if (u.section && u.section !== section) {
      doc.append(el("div", "section-heading", u.section));
      section = u.section;
    }
    const row = el("div", "para");
    row.id = `pv-${u.para_no}`;
    if (typeof u.para_no === "string") row.classList.add("sub");
    row.append(el("span", "no", u.para_no), el("span", "body", u.text));
    doc.append(row);
  }
}

function addRole(r = {}) {
  const node = $("role-template").content.firstElementChild.cloneNode(true);
  for (const input of node.querySelectorAll("input")) {
    input.value = r[input.dataset.k] || "";
    const id = `r-${crypto.randomUUID().slice(0, 8)}-${input.dataset.k}`;
    input.id = id;
    input.closest(".field").querySelector("label").htmlFor = id;
    input.closest(".field").querySelector(".source").dataset.for = id;
  }
  node.querySelector(".remove").addEventListener("click", () => {
    const next = node.nextElementSibling || node.previousElementSibling;
    node.remove();
    renumberRoles();
    onRolesChanged();
    (next?.querySelector("input") || $("f-label")).focus();
  });
  $("roles").append(node);
  renumberRoles();
}

function renumberRoles() {
  const roles = [...$("roles").children];
  roles.forEach((node, i) => {
    node.querySelector("legend").textContent = `Role ${i + 1}`;
    node.querySelector(".remove").textContent = `Remove role ${i + 1}`;
  });
  $("no-roles").hidden = roles.length > 0;
}

function currentRoles() {
  return [...$("roles").children].map((node) => Object.fromEntries(
    [...node.querySelectorAll("input")].map((i) => [i.dataset.k, i.value.trim() || null])));
}

function onRolesChanged() {
  if (!labelEdited) $("f-label").value = roleLabel(currentRoles().filter((r) => r.title), upload.profile?.organisation) || $("f-label").value;
  refreshSources();
}

function renderDropped(dropped) {
  $("dropped-box").hidden = !dropped.length;
  const names = { name: "Name", organisation: "Organisation", title: "Title", from: "From", to: "To" };
  $("dropped").replaceChildren(...dropped.map((d) => el("li", null, `${names[d.field.split(".").pop()] || d.field}: “${d.value}”`)));
}

// Each "Source" link scrolls the preview to where its field's current value appears.
function refreshSources() {
  for (const link of document.querySelectorAll("#confirm-form .source")) {
    const input = $(link.dataset.for);
    const where = input && findSource(input.value, upload.preamble, units);
    link.classList.toggle("missing", !where);
    link.textContent = where ? "Source" : "Not found";
    link.setAttribute("aria-label", where ? `Source of the ${labelOf(input)}` : `Not found: the ${labelOf(input)} is not in the statement`);
    link.setAttribute("aria-disabled", String(!where));
  }
}

function labelOf(input) {
  const label = input.closest(".field").querySelector("label").textContent.split("(")[0].trim();
  const role = input.closest("fieldset.role")?.querySelector("legend").textContent;
  return role ? `${role} ${label.toLowerCase()}` : label.toLowerCase();
}

function showSource(input) {
  const where = findSource(input.value, upload.preamble, units);
  if (!where) return;
  for (const m of document.querySelectorAll("#preview-doc .marked")) {
    m.classList.remove("marked", "flash");
    const body = m.querySelector(".body") || m;
    body.textContent = m.dataset.text;
  }
  const row = $(`pv-${where.key}`);
  const body = row.querySelector(".body") || row;
  const text = row.dataset.text ?? body.textContent;
  row.dataset.text = text;
  const mark = el("mark", null, text.slice(where.start, where.end));
  body.replaceChildren(text.slice(0, where.start), mark, text.slice(where.end));
  row.classList.add("marked", "flash");
  const doc = $("preview-doc");
  doc.scrollTo({ top: row.offsetTop - doc.offsetTop - 40, behavior: matchMedia("(prefers-reduced-motion: reduce)").matches ? "auto" : "smooth" });
  if (matchMedia("(max-width: 900px)").matches) $("preview").scrollIntoView({ block: "start" });
}

async function renderVoices() {
  const { voices, default: fallback } = await (await fetch("/api/voices")).json();
  const box = $("voices");
  box.querySelectorAll(".voice").forEach((v) => v.remove());
  let audio = null;
  for (const v of voices) {
    const row = el("div", "voice");
    const input = el("input");
    Object.assign(input, { type: "radio", name: "voice", value: v.id, id: `voice-${v.id}`, checked: v.id === fallback });
    const label = el("label");
    label.htmlFor = input.id;
    label.append(v.id, el("small", null, v.accent ? `${v.accent} English` : ""));
    const play = el("button", "play");
    play.type = "button";
    play.setAttribute("aria-label", `Play a sample of ${v.id}`);
    play.setAttribute("aria-pressed", "false");
    play.innerHTML = '<svg width="12" height="12" viewBox="0 0 12 12" aria-hidden="true"><path d="M3 1.5v9l7-4.5z" fill="currentColor"/></svg>';
    play.addEventListener("click", () => {
      const playing = play.getAttribute("aria-pressed") === "true";
      audio?.pause();
      box.querySelectorAll(".play").forEach((b) => b.setAttribute("aria-pressed", "false"));
      if (playing) return;
      audio = new Audio(`/api/voices/${encodeURIComponent(v.id)}/sample`);
      play.setAttribute("aria-pressed", "true");
      audio.addEventListener("ended", () => play.setAttribute("aria-pressed", "false"));
      audio.play().catch(() => play.setAttribute("aria-pressed", "false"));
    });
    row.append(input, label, play);
    box.append(row);
  }
}

function confirmError(text) {
  $("confirm-error").hidden = !text;
  $("confirm-error").textContent = text || "";
}

async function submitConfirm(e) {
  e.preventDefault();
  confirmError("");
  const form = $("confirm-form");
  const empty = [...form.querySelectorAll("input[required]")].find((i) => !i.value.trim());
  if (empty) { confirmError(`Fill in ${labelOf(empty)}, or remove that role.`); empty.focus(); return; }
  const body = { name: $("f-name").value.trim(), role_label: $("f-label").value.trim(),
                 roles: currentRoles(), voice: form.elements.voice.value };
  $("confirm-button").disabled = true;
  const res = await fetch(`/api/witnesses/${encodeURIComponent(upload.witness_id)}/confirm`, {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
  $("confirm-button").disabled = false;
  if (res.status === 410) return confirmError((await res.json()).detail);
  if (!res.ok) return confirmError("The witness could not be confirmed. Check the fields and try again.");
  follow(upload.id);
}

// ---------------------------------------------------------------- wiring

$("file").addEventListener("change", (e) => pickFile(e.target.files[0]));
const zone = $("dropzone");
for (const type of ["dragenter", "dragover"]) zone.addEventListener(type, (e) => { e.preventDefault(); zone.classList.add("over"); });
for (const type of ["dragleave", "drop"]) zone.addEventListener(type, () => zone.classList.remove("over"));
zone.addEventListener("drop", (e) => { e.preventDefault(); pickFile(e.dataTransfer.files[0]); });
// A file dropped anywhere else on the page would otherwise be opened by the browser.
for (const type of ["dragover", "drop"]) window.addEventListener(type, (e) => e.preventDefault());

$("confirm-form").addEventListener("input", (e) => {
  if (e.target.id === "f-label") labelEdited = true;
  if (e.target.closest("#roles")) onRolesChanged();
  else refreshSources();
});
$("confirm-form").addEventListener("click", (e) => {
  const link = e.target.closest(".source");
  if (!link) return;
  e.preventDefault();
  if (!link.classList.contains("missing")) showSource($(link.dataset.for));
});
$("confirm-form").addEventListener("submit", submitConfirm);
$("discard").addEventListener("click", async () => {
  await fetch(`/api/uploads/${encodeURIComponent(upload.id)}`, { method: "DELETE" });
  location.href = "/";
});

async function init() {
  config = await fetchConfig();
  const id = new URLSearchParams(location.search).get("upload");
  await initHome();
  if (config.demo_limit) {
    // The example witness's statement can still be read; nothing that spends credit can start.
    showNotice(demoLimitText(config), { kind: "limit", link: config.demo_video_url });
    $("file").disabled = true;
    $("dropzone").classList.add("disabled");
    $("drop-hint").textContent = "Uploads are paused for today.";
  }
  if (id) follow(id);
}

init();
