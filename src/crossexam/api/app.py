"""Web app: serves the frontend from web_dir, the witness API and the examination WebSocket.

WebSocket /ws/examine?witness={id}. Client sends {"type": "question", "text"}; the server replies
  {"type": "answer", para_no, quote, lead_in, spoken, verified, refused, routed}
  {"type": "audio", "part": "lead_in" | "quote" | "refusal", "data": base64 PCM16, "sample_rate"}
                                                                  (repeated, in play order)
  {"type": "words", "part": ..., "words": [{word, start, end, span}]}
  {"type": "done", answer_latency_ms, question_to_first_audio_ms, question_to_quote_audio_ms,
   tts_wait_ms, tts_session_opens, time_to_first_audio_ms, duration_ms, cached: [parts from cache],
   missing_words: [parts whose word timings never arrived]}
or {"type": "error", "message"}. Word start/end are ms from the start of this answer's audio
(before any hold the browser adds); span is [start, end] character offsets into paragraph
para_no's text, or None (not highlighted).

Latency: one TTS session per examination is opened when the WebSocket connects and reused
for every answer (tts.PersistentSession), so no answer waits for a connection and
questioning does not hit the account's ~5 session opens per 30 s. Lead-ins and refusals are
fixed phrases, pre-synthesised at startup (api.phrases) and sent the moment the answer is
known; the quote is synthesised live. Leading silence is trimmed from cached phrases and
live audio, with word timings shifted to match. The browser holds quote audio until the
quote's word timings arrive, for at most quote_timings_max_wait_ms.
"""
import asyncio
import base64
import time
from contextlib import asynccontextmanager
from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.staticfiles import StaticFiles
from crossexam.api import phrases, witnesses
from crossexam.clients import tts
from crossexam.config import get_settings
from crossexam.witness.align import align_words
from crossexam.witness.answer import answer, fixed_phrases, spoken_quote


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Parse statements, load the embedding model and pre-synthesise fixed phrases
    # before the first question.
    for w in witnesses.list_witnesses():
        await asyncio.to_thread(witnesses.retriever, w["id"])
    texts = fixed_phrases()
    for voice in sorted({w["voice"] for w in witnesses.list_witnesses()}):
        started = time.perf_counter()
        counts = await phrases.warm(voice, texts)
        print(f"phrases for voice {voice!r}: {counts} in {time.perf_counter() - started:.1f}s")
    yield


app = FastAPI(lifespan=lifespan)


@app.get("/api/config")
def config():
    s = get_settings()
    return {"audio_buffer_ms": s.ui_audio_buffer_ms, "quote_timings_max_wait_ms": s.quote_timings_max_wait_ms}


@app.get("/api/witnesses")
def list_witnesses():
    return witnesses.list_witnesses()


@app.get("/api/witnesses/{witness_id}/statement")
def statement(witness_id: str):
    if not witnesses.get_witness(witness_id):
        raise HTTPException(404, "unknown witness")
    return [{"para_no": p["para_no"], "page": p["page"], "text": p["text"]}
            for p in witnesses.statement(witness_id)]


class AudioOut:
    """Sends audio in play order and keeps the answer's timeline position (ms)."""

    def __init__(self, ws: WebSocket, received: float):
        self.ws, self.received = ws, received
        self.position_ms = 0.0
        self.first_audio_ms = None

    async def send(self, pcm: bytes, sample_rate: int, part: str):
        if not pcm:
            return
        if self.first_audio_ms is None:
            self.first_audio_ms = (time.perf_counter() - self.received) * 1000
        await self.ws.send_json({"type": "audio", "part": part, "data": base64.b64encode(pcm).decode(),
                                 "sample_rate": sample_rate})
        self.position_ms += len(pcm) / 2 / sample_rate * 1000

    async def silence(self, ms: float, sample_rate: int, part: str):
        await self.send(bytes(2 * int(sample_rate * ms / 1000)), sample_rate, part)

    async def words(self, part: str, words: list[dict], offset_ms: float):
        shifted = [{**w, "start": max(0, round(w["start"] + offset_ms)), "end": max(0, round(w["end"] + offset_ms))}
                   for w in words]
        await self.ws.send_json({"type": "words", "part": part, "words": shifted})


