import pytest

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
    assert settings.model == "gpt-5-mini"


def test_empty_reranker_model_disables_reranking(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RERANKER_MODEL", "")

    assert not Settings(_env_file=None).reranker_model  # type: ignore[call-arg]
