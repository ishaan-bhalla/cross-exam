import pytest
from dotenv import dotenv_values
from crossexam.config import PROJECT_ROOT, Settings, load_settings

SECRET = "sk-very-secret-value"


def test_every_setting_is_documented_in_env_example():
    documented = {k.lower() for k in dotenv_values(PROJECT_ROOT / ".env.example")}
    missing = sorted(set(Settings.model_fields) - documented)
    assert not missing, f"add to .env.example: {', '.join(m.upper() for m in missing)}"


@pytest.fixture
def clean_env(monkeypatch):
    for name in Settings.model_fields:
        monkeypatch.delenv(name.upper(), raising=False)
        monkeypatch.delenv(name, raising=False)


def _example_with(tmp_path, **overrides):
    values = {k: (v or "") for k, v in dotenv_values(PROJECT_ROOT / ".env.example").items()}
    values.update(overrides)
    for k in [k for k, v in overrides.items() if v is None]:
        del values[k]
    path = tmp_path / "test.env"
    path.write_text("".join(f"{k}='{v}'\n" for k, v in values.items()))
    return path


def test_config_error_lists_names_without_values(tmp_path, clean_env, capsys):
    env = _example_with(tmp_path, ASSEMBLYAI_API_KEY=SECRET, API_PORT="not-a-port", RRF_K=None)
    with pytest.raises(SystemExit) as exc:
        load_settings(_env_file=env)
    message = str(exc.value)
    assert "missing: RRF_K" in message
    assert "invalid: API_PORT" in message
    assert SECRET not in message and "not-a-port" not in message


def test_valid_example_loads(tmp_path, clean_env):
    s = load_settings(_env_file=_example_with(tmp_path, ASSEMBLYAI_API_KEY=SECRET))
    assert s.assemblyai_api_key == SECRET
