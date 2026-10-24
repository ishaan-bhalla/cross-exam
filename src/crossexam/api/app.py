"""Web app: serves the frontend from web_dir, the witness API and the examination WebSocket.

Uploaded witnesses belong to the visitor who uploaded them (cookie VISITOR_COOKIE); another
visitor gets 404, an expired upload 410, and the examination refuses an upload whose profile
has not been confirmed (POST /api/witnesses/{id}/confirm).

Each visitor's conversation with each witness is kept (api.conversations): GET
/api/witnesses/{id}/conversation lists its turns, DELETE clears it, and over the WebSocket
{"type": "replay", "turn": id} re-synthesises a stored answer (no LLM call): the server
replies {"type": "replay", "turn", <answer fields>} and then audio, words and done as for a question.

WebSocket /ws/examine?witness={id}. Client sends {"type": "question", "text", "spoken"?, "voice_timing"?}. For a spoken
question the server first replies {"type": "question", "text", "id_changes"} with document IDs
normalised (witness.docids); then, for every question,
  {"type": "answer", para_no, quote, lead_in, spoken, verified, refused, referral, ocr_corrected, routed}
  {"type": "audio", "part": "lead_in" | "quote" | "refusal" | "referral", "data": base64 PCM16, "sample_rate"}
                                                                  (repeated, in play order)
  {"type": "words", "part": ..., "words": [{word, start, end, span}]}
  {"type": "done", answer_latency_ms, question_to_first_audio_ms, question_to_quote_audio_ms,
   tts_wait_ms, tts_session_opens, time_to_first_audio_ms, duration_ms, cached: [parts from cache],
   missing_words: [parts whose word timings never arrived]}
then {"type": "turn", "turn": id} once the turn is stored (not after a replay),
or {"type": "error", "message"}. Word start/end are ms from the start of this answer's audio
(before any hold the browser adds); span is [start, end] character offsets into paragraph
para_no's text, or None (not highlighted). A referral (the quote failed verification) has
quote null and para_no the referred unit; its phrase names that unit, so it is synthesised live.

Latency: one TTS session per examination is opened when the WebSocket connects and reused
for every answer (tts.PersistentSession), so no answer waits for a connection and
questioning does not hit the account's ~5 session opens per 30 s. Lead-ins and refusals are
fixed phrases, pre-synthesised at startup (api.phrases) and sent the moment the answer is
known; the quote is synthesised live, one flush per chunk (witness.chunking) so each
chunk's word timings arrive as soon as it is generated. Leading and trailing silence is
trimmed from cached phrases and live audio, and silence between chunks is capped
(tts.GapLimiter), with word timings shifted to match. The browser holds quote audio until the
quote's word timings arrive, for at most quote_timings_max_wait_ms.
"""
import asyncio
import base64
import json
import secrets
import struct
import time
from urllib.parse import urlencode
import requests
from contextlib import asynccontextmanager
import logging
from fastapi import FastAPI, File, HTTPException, Request, Response, UploadFile, WebSocket, WebSocketDisconnect
from pydantic import BaseModel, Field
from fastapi.routing import APIRoute
from fastapi.staticfiles import StaticFiles
from crossexam.api import conversations, phrases, uploads, witnesses
from crossexam.statements.parser import ocr_available
from crossexam.clients import tts
from crossexam.config import get_settings
from crossexam.clients.llm_gateway import LLMBusy, on_rate_limit, track_usage
from crossexam import budget, costs
from crossexam.witness.align import align_words, key_count
from crossexam.witness.chunking import chunk_quote
from crossexam.witness.docids import normalise_ids
from crossexam.witness.answer import answer, fixed_phrase, fixed_phrases, spoken_quote

VISITOR_COOKIE = "cx_visitor"
BUSY_MESSAGE = "The witness is gathering their thoughts: the service is busy."


DIAGNOSTIC_IP_HEADERS = ("x-forwarded-for", "x-real-ip", "true-client-ip", "cf-connecting-ip")


