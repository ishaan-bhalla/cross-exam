import time
import requests
from crossexam.config import get_settings


def chat(messages: list[dict], model: str | None = None) -> str:
    """Retries on 429 (rate limit), waiting Retry-After if given, else exponential backoff."""
    s = get_settings()
    for attempt in range(s.llm_max_retries + 1):
        r = requests.post(
            s.llm_gateway_url,
            headers={"authorization": s.assemblyai_api_key},
            json={
                "model": model or s.llm_model,
                "messages": messages,
                "max_tokens": s.llm_max_tokens,
            },
            timeout=s.llm_timeout_seconds,
        )
        if r.status_code != 429 or attempt == s.llm_max_retries:
            break
        retry_after = r.headers.get("Retry-After", "")
        time.sleep(float(retry_after) if retry_after.isdigit() else s.llm_retry_backoff_seconds * 2 ** attempt)
    r.raise_for_status()
    return r.json()["choices"][0]["message"]["content"]
