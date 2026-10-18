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
    tts_ws_url: str
    tts_default_voice: str
    tts_timeout_seconds: float
    tts_sample_rate: int
    data_dir: Path
    prompts_dir: Path
    outputs_dir: Path

    def resolve(self, p: Path) -> Path:
        return p if p.is_absolute() else PROJECT_ROOT / p


@lru_cache
def get_settings() -> Settings:
    return Settings()
