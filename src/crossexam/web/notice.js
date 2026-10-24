// The banner under the header (index.html and examine.html): demo limits, cold starts.

const COLD_START_MS = 1500;

// Shown in #notice; a demo limit gets the video link if one is configured.
export function showNotice(text, { kind = "info", link = null } = {}) {
  const box = document.getElementById("notice");
  box.replaceChildren(document.createTextNode(text));
  if (link) {
    const a = document.createElement("a");
    a.href = link;
    a.textContent = "Watch the video";
    box.append(" ", a, ".");
  }
  box.className = `notice ${kind}`;
  box.hidden = false;
}

export function hideNotice() {
  document.getElementById("notice").hidden = true;
}

// /api/config, telling the visitor the server is waking up if it is slow to answer
// (Render's free plan sleeps after a quiet spell and takes a while to start).
export async function fetchConfig() {
  const timer = setTimeout(() => showNotice(
    "Starting up the server. On the free plan this can take up to a minute after a quiet spell; the page will be ready shortly.",
    { kind: "wait" }), COLD_START_MS);
  try {
    return await (await fetch("/api/config")).json();
  } finally {
    clearTimeout(timer);
    if (document.getElementById("notice").classList.contains("wait")) hideNotice();
  }
}

export function demoLimitText(config) {
  return `${config.demo_limit} The statement can still be read.`;
}