def client_ip(headers, peer) -> str | None:
    """The visitor's IP, which per-IP limits key on, so it must be one the client can't choose:
    1. client_ip_header, a header the platform sets and the client can't override, if it holds
       one address;
    2. else the X-Forwarded-For entry trusted_proxy_hops from the right (1 = the rightmost, added
       by the proxy nearest the app). Each proxy appends the address it received the request
       from, so entries to the left of the trusted ones may be whatever the client sent; the
       leftmost is never used.
    3. else (no such header or not enough entries) the socket peer address."""
    s = get_settings()
    name = s.client_ip_header.strip()
    if name:
        value = headers.get(name, "").strip()
        if value and "," not in value:
            return value
    if s.trusted_proxy_hops > 0:
        parts = [p.strip() for p in headers.get("x-forwarded-for", "").split(",") if p.strip()]
        if len(parts) >= s.trusted_proxy_hops:
            return parts[-s.trusted_proxy_hops]
    return peer.host if peer else None


def limit_keys(conn) -> list[tuple[str, int]]:
    """Quota keys for a request or WebSocket: its visitor cookie and its IP."""
    return budget.client_keys(conn.cookies.get(VISITOR_COOKIE), client_ip(conn.headers, conn.client))


def guard(check, *args):
    """Run a budget check, turning LimitReached into HTTP 429 with the visitor-facing message."""
    try:
        check(*args)
    except budget.LimitReached as e:
        raise HTTPException(429, str(e)) from None


log = logging.getLogger("crossexam")


@asynccontextmanager
async def lifespan(app: FastAPI):
    s = get_settings()
    if s.ocr_enabled and not ocr_available():
        log.warning("OCR unavailable: Tesseract (%r) or pypdfium2 not found. Statements will be parsed "
                    "from their text layer only; install tesseract (conda-forge) for scanned pages.",
                    s.tesseract_cmd)
    loaded = await asyncio.to_thread(uploads.load_persisted)
    if loaded:
        print(f"reloaded {loaded} persisted upload(s)")
    # Parse statements, load the embedding model and pre-synthesise fixed phrases
    # before the first question.
    for w in witnesses.list_witnesses():
        await asyncio.to_thread(witnesses.retriever, w["id"])
    texts = fixed_phrases()
    for voice in sorted({w["voice"] for w in witnesses.list_witnesses()}):
        started = time.perf_counter()
        try:
            counts = await phrases.warm(voice, texts)
        except Exception as e:
            # Not fatal: an uncached phrase is synthesised live when needed. (Seen when the account
            # was at its TTS session limit, error 3009, as containers restarted.)
            log.warning("phrases for voice %r not pre-synthesised (%s: %s); they will be synthesised live",
                        voice, type(e).__name__, e)
            continue
        print(f"phrases for voice {voice!r}: {counts} in {time.perf_counter() - started:.1f}s")
    yield


app = FastAPI(lifespan=lifespan)


@app.get("/api/config")
def config():
    s = get_settings()
    return {"audio_buffer_ms": s.ui_audio_buffer_ms, "quote_timings_max_wait_ms": s.quote_timings_max_wait_ms,
            "stt_sample_rate": s.stt_sample_rate, "stt_chunk_ms": s.stt_chunk_ms,
            "upload_max_mb": s.upload_max_mb, "ocr_available": s.ocr_enabled and ocr_available(),
            "uploads_per_client": s.uploads_per_client, "upload_ttl_minutes": s.upload_ttl_minutes,
            "demo_limit": budget.demo_limited(), "demo_video_url": s.demo_video_url or None}


@app.get("/healthz")
def healthz():
    """For Render's health check and an uptime pinger: the app is up. Counts towards no limit or
    budget; it does expire idle uploads, so their memory is freed even when nobody else visits."""
    uploads.expire_stale()
    return {"status": "ok"}


@app.get("/api/limits")
def limits(request: Request):
    """This visitor's usage against the demo's quotas (cookie and IP), and the global budget."""
    keys = limit_keys(request)
    out = {"ip": client_ip(request.headers, request.client), **budget.client_status(keys), "global": budget.status()}
    if get_settings().debug_client_ip:
        # Temporary, to see what the platform's proxies send (DEBUG_CLIENT_IP; off by default).
        out["debug_client_ip"] = {"headers": {h: request.headers.get(h) for h in DIAGNOSTIC_IP_HEADERS},
                                  "peer": request.client.host if request.client else None}
    return out


def voices() -> list[dict]:
    """[{"id", "accent"}] from tts_voices ("voice:accent,...")."""
    pairs = [v.split(":", 1) for v in get_settings().tts_voices.split(",") if v.strip()]
    return [{"id": v[0].strip(), "accent": v[1].strip() if len(v) > 1 else ""} for v in pairs]


