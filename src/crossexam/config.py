from functools import lru_cache
from pathlib import Path
from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=PROJECT_ROOT / ".env", extra="ignore")

    assemblyai_api_key: str
    llm_gateway_url: str
    llm_model: str
    llm_max_tokens: int
    llm_timeout_seconds: float
    llm_max_retries: int
    llm_retry_backoff_seconds: float
    tts_ws_url: str
    tts_default_voice: str
    tts_timeout_seconds: float
    tts_sample_rate: int
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
    data_dir: Path
    prompts_dir: Path
    witness_answer_prompt: str
    lead_ins_file: str
    hyde_prompt: str
    relevance_check_prompt: str
    outputs_dir: Path
    processed_dir: Path
    eval_dir: Path

    def resolve(self, p: Path) -> Path:
        return p if p.is_absolute() else PROJECT_ROOT / p


@lru_cache
def get_settings() -> Settings:
    return Settings()
