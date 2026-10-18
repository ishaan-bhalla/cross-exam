import requests
from crossexam.config import get_settings


def chat(messages: list[dict], model: str | None = None) -> str:
    s = get_settings()
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
    r.raise_for_status()
    return r.json()["choices"][0]["message"]["content"]
