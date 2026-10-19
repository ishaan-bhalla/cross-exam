"""Web app: serves the frontend from web_dir, the witness API and the examination WebSocket.

WebSocket /ws/examine?witness={id}. Client sends {"type": "question", "text"}; the server replies
  {"type": "answer", para_no, quote, lead_in, spoken, verified, refused, routed}
  {"type": "audio", "data": base64 PCM16, "sample_rate"}             (repeated, as generated)
  {"type": "words", "part": "lead_in" | "quote" | "refusal", "words": [{word, start, end, span}]}
  {"type": "done", time_to_first_audio_ms, answer_latency_ms, question_to_first_audio_ms, duration_ms}
or {"type": "error", "message"}. Word start/end are ms from the start of this answer's audio;
span is [start, end] character offsets into paragraph para_no's text, or None (not highlighted).
The lead-in and the quote are synthesized as separate parts so that word timings for the quote
arrive before the quote starts playing.
"""
import asyncio
import base64
import time
from contextlib import asynccontextmanager
from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.staticfiles import StaticFiles
from crossexam.api import witnesses
from crossexam.clients import tts
from crossexam.config import get_settings
from crossexam.witness.align import align_words
from crossexam.witness.answer import answer, spoken_quote


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Parse statements and load the embedding model before the first question.
    for w in witnesses.list_witnesses():
        await asyncio.to_thread(witnesses.retriever, w["id"])
    yield


app = FastAPI(lifespan=lifespan)


@app.get("/api/config")
def config():
    return {"audio_buffer_ms": get_settings().ui_audio_buffer_ms}


@app.get("/api/witnesses")
def list_witnesses():
    return witnesses.list_witnesses()


@app.get("/api/witnesses/{witness_id}/statement")
def statement(witness_id: str):
    if not witnesses.get_witness(witness_id):
        raise HTTPException(404, "unknown witness")
    return [{"para_no": p["para_no"], "page": p["page"], "text": p["text"]}
            for p in witnesses.statement(witness_id)]


async def _examine(ws: WebSocket, witness: dict, question: str):
    received = time.perf_counter()
    result = await asyncio.to_thread(answer, question, witnesses.retriever(witness["id"]), witness["name"])
    answer_latency_ms = (time.perf_counter() - received) * 1000
    refused = result["para_no"] is None
    await ws.send_json({"type": "answer", "para_no": result["para_no"], "quote": result["quote"],
                        "lead_in": result["lead_in"], "spoken": result["spoken"],
                        "verified": result["verified"], "refused": refused, "routed": result["routed"]})

    if refused:
        parts = [("refusal", result["spoken"])]
    else:
        parts = [("lead_in", result["lead_in"]), ("quote", spoken_quote(result["quote"]))]
        parts = [(kind, text) for kind, text in parts if text]
    paragraph = None if refused else next(
        p["text"] for p in witnesses.statement(witness["id"]) if p["para_no"] == result["para_no"])

    sample_rate = first_audio_at = None
    async for event in tts.stream([text for _, text in parts], witness["voice"]):
        if event["type"] == "begin":
            sample_rate = event["sample_rate"]
        elif event["type"] == "audio":
            if first_audio_at is None:
                first_audio_at = time.perf_counter()
            await ws.send_json({"type": "audio", "data": base64.b64encode(event["pcm"]).decode(),
                                "sample_rate": sample_rate})
        elif event["type"] == "words":
            kind = parts[event["flush_id"]][0]
            words = event["words"]
            if kind == "quote":
                words = align_words(words, result["quote"], paragraph)
            else:
                words = [{**w, "span": None} for w in words]
            await ws.send_json({"type": "words", "part": kind, "words": words})
        elif event["type"] == "done":
            await ws.send_json({"type": "done", "time_to_first_audio_ms": round(event["time_to_first_audio_ms"]),
                                "answer_latency_ms": round(answer_latency_ms),
                                "question_to_first_audio_ms": round((first_audio_at - received) * 1000),
                                "duration_ms": event["duration_ms"]})


@app.websocket("/ws/examine")
async def examine(ws: WebSocket, witness: str):
    w = witnesses.get_witness(witness)
    await ws.accept()
    if not w:
        await ws.send_json({"type": "error", "message": f"unknown witness {witness!r}"})
        await ws.close()
        return
    try:
        while True:
            msg = await ws.receive_json()
            if msg.get("type") != "question" or not str(msg.get("text", "")).strip():
                await ws.send_json({"type": "error", "message": "expected {\"type\": \"question\", \"text\": ...}"})
                continue
            try:
                await _examine(ws, w, msg["text"].strip())
            except WebSocketDisconnect:
                raise
            except Exception as e:
                await ws.send_json({"type": "error", "message": f"{type(e).__name__}: {e}"})
    except WebSocketDisconnect:
        pass


app.mount("/", StaticFiles(directory=get_settings().resolve(get_settings().web_dir), html=True), name="web")
