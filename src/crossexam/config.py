import os
from functools import lru_cache
from pathlib import Path
from pydantic import ValidationError
from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parents[2]
# Settings come from this file, and real environment variables override it. Locally it is .env;
# the Docker image sets CROSSEXAM_ENV_FILE=deploy/render.env (committed, no secrets), so on Render
# only ASSEMBLYAI_API_KEY needs to be set in the environment.
ENV_FILE = PROJECT_ROOT / os.environ.get("CROSSEXAM_ENV_FILE", ".env")


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
    llm_max_wait_seconds: float
    stt_ws_url: str
    stt_token_url: str
    stt_speech_model: str
    stt_sample_rate: int
    stt_chunk_ms: int
    stt_token_expires_seconds: int
    stt_token_refresh_margin_seconds: int
    stt_max_session_seconds: int
    stt_max_keyterms: int
    stt_submit_grace_ms: int
    stt_min_turn_silence_ms: str
    stt_max_turn_silence_ms: str
    stt_end_of_turn_confidence: str
    doc_id_snap_max_distance: int
    tts_ws_url: str
    tts_default_voice: str
    tts_voices: str
    tts_timeout_seconds: float
    tts_sample_rate: int
    tts_session_retries: int
    tts_session_retry_backoff_seconds: float
    tts_silence_threshold: int
    tts_trim_preroll_ms: int
    tts_trim_tail_ms: int
    tts_chunk_max_words: int
    tts_chunk_gap_ms: int
    tts_lead_in_gap_ms: int
    tts_keepalive_seconds: float
    tts_words_wait_seconds: float
    quote_timings_max_wait_ms: int
    phrase_cache_dir: str
    quote_max_words: int
    retrieval_top_k: int
    reranker_enabled: bool
    reranker_model: str
    rerank_candidates: int
    retrieval_min_score: float
    retriever: str
    embedding_model: str
    embedding_batch_size: int
    embedding_threads: str
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
    quote_select_prompt: str
    ocr_verify_enabled: bool
    ocr_verify_max_edit_ratio: float
    ocr_verify_max_edits: int
    ocr_verify_max_merge: int
    ocr_verify_min_exact_fraction: float
    ocr_verify_confusable: str
    outputs_dir: Path
    processed_dir: Path
    statements_dir: Path
    ocr_enabled: bool
    tesseract_cmd: str
    ocr_dpi: int
    ocr_min_chars: int
    ocr_max_junk_ratio: float
    parser_running_line_min_fraction: float
    parser_edge_lines: int
    parser_max_para_gap: int
    parser_jump_lookahead_lines: int
    parser_gap_ocr_max_pages: int
    parser_heading_max_words: int
    eval_dir: Path
    upload_max_mb: int
    upload_workers: int
    persist_uploads: bool
    uploads_dir: Path
    uploads_per_client: int
    max_open_uploads: int
    upload_ttl_minutes: int
    conversation_max_turns: int
    profile_prompt: str
    profile_opening_units: int
    profile_max_roles: int
    profile_max_tokens: int
    keyterms_min_acronym_count: int
    subunit_context_enabled: bool
    subunit_context_max_words: int
    witnesses_file: Path
    web_dir: Path
    api_host: str
    api_port: int
    ui_audio_buffer_ms: int
    cost_llm_input_per_million_tokens: str
    cost_llm_output_per_million_tokens: str
    cost_tts_per_million_chars: str
    cost_stt_per_hour: str
    demo_paused: bool
    demo_video_url: str
    budget_llm_calls_per_day: int
    budget_tts_chars_per_day: int
    budget_stt_seconds_per_day: int
    budget_stt_sessions_per_day: int
    budget_cost_usd_per_day: float
    budget_state_file: str
    limit_questions_per_minute: int
    limit_questions_per_day: int
    limit_spoken_seconds_per_day: int
    limit_stt_sessions_per_day: int
    limit_uploads_per_day: int
    limit_ip_factor: int
    client_ip_header: str
    client_ip_position: str

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
