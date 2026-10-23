"""Witness registry.

Configured witnesses: witnesses_file lists {id, name, role_label, statement_pdf, voice, keyterms},
with statement_pdf relative to data_dir; statements and retrievers are built once and cached.

Uploaded witnesses (api.uploads) are registered here once ready, with their parsed statement,
retriever and key terms held in memory, and are then used exactly like configured ones.
"""
import json
from functools import lru_cache
from pathlib import Path
from crossexam.config import get_settings
from crossexam.retrieval.retriever import Retriever
from crossexam.statements import keyterms as statement_terms
from crossexam.statements.parser import load_statement

PUBLIC_FIELDS = ("id", "name", "role_label", "voice")
UPLOAD_FIELDS = PUBLIC_FIELDS + ("profile_confirmed",)

_uploaded: dict[str, dict] = {}  # id -> {"witness", "statement", "retriever", "keyterms", "cited_ids"}


@lru_cache
def _registry() -> dict[str, dict]:
    s = get_settings()
    return {w["id"]: w for w in json.loads(s.resolve(s.witnesses_file).read_text())}


def register_upload(witness: dict, paragraphs: list[dict], retriever: Retriever, terms: list[str]):
    _uploaded[witness["id"]] = {"witness": witness, "statement": paragraphs, "retriever": retriever,
                                "keyterms": terms, "cited_ids": statement_terms.cited_ids(paragraphs)}


def unregister(witness_id: str):
    _uploaded.pop(witness_id, None)


def list_witnesses(visitor: str | None = None, uploads: bool = True) -> list[dict]:
    """Configured witnesses, then (if uploads) the uploads this visitor may see: their own, and
    any reloaded by persist_uploads (which have no visitor)."""
    configured = [{k: w[k] for k in PUBLIC_FIELDS} for w in _registry().values()]
    uploaded = [{**{k: u["witness"].get(k) for k in UPLOAD_FIELDS}, "uploaded": True} for u in _uploaded.values()
                if uploads and u["witness"].get("visitor") in (None, visitor)]
    return configured + uploaded


def get_witness(witness_id: str) -> dict | None:
    if witness_id in _uploaded:
        return _uploaded[witness_id]["witness"]
    return _registry().get(witness_id)


def _pdf(witness: dict) -> Path:
    s = get_settings()
    return s.resolve(s.data_dir) / witness["statement_pdf"]


def statement(witness_id: str) -> list[dict]:
    if witness_id in _uploaded:
        return _uploaded[witness_id]["statement"]
    return _configured_statement(witness_id)


@lru_cache
def _configured_statement(witness_id: str) -> list[dict]:
    return load_statement(_pdf(_registry()[witness_id]))


def cited_ids(witness_id: str) -> list[str]:
    """Document IDs cited in the statement, in order of first mention."""
    if witness_id in _uploaded:
        return _uploaded[witness_id]["cited_ids"]
    return _configured_cited_ids(witness_id)


@lru_cache
def _configured_cited_ids(witness_id: str) -> list[str]:
    return statement_terms.cited_ids(statement(witness_id))


def keyterms(witness_id: str) -> list[str]:
    """Speech-to-text key terms. Configured: the witness file's terms, then every document ID
    cited in the statement. Uploaded: the statement's acronyms and cited IDs. Capped at
    stt_max_keyterms."""
    if witness_id in _uploaded:
        return _uploaded[witness_id]["keyterms"]
    return _configured_keyterms(witness_id)


@lru_cache
def _configured_keyterms(witness_id: str) -> list[str]:
    terms = list(_registry()[witness_id].get("keyterms", [])) + cited_ids(witness_id)
    return list(dict.fromkeys(terms))[:get_settings().stt_max_keyterms]


def retriever(witness_id: str) -> Retriever:
    if witness_id in _uploaded:
        return _uploaded[witness_id]["retriever"]
    return _configured_retriever(witness_id)


@lru_cache
def _configured_retriever(witness_id: str) -> Retriever:
    pdf = _pdf(_registry()[witness_id])
    return Retriever(statement(witness_id), pdf.stem)
