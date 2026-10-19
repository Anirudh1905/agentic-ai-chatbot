from pathlib import Path

import pytest
from pydantic import ValidationError

from chatbot.config import Settings


def test_empty_env_values_fall_back_to_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MAX_TOKENS", "")
    monkeypatch.setenv("OPENAI_BASE_URL", "")
    monkeypatch.setenv("OPENAI_API_KEY", "")
    monkeypatch.setenv("MODEL", "")

    settings = Settings(_env_file=None)  # type: ignore[call-arg]

    assert settings.max_tokens is None
    assert settings.openai_base_url is None
    assert settings.openai_api_key is None
    assert settings.model == Settings.model_fields["model"].default


def test_empty_reranker_model_disables_reranking(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RERANKER_MODEL", "")

    assert not Settings(_env_file=None).reranker_model  # type: ignore[call-arg]


def test_a_short_jwt_secret_is_refused() -> None:
    with pytest.raises(ValidationError, match="at least 32 characters"):
        Settings(_env_file=None, jwt_secret="too-short")  # type: ignore[call-arg]


def test_secrets_can_come_from_files(tmp_path: Path) -> None:
    (tmp_path / "jwt_secret").write_text("s" * 40)
    (tmp_path / "openai_api_key").write_text("sk-from-a-file")

    settings = Settings(_env_file=None, _secrets_dir=tmp_path)  # type: ignore[call-arg]

    assert settings.jwt_secret and settings.jwt_secret.get_secret_value() == "s" * 40
    assert settings.openai_api_key
    assert settings.openai_api_key.get_secret_value() == "sk-from-a-file"
