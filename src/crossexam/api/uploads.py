"""Uploaded witness statements.

POST /api/witnesses/upload hands the PDF bytes to start(), which returns at once with an
upload record; a worker thread then parses the statement (OCR where needed), builds its
retriever, extracts key terms and drafts a role profile, updating the record's progress as it
goes. GET /api/uploads/{id} returns that record, so the page can poll it.

Once ready, the witness is a draft: confirm() takes the user's corrected name, role label, roles
and voice, then synthesises the fixed phrases in that voice ("voice" stage) before the stage
becomes "confirmed" and the examination may start.

Uploads belong to the visitor (a browser cookie) who made them: each visitor may have
uploads_per_client open at once, and an upload unused for upload_ttl_minutes expires: it is
dropped from memory, and its record is kept only to say so.

By default everything is held in memory for the life of the server process and nothing is
written to data_dir: the parser is given the bytes, its OCR temporary files are deleted, and
the vector index is not cached. With persist_uploads, the PDF and the parse are kept under
uploads_dir and reloaded at startup.
"""
import asyncio
import json
import secrets
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from crossexam.api import phrases, witnesses
from crossexam.config import get_settings
from crossexam.retrieval.retriever import Retriever
from crossexam.statements.keyterms import statement_keyterms
from crossexam.statements.parser import parse_statement
from crossexam.witness.profile import draft_profile

STAGES = ("queued", "reading", "ocr", "parsing", "indexing", "profiling", "ready", "voice", "confirmed",
          "error", "expired")
EXPIRED_MESSAGE = "This upload has expired, please upload again."

_uploads: dict[str, dict] = {}
_lock = threading.Lock()
_pool: ThreadPoolExecutor | None = None


def _executor() -> ThreadPoolExecutor:
    global _pool
    if _pool is None:
        _pool = ThreadPoolExecutor(max_workers=get_settings().upload_workers, thread_name_prefix="upload")
    return _pool


def _update(upload_id: str, **fields):
    with _lock:
        _uploads[upload_id].update(fields)


def expire_stale():
    """Expire uploads unused for upload_ttl_minutes: unregister the witness and drop the text."""
    cutoff = time.time() - get_settings().upload_ttl_minutes * 60
    with _lock:
        stale = [u for u in _uploads.values()
                 if u["stage"] not in ("expired", "error") and u.get("last_active", time.time()) < cutoff]
        for u in stale:
            u.update(stage="expired", message=EXPIRED_MESSAGE, profile=None, preamble=None, keyterms=[])
    for u in stale:
        witnesses.unregister(u["id"])


def get(upload_id: str, visitor: str | None = None, touch: bool = True) -> dict | None:
    """The upload's record, or None if unknown or another visitor's. Using it counts as activity."""
    expire_stale()
    with _lock:
        u = _uploads.get(upload_id)
        if not u or (u.get("visitor") and u["visitor"] != visitor):
            return None
        if touch and u["stage"] != "expired":
            u["last_active"] = time.time()
        return dict(u)


def remove(upload_id: str):
    """Forget an upload now, as if it had expired."""
    with _lock:
        _uploads.pop(upload_id, None)
    witnesses.unregister(upload_id)


def active_count(visitor: str) -> int:
    expire_stale()
    with _lock:
        return sum(u.get("visitor") == visitor and u["stage"] not in ("expired", "error") for u in _uploads.values())


def start(filename: str, data: bytes, visitor: str | None = None) -> dict:
    upload_id = f"upload-{secrets.token_hex(8)}"
    record = {"id": upload_id, "witness_id": upload_id, "visitor": visitor, "last_active": time.time(),
              "filename": filename, "bytes": len(data),
              "stage": "queued", "message": "Waiting to start", "page": None, "pages": None,
              "started_at": time.time(), "ready_at": None, "elapsed_ms": None, "error": None,
              "units": None, "ocr_pages": [], "keyterms": [], "profile": None, "persisted": False}
    with _lock:
        _uploads[upload_id] = record
    _executor().submit(_process, upload_id, filename, data, visitor)
    return dict(record)


def _message(stage: str, page=None, pages=None) -> str:
    return {"reading": f"Reading page {page} of {pages}",
            "ocr": f"OCR on page {page} of {pages}",
            "parsing": "Finding paragraphs",
            "indexing": "Indexing the statement",
            "profiling": "Drafting the witness profile",
            "voice": "Preparing the witness's voice"}.get(stage, stage)


