"""Each witness's examination transcript, per visitor, kept in memory only.

A turn is recorded once its answer has been spoken: the question, how it was asked (typed or
voice), the answer as sent to the browser (unit, quote, badges) plus what replay needs (the
OCR spans), and the timing. A visitor's conversation with a witness is dropped after
upload_ttl_minutes without use (the upload's own TTL; for the example witness the same period),
or with the upload when it expires or is removed. At most conversation_max_turns are kept per
witness; older ones are dropped first.
"""
import itertools
import threading
import time
from crossexam.config import get_settings

_conversations: dict[tuple[str, str], dict] = {}  # (visitor, witness_id) -> {"turns", "last_active"}
_lock = threading.Lock()
_ids = itertools.count(1)


def _expire_stale():
    cutoff = time.time() - get_settings().upload_ttl_minutes * 60
    for key in [k for k, c in _conversations.items() if c["last_active"] < cutoff]:
        del _conversations[key]


def add(visitor: str | None, witness_id: str, turn: dict) -> dict:
    """Record a turn; returns it with its id and time."""
    turn = {**turn, "id": next(_ids), "at": time.time()}
    with _lock:
        _expire_stale()
        c = _conversations.setdefault((visitor or "", witness_id), {"turns": [], "last_active": 0})
        c["turns"].append(turn)
        del c["turns"][:-get_settings().conversation_max_turns]
        c["last_active"] = time.time()
    return turn


def turns(visitor: str | None, witness_id: str) -> list[dict]:
    with _lock:
        _expire_stale()
        c = _conversations.get((visitor or "", witness_id))
        if not c:
            return []
        c["last_active"] = time.time()
        return list(c["turns"])


def turn(visitor: str | None, witness_id: str, turn_id: int) -> dict | None:
    return next((t for t in turns(visitor, witness_id) if t["id"] == turn_id), None)


def count(visitor: str | None, witness_id: str) -> int:
    with _lock:
        c = _conversations.get((visitor or "", witness_id))
        return len(c["turns"]) if c else 0


def clear(visitor: str | None, witness_id: str):
    with _lock:
        _conversations.pop((visitor or "", witness_id), None)


def drop_witness(witness_id: str):
    """Forget every conversation with a witness (its upload expired or was removed)."""
    with _lock:
        for key in [k for k in _conversations if k[1] == witness_id]:
            del _conversations[key]