def _public_record(record: dict) -> dict:
    return {k: v for k, v in record.items() if k not in ("visitor", "last_active")}


def check_access(witness_id: str, visitor: str | None) -> dict:
    """The witness, or HTTPException: 404 if unknown or another visitor's upload, 410 if expired."""
    w = witnesses.get_witness(witness_id)
    if w and not w.get("uploaded"):
        return w
    record = uploads.get(witness_id, visitor)
    if record and record["stage"] == "expired":
        raise HTTPException(410, uploads.EXPIRED_MESSAGE)
    w = witnesses.get_witness(witness_id)
    if not record or not w:
        raise HTTPException(404, "unknown witness")
    return w


@app.middleware("http")
async def visitor_cookie(request: Request, call_next):
    """Give every visitor a cookie on their first request, so the example witness's
    conversation (and later uploads) can be kept for them."""
    response = await call_next(request)
    if request.url.path == "/healthz":  # uptime pingers are not visitors
        return response
    if VISITOR_COOKIE not in request.cookies and VISITOR_COOKIE not in response.headers.get("set-cookie", ""):
        response.set_cookie(VISITOR_COOKIE, secrets.token_urlsafe(16), httponly=True, samesite="lax", max_age=30 * 24 * 3600)
    return response


@app.get("/api/witnesses")
def list_witnesses(request: Request):
    """Witnesses this visitor can see, each with "questions": the turns in their conversation."""
    uploads.expire_stale()
    visitor = request.cookies.get(VISITOR_COOKIE)
    return [{**w, "questions": conversations.count(visitor, w["id"])} for w in witnesses.list_witnesses(visitor)]


@app.get("/api/witnesses/{witness_id}/conversation")
def get_conversation(witness_id: str, request: Request):
    visitor = request.cookies.get(VISITOR_COOKIE)
    check_access(witness_id, visitor)
    return [{k: v for k, v in t.items() if k != "ocr_spans"} for t in conversations.turns(visitor, witness_id)]


@app.delete("/api/witnesses/{witness_id}/conversation", status_code=204)
def clear_conversation(witness_id: str, request: Request):
    visitor = request.cookies.get(VISITOR_COOKIE)
    check_access(witness_id, visitor)
    conversations.clear(visitor, witness_id)


@app.get("/api/voices")
def list_voices():
    return {"voices": voices(), "default": get_settings().tts_default_voice}


@app.get("/api/voices/{voice}/sample")
async def voice_sample(voice: str):
    """A short sample of the voice as WAV, synthesised once and then served from the phrase cache."""
    if voice not in {v["id"] for v in voices()}:
        raise HTTPException(404, "unknown voice")
    text = fixed_phrase("SAMPLE")
    if not phrases.get(voice, text):
        guard(budget.check_demo)
        await phrases.warm(voice, [text])
    phrase = phrases.get(voice, text)
    header = struct.pack("<4sI4s4sIHHIIHH4sI", b"RIFF", 36 + len(phrase.pcm), b"WAVE", b"fmt ", 16, 1, 1,
                         phrase.sample_rate, phrase.sample_rate * 2, 2, 16, b"data", len(phrase.pcm))
    return Response(header + phrase.pcm, media_type="audio/wav", headers={"Cache-Control": "max-age=86400"})


@app.post("/api/witnesses/upload", status_code=202)
async def upload_statement(request: Request, response: Response, file: UploadFile = File(...)):
    """Start processing an uploaded statement PDF. Returns the upload record (poll its
    status_url); the draft profile is in it once the stage is "ready"."""
    s = get_settings()
    visitor = request.cookies.get(VISITOR_COOKIE) or secrets.token_urlsafe(16)
    response.set_cookie(VISITOR_COOKIE, visitor, httponly=True, samesite="lax", max_age=30 * 24 * 3600)
    keys = budget.client_keys(visitor, client_ip(request.headers, request.client))
    guard(budget.check_demo)
    guard(budget.check_upload, keys)
    if s.max_open_uploads and uploads.active_count() >= s.max_open_uploads:
        # Every open upload holds a parsed statement and its index in memory (512 MB on Render).
        raise HTTPException(503, "The demo is busy, please try again in a few minutes.")
    open_now = uploads.active_count(visitor)
    if open_now >= s.uploads_per_client:
        raise HTTPException(429, f"You already have {open_now} statements open, the most allowed at once. "
                                 "Remove one from the front page, or wait for one to expire after "
                                 f"{s.upload_ttl_minutes} minutes without use.")
    limit = s.upload_max_mb * 1024 * 1024
    data = await file.read(limit + 1)
    if len(data) > limit:
        raise HTTPException(413, f"file is larger than the {s.upload_max_mb} MB limit")
    if not data.startswith(b"%PDF-"):
        raise HTTPException(415, "only PDF files can be uploaded")
    budget.charge_upload(keys)
    record = uploads.start(file.filename or "statement.pdf", data, visitor)
    return {**_public_record(record), "status_url": f"/api/uploads/{record['id']}"}