def _process(upload_id: str, filename: str, data: bytes, visitor: str | None = None):
    s = get_settings()
    t0 = time.perf_counter()
    try:
        def progress(stage, page=None, pages=None, **_):
            _update(upload_id, stage=stage, page=page, pages=pages, message=_message(stage, page, pages))

        parsed = parse_statement(data, progress)
        paragraphs = parsed["paragraphs"]
        if not paragraphs:
            raise ValueError("no numbered paragraphs found in this PDF")
        progress("indexing")
        retriever = Retriever(paragraphs, upload_id, persist_index=s.persist_uploads)
        terms = statement_keyterms(paragraphs)
        progress("profiling")
        profile = draft_profile(parsed["preamble"], paragraphs)
        witness = {"id": upload_id, "name": profile.get("name") or filename.rsplit(".", 1)[0],
                   "role_label": profile.get("role_label") or "Uploaded statement",
                   "voice": s.tts_default_voice, "uploaded": True, "profile_confirmed": False,
                   "visitor": visitor}
        witnesses.register_upload(witness, paragraphs, retriever, terms)
        if s.persist_uploads:
            _persist(upload_id, filename, data, parsed, profile, witness)
        elapsed = round((time.perf_counter() - t0) * 1000)
        _update(upload_id, stage="ready", message="Ready", ready_at=time.time(), elapsed_ms=elapsed,
                units=len(paragraphs), ocr_pages=parsed["ocr_pages"], pages=parsed["pages"], page=None,
                keyterms=terms, warnings=parsed["warnings"], persisted=s.persist_uploads,
                preamble=parsed["preamble"],
                profile={k: profile[k] for k in ("name", "organisation", "roles", "dropped", "role_label", "error")})
    except Exception as e:
        _update(upload_id, stage="error", message=f"Could not process this statement: {e}",
                error=f"{type(e).__name__}: {e}", elapsed_ms=round((time.perf_counter() - t0) * 1000))


def confirm(upload_id: str, name: str, role_label: str, roles: list[dict], voice: str):
    """Apply the user's corrections to the draft witness and prepare its voice. The examination
    can start once the stage is "confirmed"."""
    witness = witnesses.get_witness(upload_id)
    witness.update(name=name, role_label=role_label, roles=roles, voice=voice, profile_confirmed=False)
    _update(upload_id, stage="voice", message=_message("voice"))
    _executor().submit(_prepare_voice, upload_id, voice)


def _prepare_voice(upload_id: str, voice: str):
    from crossexam.witness.answer import fixed_phrases
    try:
        asyncio.run(phrases.warm(voice, fixed_phrases()))
    except Exception as e:
        _update(upload_id, stage="error", message=f"Could not prepare the witness's voice: {e}",
                error=f"{type(e).__name__}: {e}")
        return
    witness = witnesses.get_witness(upload_id)
    if witness:  # it may have expired meanwhile
        witness["profile_confirmed"] = True
        _update(upload_id, stage="confirmed", message="Confirmed")


def _persist(upload_id, filename, data, parsed, profile, witness):
    s = get_settings()
    folder = s.resolve(s.uploads_dir) / upload_id
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "statement.pdf").write_bytes(data)
    (folder / "upload.json").write_text(json.dumps({"filename": filename, "parsed": parsed, "profile": profile,
                                                    "witness": witness}, indent=1))


def load_persisted() -> int:
    """Re-register uploads kept by persist_uploads. Returns how many were loaded."""
    s = get_settings()
    root = s.resolve(s.uploads_dir)
    if not s.persist_uploads or not root.exists():
        return 0
    n = 0
    for meta_path in sorted(root.glob("*/upload.json")):
        meta = json.loads(meta_path.read_text())
        upload_id = meta_path.parent.name
        paragraphs = meta["parsed"]["paragraphs"]
        retriever = Retriever(paragraphs, upload_id, persist_index=False)
        witnesses.register_upload(meta["witness"], paragraphs, retriever, statement_keyterms(paragraphs))
        with _lock:
            _uploads[upload_id] = {"id": upload_id, "witness_id": upload_id, "filename": meta["filename"],
                                   "stage": "ready", "message": "Ready (reloaded)", "units": len(paragraphs),
                                   "profile": meta["profile"], "persisted": True, "visitor": None,
                                   "last_active": time.time()}
        n += 1
    return n