async def _examine(ws: WebSocket, witness: dict, question: str, tts_conn: tts.PersistentSession):
    s = get_settings()
    received = time.perf_counter()
    voice = witness["voice"]
    result = await asyncio.to_thread(answer, question, witnesses.retriever(witness["id"]), witness["name"])
    answer_latency_ms = (time.perf_counter() - received) * 1000
    refused = result["para_no"] is None
    await ws.send_json({"type": "answer", "para_no": result["para_no"], "quote": result["quote"],
                        "lead_in": result["lead_in"], "spoken": result["spoken"],
                        "verified": result["verified"], "refused": refused, "routed": result["routed"]})
    out = AudioOut(ws, received)
    cached, live = [], []  # live: [(part, text)]

    if refused:
        phrase = phrases.get(voice, result["spoken"])
        if phrase:
            await out.send(phrase.pcm, phrase.sample_rate, "refusal")
            await out.words("refusal", phrase.words, 0)
            cached.append("refusal")
        else:
            live = [("refusal", result["spoken"])]
    else:
        lead = phrases.get(voice, result["lead_in"]) if result["lead_in"] else None
        if lead:
            await out.send(lead.pcm, lead.sample_rate, "lead_in")
            await out.words("lead_in", lead.words, 0)
            await out.silence(s.tts_lead_in_gap_ms, lead.sample_rate, "lead_in")
            cached.append("lead_in")
        elif result["lead_in"]:
            live.append(("lead_in", result["lead_in"]))
        live.append(("quote", spoken_quote(result["quote"])))

    tts_wait_ms = time_to_first_audio_ms = quote_audio_ms = None
    missing_words = []
    if live:
        wait_started = time.perf_counter()
        session = await tts_conn.ensure()  # normally already open: no wait
        tts_wait_ms = (time.perf_counter() - wait_started) * 1000
        paragraph = None if refused else next(
            p["text"] for p in witnesses.statement(witness["id"]) if p["para_no"] == result["para_no"])
        trimmer = tts.SilenceTrimmer(session.sample_rate)
        live_start_ms = out.position_ms
        async for event in tts_conn.generate([text for _, text in live]):
            if event["type"] == "audio":
                part = live[event["part"]][0]
                pcm = trimmer.feed(event["pcm"])
                if pcm and quote_audio_ms is None and part == "quote":
                    quote_audio_ms = (time.perf_counter() - received) * 1000
                await out.send(pcm, session.sample_rate, part)
            elif event["type"] == "words":
                part = live[event["part"]][0]
                words = event["words"]
                words = align_words(words, result["quote"], paragraph) if part == "quote" else \
                    [{**w, "span": None} for w in words]
                await out.words(part, words, live_start_ms - trimmer.trimmed_ms)
            elif event["type"] == "done":
                time_to_first_audio_ms = round(event["time_to_first_audio_ms"])
                missing_words = [live[i][0] for i in event["missing_words"]]
    if quote_audio_ms is None and not refused:
        quote_audio_ms = out.first_audio_ms

    await ws.send_json({"type": "done", "answer_latency_ms": round(answer_latency_ms),
                        "question_to_first_audio_ms": round(out.first_audio_ms or 0),
                        "question_to_quote_audio_ms": round(quote_audio_ms) if quote_audio_ms else None,
                        "tts_wait_ms": round(tts_wait_ms) if tts_wait_ms is not None else None,
                        "tts_session_opens": tts_conn.opens,
                        "time_to_first_audio_ms": time_to_first_audio_ms,
                        "duration_ms": round(out.position_ms), "cached": cached,
                        "missing_words": missing_words})


@app.websocket("/ws/examine")
async def examine(ws: WebSocket, witness: str):
    w = witnesses.get_witness(witness)
    await ws.accept()
    if not w:
        await ws.send_json({"type": "error", "message": f"unknown witness {witness!r}"})
        await ws.close()
        return
    tts_conn = tts.PersistentSession(w["voice"])
    opening = asyncio.create_task(tts_conn.ensure())  # ready before the first answer
    opening.add_done_callback(lambda t: t.cancelled() or t.exception())  # retried on first use
    try:
        while True:
            msg = await ws.receive_json()
            if msg.get("type") != "question" or not str(msg.get("text", "")).strip():
                await ws.send_json({"type": "error", "message": "expected {\"type\": \"question\", \"text\": ...}"})
                continue
            try:
                await _examine(ws, w, msg["text"].strip(), tts_conn)
            except WebSocketDisconnect:
                raise
            except Exception as e:
                await ws.send_json({"type": "error", "message": f"{type(e).__name__}: {e}"})
    except WebSocketDisconnect:
        pass
    finally:
        opening.cancel()
        await tts_conn.close()


app.mount("/", StaticFiles(directory=get_settings().resolve(get_settings().web_dir), html=True), name="web")