@app.get("/api/uploads")
def my_uploads(request: Request):
    """This visitor's uploads that have not expired, newest first."""
    visitor = request.cookies.get(VISITOR_COOKIE)
    mine = []
    for w in witnesses.list_witnesses(visitor):
        record = uploads.get(w["id"], visitor, touch=False) if w.get("uploaded") else None
        if record and record.get("visitor") == visitor:
            mine.append({"id": w["id"], "name": w["name"], "filename": record["filename"], "stage": record["stage"],
                         "started_at": record["started_at"]})
    return sorted(mine, key=lambda r: -r["started_at"])


@app.get("/api/uploads/{upload_id}")
def upload_status(upload_id: str, request: Request):
    record = uploads.get(upload_id, request.cookies.get(VISITOR_COOKIE))
    if not record:
        raise HTTPException(404, "unknown upload")
    return _public_record(record)


@app.delete("/api/uploads/{upload_id}", status_code=204)
def remove_upload(upload_id: str, request: Request):
    if not uploads.get(upload_id, request.cookies.get(VISITOR_COOKIE)):
        raise HTTPException(404, "unknown upload")
    uploads.remove(upload_id)


class Role(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    organisation: str | None = Field(None, max_length=200)
    from_: str | None = Field(None, alias="from", max_length=60)
    to: str | None = Field(None, max_length=60)


class Confirmation(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    role_label: str = Field(min_length=1, max_length=300)
    roles: list[Role] = Field(default_factory=list, max_length=20)
    voice: str


@app.post("/api/witnesses/{witness_id}/confirm", status_code=202)
def confirm_witness(witness_id: str, body: Confirmation, request: Request):
    """Confirm an uploaded witness's profile and voice; poll the upload until "confirmed"."""
    visitor = request.cookies.get(VISITOR_COOKIE)
    w = check_access(witness_id, visitor)
    if not w.get("uploaded"):
        raise HTTPException(400, "only uploaded witnesses are confirmed")
    if body.voice not in {v["id"] for v in voices()}:
        raise HTTPException(422, "unknown voice")
    if uploads.get(witness_id, visitor)["stage"] not in ("ready", "confirmed", "error"):
        raise HTTPException(409, "this statement is not ready to confirm")
    guard(budget.check_demo)  # preparing the voice synthesises its phrases
    roles = [{"title": r.title.strip(), "organisation": r.organisation, "from": r.from_, "to": r.to} for r in body.roles]
    uploads.confirm(witness_id, body.name.strip(), body.role_label.strip(), roles, body.voice)
    return _public_record(uploads.get(witness_id, visitor))


@app.post("/api/witnesses/{witness_id}/stt-session")
def stt_session(witness_id: str, request: Request):
    """A temporary speech-to-text token, as a ready-to-use WebSocket URL. The API key stays
    on the server; the token is single-use and expires after stt_token_expires_seconds."""
    check_access(witness_id, request.cookies.get(VISITOR_COOKIE))
    keys = limit_keys(request)
    guard(budget.check_demo)
    guard(budget.check_spoken, keys)
    s = get_settings()
    r = requests.get(s.stt_token_url, headers={"Authorization": s.assemblyai_api_key},
                     params={"expires_in_seconds": s.stt_token_expires_seconds,
                             "max_session_duration_seconds": s.stt_max_session_seconds},
                     timeout=s.llm_timeout_seconds)
    if r.status_code != 200:
        raise HTTPException(502, f"speech-to-text token request failed ({r.status_code})")
    # Each token allows one session of up to stt_max_session_seconds: counted when issued.
    budget.charge_stt_session(keys)
    terms = witnesses.keyterms(witness_id)
    params = {"token": r.json()["token"], "speech_model": s.stt_speech_model, "encoding": "pcm_s16le",
              "sample_rate": s.stt_sample_rate, "keyterms_prompt": json.dumps(terms)}
    for name, value in (("min_turn_silence", s.stt_min_turn_silence_ms), ("max_turn_silence", s.stt_max_turn_silence_ms),
                        ("end_of_turn_confidence_threshold", s.stt_end_of_turn_confidence)):
        if value.strip():
            params[name] = value.strip()
    return {"url": f"{s.stt_ws_url}?{urlencode(params)}", "sample_rate": s.stt_sample_rate,
            "chunk_ms": s.stt_chunk_ms, "submit_grace_ms": s.stt_submit_grace_ms, "keyterms": terms,
            "expires_in_seconds": s.stt_token_expires_seconds,
            "refresh_margin_seconds": s.stt_token_refresh_margin_seconds}


@app.get("/api/witnesses/{witness_id}/statement")
def statement(witness_id: str, request: Request):
    check_access(witness_id, request.cookies.get(VISITOR_COOKIE))
    return [{"para_no": p["para_no"], "page": p["page"], "text": p["text"], "section": p.get("section")}
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


ANSWER_FIELDS = ("para_no", "quote", "lead_in", "spoken", "verified", "referral", "ocr_corrected", "routed")


def answer_message(result: dict) -> dict:
    """The answer as sent to the browser (and stored in the conversation)."""
    return {**{k: result[k] for k in ANSWER_FIELDS}, "refused": result["para_no"] is None}


async def _examine(ws: WebSocket, witness: dict, question: str, tts_conn: tts.PersistentSession,
                   stt_audio_seconds: float | None = None) -> tuple[dict, dict]:
    """Answer a question and speak it. Returns (answer result, done message)."""
    received = time.perf_counter()
    loop = asyncio.get_running_loop()

    def busy(wait_seconds: float):
        msg = {"type": "busy", "message": BUSY_MESSAGE, "wait_seconds": round(wait_seconds, 1)}
        loop.call_soon_threadsafe(lambda: asyncio.ensure_future(ws.send_json(msg)))

    def run():
        with track_usage() as calls, on_rate_limit(busy):
            return answer(question, witnesses.retriever(witness["id"]), witness["name"]), calls
    result, llm_calls = await asyncio.to_thread(run)
    answer_latency_ms = (time.perf_counter() - received) * 1000
    await ws.send_json({"type": "answer", **answer_message(result)})
    return result, await _speak(ws, witness, result, tts_conn, received, answer_latency_ms, llm_calls, stt_audio_seconds)


async def _speak(ws: WebSocket, witness: dict, result: dict, tts_conn: tts.PersistentSession,
                 received: float, answer_latency_ms: float, llm_calls: list[dict] | None = None,
                 stt_audio_seconds: float | None = None) -> dict:
    """Send the answer's audio and word timings (cached phrases, then the live quote), then
    the done message, which is returned. Its "usage" (crossexam.costs) counts the LLM calls
    passed in and what was synthesised live: characters sent and the audio seconds the TTS
    server reported per flush (FlushDone audio_duration_ms, the per-flush form of
    Termination's audio_duration_seconds)."""
    s = get_settings()
    voice = witness["voice"]
    refused = result["para_no"] is None
    referral = result["referral"]
    out = AudioOut(ws, received)
    cached, live = [], []  # live: [{"kind", "text", "label", "start"}], one per flush

    if refused:
        phrase = phrases.get(voice, result["spoken"])
        if phrase:
            await out.send(phrase.pcm, phrase.sample_rate, "refusal")
            await out.words("refusal", phrase.words, 0)
            cached.append("refusal")
        else:
            live = [{"kind": "refusal", "text": result["spoken"]}]
    elif referral:
        live = [{"kind": "referral", "text": result["spoken"]}]
    else:
        lead = phrases.get(voice, result["lead_in"]) if result["lead_in"] else None
        if lead:
            await out.send(lead.pcm, lead.sample_rate, "lead_in")
            await out.words("lead_in", lead.words, 0)
            await out.silence(s.tts_lead_in_gap_ms, lead.sample_rate, "lead_in")
            cached.append("lead_in")
        elif result["lead_in"]:
            live.append({"kind": "lead_in", "text": result["lead_in"]})
        chunks = chunk_quote(spoken_quote(result["quote"]))
        offset = 0  # index of the chunk's first word within the quote, for alignment
        for i, chunk in enumerate(chunks, 1):
            live.append({"kind": "quote", "text": chunk, "label": f"quote {i}/{len(chunks)}", "start": offset})
            offset += key_count(chunk)
    for part in live:
        part.setdefault("label", part["kind"])

    tts_wait_ms = time_to_first_audio_ms = quote_audio_ms = None
    tts_live_ms = None
    missing_words = []
    if live:
        wait_started = time.perf_counter()
        session = await tts_conn.ensure()  # normally already open: no wait
        tts_wait_ms = (time.perf_counter() - wait_started) * 1000
        paragraph = None if refused or referral else next(
            p["text"] for p in witnesses.statement(witness["id"]) if p["para_no"] == result["para_no"])
        # Silence allowed before each part's speech: a short pre-roll before the first,
        # the lead-in gap between a live lead-in and the quote, a short pause between chunks.
        gaps = [s.tts_trim_preroll_ms if i == 0 else
                s.tts_lead_in_gap_ms if live[i - 1]["kind"] == "lead_in" else s.tts_chunk_gap_ms
                for i in range(len(live))]
        limiter = tts.GapLimiter(session.sample_rate, gaps, s.tts_trim_tail_ms, s.tts_silence_threshold)
        live_start_ms = out.position_ms
        async for event in tts_conn.generate([p["text"] for p in live]):
            if event["type"] == "audio":
                part = live[event["part"]]
                pcm = limiter.feed(event["part"], event["pcm"])
                if pcm and quote_audio_ms is None and part["kind"] == "quote":
                    quote_audio_ms = (time.perf_counter() - received) * 1000
                await out.send(pcm, session.sample_rate, part["kind"])
            elif event["type"] == "words":
                part = live[event["part"]]
                words = event["words"]
                words = align_words(words, result["quote"], paragraph, part["start"], result["ocr_spans"]) \
                    if part["kind"] == "quote" \
                    else [{**w, "span": None} for w in words]
                await out.words(part["kind"], words, live_start_ms - limiter.shift_ms(event["part"]))
            elif event["type"] == "done":
                await out.send(limiter.finish(), session.sample_rate, live[-1]["kind"])
                time_to_first_audio_ms = round(event["time_to_first_audio_ms"])
                tts_live_ms = event["duration_ms"]
                missing_words = [live[i]["label"] for i in event["missing_words"]]
    if quote_audio_ms is None and not refused and not referral:
        quote_audio_ms = out.first_audio_ms

    done = {"type": "done", "answer_latency_ms": round(answer_latency_ms),
                        "question_to_first_audio_ms": round(out.first_audio_ms or 0),
                        "question_to_quote_audio_ms": round(quote_audio_ms) if quote_audio_ms else None,
                        "tts_wait_ms": round(tts_wait_ms) if tts_wait_ms is not None else None,
                        "tts_session_opens": tts_conn.opens,
                        "time_to_first_audio_ms": time_to_first_audio_ms,
                        "duration_ms": round(out.position_ms), "cached": cached,
                        "missing_words": missing_words,
                        "usage": costs.usage(llm_calls, sum(len(p["text"]) for p in live),
                                             tts_live_ms / 1000 if tts_live_ms is not None else 0.0, stt_audio_seconds)}
    await ws.send_json(done)
    return done


@app.websocket("/ws/examine")
async def examine(ws: WebSocket, witness: str):
    visitor = ws.cookies.get(VISITOR_COOKIE)
    keys = limit_keys(ws)
    await ws.accept()
    try:
        w = check_access(witness, visitor)
        if w.get("uploaded") and not w.get("profile_confirmed"):
            raise HTTPException(409, "Confirm this witness's profile before the examination starts.")
    except HTTPException as e:
        await ws.send_json({"type": "error", "message": e.detail, "status": e.status_code})
        await ws.close()
        return
    tts_conn = tts.PersistentSession(w["voice"])
    opening = asyncio.create_task(tts_conn.ensure())  # ready before the first answer
    opening.add_done_callback(lambda t: t.cancelled() or t.exception())  # retried on first use
    try:
        while True:
            msg = await ws.receive_json()
            replay = msg.get("type") == "replay"
            if not replay and (msg.get("type") != "question" or not str(msg.get("text", "")).strip()):
                await ws.send_json({"type": "error", "message": "expected {\"type\": \"question\", \"text\": ...}"})
                continue
            try:
                check_access(w["id"], visitor)  # counts as activity, and stops if it expired
            except HTTPException as e:
                await ws.send_json({"type": "error", "message": e.detail, "status": e.status_code})
                continue
            try:
                budget.check_demo()
                budget.check_question(keys)
            except budget.LimitReached as e:
                await ws.send_json({"type": "error", "message": str(e), "status": 429,
                                    "code": "demo_limit" if e.demo else "limit"})
                continue
            budget.charge_question(keys)
            if replay:
                stored = conversations.turn(visitor, w["id"], msg.get("turn"))
                if not stored:
                    await ws.send_json({"type": "error", "message": "that answer is no longer in the conversation"})
                    continue
                try:
                    await ws.send_json({"type": "replay", "turn": stored["id"], **stored["answer"]})
                    await _speak(ws, w, {**stored["answer"], "ocr_spans": stored["ocr_spans"]}, tts_conn,
                                 time.perf_counter(), 0)
                except WebSocketDisconnect:
                    raise
                except Exception as e:
                    await ws.send_json({"type": "error", "message": f"{type(e).__name__}: {e}"})
                continue
            try:
                question = msg["text"].strip()
                if msg.get("spoken"):
                    # Rebuild document IDs the transcript split or misheard, and tell the client
                    # what was understood so the submitted question shows the corrected ID.
                    question, changes = normalise_ids(question, witnesses.cited_ids(w["id"]),
                                                      get_settings().doc_id_snap_max_distance)
                    await ws.send_json({"type": "question", "text": question, "id_changes": changes})
                voice_timing = msg.get("voice_timing") if isinstance(msg.get("voice_timing"), dict) else {}
                stt_seconds = voice_timing.get("stt_audio_seconds")
                if msg.get("spoken") and isinstance(stt_seconds, (int, float)):
                    budget.charge_spoken(keys, min(float(stt_seconds), get_settings().stt_max_session_seconds))
                result, done = await _examine(ws, w, question, tts_conn,
                                              stt_seconds if isinstance(stt_seconds, (int, float)) else None)
                timing = {k: v for k, v in done.items() if k != "type"}
                if isinstance(msg.get("voice_timing"), dict):
                    timing["voice"] = {k: v for k, v in msg["voice_timing"].items() if isinstance(v, (int, float))}
                stored = conversations.add(visitor, w["id"], {
                    "question": question, "asked": "voice" if msg.get("spoken") else "typed",
                    "answer": answer_message(result), "ocr_spans": result["ocr_spans"], "timing": timing})
                await ws.send_json({"type": "turn", "turn": stored["id"]})
            except WebSocketDisconnect:
                raise
            except LLMBusy as e:
                await ws.send_json({"type": "error", "message": str(e), "code": "busy"})
            except Exception as e:
                await ws.send_json({"type": "error", "message": f"{type(e).__name__}: {e}"})
    except WebSocketDisconnect:
        pass
    finally:
        opening.cancel()
        # Shielded: if this handler is cancelled as the socket closes, the TTS session is still
        # terminated (and its totals logged) rather than left open against the session limit.
        closing = asyncio.ensure_future(tts_conn.close())
        closing.add_done_callback(lambda f: f.cancelled() or f.exception() or _log_termination(w["id"], f.result()))
        await asyncio.shield(closing)


def _log_termination(witness_id: str, termination: dict | None):
    if termination:  # the TTS server's own totals for this examination's session
        print(f"tts session for {witness_id}: {termination.get('total_input_char_length')} chars, "
              f"{termination.get('audio_duration_seconds')} s audio, {termination.get('session_duration_seconds')} s open")


# Every GET route also answers HEAD (uptime monitors such as UptimeRobot's free plan check with
# HEAD). Without this a HEAD request matches no route and falls through to the static files
# mounted at "/", which answer 404 for /healthz and the API. Starlette serves HEAD with GET's
# status and headers and no body; static files already handle HEAD themselves.
for route in app.routes:
    if isinstance(route, APIRoute) and "GET" in route.methods:
        route.methods.add("HEAD")

app.mount("/", StaticFiles(directory=get_settings().resolve(get_settings().web_dir), html=True), name="web")
