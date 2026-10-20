// Spoken questions via streaming speech-to-text. The server returns a WebSocket URL carrying
// a temporary token and the witness's key terms; the API key never reaches the browser.
//
// startMic resolves to { stop } once listening. Callbacks:
//   onPartial(text)          live transcript of the current turn
//   onFinal(text, timing)    the finished turn; timing.end_of_speech_to_final_ms is the time from
//                            when the last spoken word was captured to the final transcript
//   onError(message)
export async function startMic(witnessId, { onPartial, onFinal, onError }) {
  const session = await (await fetch(`/api/witnesses/${encodeURIComponent(witnessId)}/stt-session`, { method: "POST" })).json();
  if (!session.url) throw new Error(session.detail || "could not start speech-to-text");

  const stream = await navigator.mediaDevices.getUserMedia({
    audio: { channelCount: 1, echoCancellation: true, noiseSuppression: true },
  });
  let ctx;
  try {
    ctx = new AudioContext({ sampleRate: session.sample_rate }); // the browser resamples the mic
  } catch {
    ctx = new AudioContext(); // the worklet decimates instead
  }
  await ctx.audioWorklet.addModule("pcm-worklet.js");
  const source = ctx.createMediaStreamSource(stream);
  const capture = new AudioWorkletNode(ctx, "pcm-capture", {
    processorOptions: { targetRate: session.sample_rate, chunkMs: session.chunk_ms },
  });
  const mute = ctx.createGain();
  mute.gain.value = 0; // keeps the graph running without playing the mic back
  source.connect(capture).connect(mute).connect(ctx.destination);

  // Wall-clock time at which each chunk's audio ended, to time the end of speech.
  const chunkEnds = []; // [audio ms at end of chunk, performance.now() when captured]
  let sentMs = 0;
  let stopped = false;

  const ws = new WebSocket(session.url);
  ws.binaryType = "arraybuffer";
  capture.port.onmessage = (e) => {
    if (ws.readyState !== WebSocket.OPEN) return;
    ws.send(e.data);
    sentMs += (e.data.byteLength / 2 / session.sample_rate) * 1000;
    chunkEnds.push([sentMs, performance.now()]);
  };
  ws.onmessage = (e) => {
    const msg = JSON.parse(e.data);
    if (msg.type !== "Turn") return;
    if (!msg.end_of_turn) {
      onPartial(msg.transcript);
      return;
    }
    if (!msg.transcript.trim()) return; // a turn of noise: keep listening
    const lastEnd = msg.words?.length ? msg.words[msg.words.length - 1].end : null;
    const captured = lastEnd === null ? null : chunkEnds.find(([ms]) => ms >= lastEnd);
    onFinal(msg.transcript, {
      end_of_speech_to_final_ms: captured ? Math.round(performance.now() - captured[1]) : null,
      final_at: performance.now(),
    });
  };
  ws.onerror = () => { if (!stopped) onError("speech-to-text connection failed"); };
  ws.onclose = (e) => { if (!stopped && e.code !== 1000) onError(`speech-to-text closed (${e.code}${e.reason ? ": " + e.reason : ""})`); };
  await new Promise((resolve, reject) => {
    ws.addEventListener("open", resolve, { once: true });
    ws.addEventListener("close", () => reject(new Error("speech-to-text connection refused")), { once: true });
  });

  return {
    stop() {
      if (stopped) return;
      stopped = true;
      try { ws.send(JSON.stringify({ type: "Terminate" })); } catch {}
      setTimeout(() => ws.close(), 1000);
      stream.getTracks().forEach((t) => t.stop());
      ctx.close();
    },
  };
}
