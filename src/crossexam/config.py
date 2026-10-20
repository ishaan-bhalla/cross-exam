from functools import lru_cache
from pathlib import Path
from pydantic import ValidationError
from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parents[2]
ENV_FILE = PROJECT_ROOT / ".env"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=ENV_FILE, extra="ignore")

    assemblyai_api_key: str
    llm_gateway_url: str
    llm_model: str
    llm_max_tokens: int
    llm_temperature: float
    llm_timeout_seconds: float
    llm_max_retries: int
    llm_retry_backoff_seconds: float
    tts_ws_url: str
    tts_default_voice: str
    tts_timeout_seconds: float
    tts_sample_rate: int
    tts_session_retries: int
    tts_session_retry_backoff_seconds: float
    tts_silence_threshold: int
    tts_trim_preroll_ms: int
    tts_lead_in_gap_ms: int
    tts_keepalive_seconds: float
    tts_words_wait_seconds: float
    quote_timings_max_wait_ms: int
    phrase_cache_dir: str
    retrieval_top_k: int
    retrieval_min_score: float
    retriever: str
    embedding_model: str
    embedding_query_prefix: str
    embedding_cache_dir: Path
    rrf_k: int
    dense_weight: float
    dense_min_score: float
    hyde_enabled: bool
    relevance_check_enabled: bool
    doc_id_pattern: str
    data_dir: Path
    prompts_dir: Path
    witness_answer_prompt: str
    lead_ins_file: str
    hyde_prompt: str
    relevance_check_prompt: str
    outputs_dir: Path
    processed_dir: Path
    eval_dir: Path
    witnesses_file: Path
    web_dir: Path
    api_host: str
    api_port: int
    ui_audio_buffer_ms: int

    def resolve(self, p: Path) -> Path:
        return p if p.is_absolute() else PROJECT_ROOT / p


def describe_errors(error: ValidationError) -> str:
    """Setting names only: pydantic's own message includes the offending input values,
    which can contain secrets."""
    missing = sorted({str(e["loc"][0]).upper() for e in error.errors() if e["type"] == "missing"})
    invalid = sorted({str(e["loc"][0]).upper() for e in error.errors() if e["type"] != "missing"})
    lines = ["Configuration error (see .env.example):"]
    if missing:
        lines.append("  missing: " + ", ".join(missing))
    if invalid:
        lines.append("  invalid: " + ", ".join(invalid))
    return "\n".join(lines)


def load_settings(**kwargs) -> Settings:
    """Settings(**kwargs), exiting with a list of bad setting names instead of a traceback."""
    try:
        return Settings(**kwargs)
    except ValidationError as e:
        raise SystemExit(describe_errors(e)) from None


@lru_cache
def get_settings() -> Settings:
    return load_settings()
