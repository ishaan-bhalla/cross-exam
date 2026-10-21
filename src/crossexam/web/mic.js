// Spoken questions via streaming speech-to-text. The server returns a WebSocket URL carrying
// a temporary token and the witness's key terms; the API key never reaches the browser.
//
// Capture starts as soon as the mic is clicked, in parallel with fetching the token and opening
// the socket; frames captured before the socket opens are buffered and sent first, so the start
// of the question is not lost. Every final turn since the mic started is joined into one
// question, submitted only after an end of turn followed by submit_grace_ms with no new speech.
//
// startMic(witnessId, callbacks) resolves to { stop }. Callbacks:
//   onListening()          capture has started (the user can speak)
//   onText(text)           the question so far: joined final turns plus the current partial
//   onSubmit(text, info)   the finished question; info.end_of_speech_to_submit_ms, info.log
//   onError(message)
// Every step is logged with ms since the click, to the console and in info.log / window.__micLog.

export async function startMic(witnessId, { onListening, onText, onSubmit, onError }) {
  const t0 = performance.now();
  const log = [];
  window.__micLog = log;
  const event = (name, detail) => {
    const entry = { t_ms: Math.round(performance.now() - t0), event: name, ...(detail ? { detail } : {}) };
    log.push(entry);
    console.debug("[mic]", entry.t_ms, name, detail ?? "");
  };
  event("mic click");

  let stopped = false;
  let ws = null;
  let ctx = null;
  let stream = null;
  let sampleRate = null;
  let graceMs = 0;
  const buffered = [];       // frames captured before the socket opened
  const chunkEnds = [];      // [audio ms at end of frame, performance.now() when captured]
  let capturedMs = 0;
  let firstSent = false;
  const finals = [];         // final turn transcripts, in order
  let lastFinalEndMs = null; // audio time at which the last final turn's last word ended
  let partial = "";
  let graceTimer = null;

  const shown = () => [...finals, partial].filter(Boolean).join(" ");

  function sendFrame(frame) {
    ws.send(frame);
    if (!firstSent) { firstSent = true; event("first audio frame sent"); }
  }

  function submit() {
    if (stopped) return;
    const text = finals.join(" ").trim();
    event("submit", text);
    const captured = lastFinalEndMs === null ? null : chunkEnds.find(([ms]) => ms >= lastFinalEndMs);
    stop();
    onSubmit(text, {
      end_of_speech_to_submit_ms: captured ? Math.round(performance.now() - captured[1]) : null,
      log,
    });
  }

  function onMessage(e) {
    const msg = JSON.parse(e.data);
    if (msg.type === "SpeechStarted") {
      event("speech started");
      if (graceTimer) { clearTimeout(graceTimer); graceTimer = null; event("grace cancelled: speech resumed"); }
      return;
    }
    if (msg.type !== "Turn") {
      event(`server ${msg.type}`, msg.error ?? undefined);
      return;
    }
    if (!msg.end_of_turn) {
      partial = msg.transcript;
      event("partial", partial);
      if (partial.trim() && graceTimer) {
        clearTimeout(graceTimer);
        graceTimer = null;
        event("grace cancelled: speech resumed");
      }
      onText(shown());
      return;
    }
    event("end of turn", msg.transcript);
    partial = "";
    if (msg.transcript.trim()) {
      finals.push(msg.transcript.trim());
      if (msg.words?.length) lastFinalEndMs = msg.words[msg.words.length - 1].end;
    }
    onText(shown());
    if (!finals.length) return; // a turn of noise: keep listening
    if (graceTimer) clearTimeout(graceTimer);
    graceTimer = setTimeout(submit, graceMs);
    event(`waiting ${graceMs} ms for more speech`);
  }

  function stop() {
    if (stopped) return;
    stopped = true;
    if (graceTimer) clearTimeout(graceTimer);
    if (ws && ws.readyState === WebSocket.OPEN) {
      try { ws.send(JSON.stringify({ type: "Terminate" })); } catch {}
      setTimeout(() => ws.close(), 1000);
    } else if (ws) {
      ws.close();
    }
    stream?.getTracks().forEach((t) => t.stop());
    ctx?.close();
  }

  // Token and socket, in parallel with starting capture.
  const session = fetch(`/api/witnesses/${encodeURIComponent(witnessId)}/stt-session`, { method: "POST" })
    .then((r) => r.json())
    .then((s) => {
      if (!s.url) throw new Error(s.detail || "could not start speech-to-text");
      event("token received");
      return s;
    });

  // Capture. The worklet needs the target rate before the token arrives, so it comes from config.
  const config = await (await fetch("/api/config")).json();
  sampleRate = config.stt_sample_rate;
  stream = await navigator.mediaDevices.getUserMedia({
    audio: { channelCount: 1, echoCancellation: true, noiseSuppression: true },
  });
  if (stopped) return { stop };
  try {
    ctx = new AudioContext({ sampleRate }); // the browser resamples the mic
  } catch {
    ctx = new AudioContext(); // the worklet decimates instead
  }
  await ctx.audioWorklet.addModule("pcm-worklet.js");
  const source = ctx.createMediaStreamSource(stream);
  const capture = new AudioWorkletNode(ctx, "pcm-capture", {
    processorOptions: { targetRate: sampleRate, chunkMs: config.stt_chunk_ms },
  });
  const mute = ctx.createGain();
  mute.gain.value = 0; // keeps the graph running without playing the mic back
  source.connect(capture).connect(mute).connect(ctx.destination);
  let firstFrame = true;
  capture.port.onmessage = (e) => {
    if (stopped) return;
    if (firstFrame) { firstFrame = false; event("first audio frame captured"); }
    capturedMs += (e.data.byteLength / 2 / sampleRate) * 1000;
    chunkEnds.push([capturedMs, performance.now()]);
    if (ws && ws.readyState === WebSocket.OPEN) sendFrame(e.data);
    else buffered.push(e.data);
  };
  event("mic capture started");
  onListening();

  session.then((s) => {
    if (stopped) return;
    graceMs = s.submit_grace_ms;
    ws = new WebSocket(s.url);
    ws.binaryType = "arraybuffer";
    ws.onopen = () => {
      const ms = Math.round(buffered.reduce((n, f) => n + f.byteLength / 2, 0) / sampleRate * 1000);
      event("stt socket open", `sending ${buffered.length} buffered frames (${ms} ms of audio)`);
      while (buffered.length) sendFrame(buffered.shift());
    };
    ws.onmessage = onMessage;
    ws.onerror = () => { if (!stopped) { event("stt socket error"); onError("speech-to-text connection failed"); stop(); } };
    ws.onclose = (e) => {
      event("stt socket closed", `${e.code}${e.reason ? " " + e.reason : ""}`);
      if (!stopped && e.code !== 1000) { onError(`speech-to-text closed (${e.code}${e.reason ? ": " + e.reason : ""})`); stop(); }
    };
  }).catch((err) => { if (!stopped) { event("token failed", err.message); onError(err.message); stop(); } });

  return { stop };
}
