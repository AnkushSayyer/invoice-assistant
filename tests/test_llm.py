import os
from unittest.mock import MagicMock, patch

import pytest

from app.services.llm import (
    MissingLLMApiKeyError,
    create_instructor_client,
    format_llm_error_detail,
    get_default_model,
    get_llm_provider,
    llm_http_status_for_error,
    ExtractionLLMError,
)


def test_get_llm_provider_defaults_to_openai(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("LLM_PROVIDER", raising=False)
    assert get_llm_provider() == "openai"


def test_get_default_model_for_gemini(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LLM_PROVIDER", "gemini")
    monkeypatch.delenv("GEMINI_MODEL", raising=False)
    assert get_default_model() == "gemini-3.5-flash"


@patch("app.services.llm.instructor.from_genai")
@patch("app.services.llm._create_gemini_client")
def test_create_instructor_client_uses_gemini(
    mock_create_gemini_client: MagicMock,
    mock_from_genai: MagicMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LLM_PROVIDER", "gemini")
    monkeypatch.setenv("GEMINI_API_KEY", "test-gemini-key")
    mock_create_gemini_client.return_value = MagicMock()

    create_instructor_client()

    mock_create_gemini_client.assert_called_once_with("test-gemini-key")
    mock_from_genai.assert_called_once_with(mock_create_gemini_client.return_value)


def test_format_llm_error_detail_for_rate_limit() -> None:
    detail = format_llm_error_detail(
        ExtractionLLMError("429 RESOURCE_EXHAUSTED quota exceeded")
    )
    assert "rate limit" in detail.lower()


def test_llm_http_status_for_error_maps_rate_limit() -> None:
    assert (
        llm_http_status_for_error(
            ExtractionLLMError("429 RESOURCE_EXHAUSTED quota exceeded")
        )
        == 429
    )


@patch("app.services.llm.instructor.from_anthropic")
def test_create_instructor_client_uses_anthropic(
    mock_from_anthropic: MagicMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LLM_PROVIDER", "anthropic")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-anthropic-key")

    create_instructor_client()

    mock_from_anthropic.assert_called_once()


def test_create_instructor_client_requires_api_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LLM_PROVIDER", "gemini")
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)

    with pytest.raises(MissingLLMApiKeyError):
        create_instructor_client()
