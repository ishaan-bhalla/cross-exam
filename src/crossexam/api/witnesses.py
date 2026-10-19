"""Witness registry: witnesses_file lists {id, name, role_label, statement_pdf, voice},
with statement_pdf relative to data_dir. Statements and retrievers are built once."""
import json
from functools import lru_cache
from pathlib import Path
from crossexam.config import get_settings
from crossexam.retrieval.retriever import Retriever
from crossexam.statements.parser import load_statement

PUBLIC_FIELDS = ("id", "name", "role_label", "voice")


@lru_cache
def _registry() -> dict[str, dict]:
    s = get_settings()
    return {w["id"]: w for w in json.loads(s.resolve(s.witnesses_file).read_text())}


def list_witnesses() -> list[dict]:
    return [{k: w[k] for k in PUBLIC_FIELDS} for w in _registry().values()]


def get_witness(witness_id: str) -> dict | None:
    return _registry().get(witness_id)


def _pdf(witness: dict) -> Path:
    s = get_settings()
    return s.resolve(s.data_dir) / witness["statement_pdf"]


@lru_cache
def statement(witness_id: str) -> list[dict]:
    return load_statement(_pdf(_registry()[witness_id]))


@lru_cache
def retriever(witness_id: str) -> Retriever:
    pdf = _pdf(_registry()[witness_id])
    return Retriever(statement(witness_id), pdf.stem)
