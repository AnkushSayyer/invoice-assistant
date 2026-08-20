import os
from typing import Any, Literal

import instructor

LLMProvider = Literal["openai", "anthropic", "gemini"]

SUPPORTED_PROVIDERS: tuple[LLMProvider, ...] = ("openai", "anthropic", "gemini")

DEFAULT_MODELS: dict[LLMProvider, str] = {
    "openai": "gpt-4o-mini",
    "anthropic": "claude-3-5-sonnet-latest",
    "gemini": "gemini-3.5-flash",
}

MODEL_ENV_VARS: dict[LLMProvider, str] = {
    "openai": "OPENAI_MODEL",
    "anthropic": "ANTHROPIC_MODEL",
    "gemini": "GEMINI_MODEL",
}

API_KEY_ENV_VARS: dict[LLMProvider, str] = {
    "openai": "OPENAI_API_KEY",
    "anthropic": "ANTHROPIC_API_KEY",
    "gemini": "GEMINI_API_KEY",
}


class MissingLLMApiKeyError(Exception):
    """Raised when the selected provider is missing its API key."""


class ExtractionLLMError(Exception):
    """Raised when the configured LLM provider fails during extraction."""


def get_llm_provider() -> LLMProvider:
    provider = os.getenv("LLM_PROVIDER", "openai").strip().lower()
    if provider not in SUPPORTED_PROVIDERS:
        supported = ", ".join(SUPPORTED_PROVIDERS)
        raise ValueError(f"Unsupported LLM_PROVIDER '{provider}'. Use one of: {supported}")
    return provider


def get_default_model(provider: LLMProvider | None = None) -> str:
    resolved_provider = provider or get_llm_provider()
    return os.getenv(
        MODEL_ENV_VARS[resolved_provider],
        DEFAULT_MODELS[resolved_provider],
    )


def get_gemini_timeout_seconds() -> float:
    return float(os.getenv("GEMINI_TIMEOUT_SECONDS", "90"))


def get_gemini_retry_attempts() -> int:
    return int(os.getenv("GEMINI_RETRY_ATTEMPTS", "2"))


def _create_gemini_client(api_key: str) -> Any:
    from google import genai
    from google.genai import types

    timeout_ms = int(get_gemini_timeout_seconds() * 1000)
    http_options = types.HttpOptions(
        timeout=timeout_ms,
        retry_options=types.HttpRetryOptions(
            attempts=get_gemini_retry_attempts(),
            max_delay=5.0,
        ),
    )
    return genai.Client(api_key=api_key, http_options=http_options)


def format_llm_error_detail(exc: ExtractionLLMError) -> str:
    message = str(exc)
    if "429" in message or "RESOURCE_EXHAUSTED" in message:
        return (
            "Gemini API rate limit exceeded. Wait a minute and retry, "
            "or switch LLM_PROVIDER / GEMINI_MODEL in .env."
        )
    if "503" in message or "UNAVAILABLE" in message:
        return (
            "Gemini model is temporarily unavailable due to high demand. "
            "Retry in a few seconds."
        )
    return message


def llm_http_status_for_error(exc: ExtractionLLMError) -> int:
    message = str(exc)
    if "429" in message or "RESOURCE_EXHAUSTED" in message:
        return 429
    if "503" in message or "UNAVAILABLE" in message:
        return 503
    return 502


def create_instructor_client(provider: LLMProvider | None = None) -> Any:
    resolved_provider = provider or get_llm_provider()
    api_key = os.getenv(API_KEY_ENV_VARS[resolved_provider], "").strip()
    if not api_key:
        raise MissingLLMApiKeyError(
            f"{API_KEY_ENV_VARS[resolved_provider]} is required when "
            f"LLM_PROVIDER={resolved_provider}"
        )

    if resolved_provider == "openai":
        from openai import OpenAI

        return instructor.from_openai(OpenAI(api_key=api_key))

    if resolved_provider == "anthropic":
        import anthropic

        return instructor.from_anthropic(anthropic.Anthropic(api_key=api_key))

    from google import genai

    return instructor.from_genai(_create_gemini_client(api_key))


def is_llm_timeout(exc: Exception) -> bool:
    timeout_types: tuple[type[BaseException], ...] = (TimeoutError,)
    try:
        from openai import APITimeoutError

        timeout_types = (*timeout_types, APITimeoutError)
    except ImportError:
        pass
    try:
        import anthropic

        timeout_types = (*timeout_types, anthropic.APITimeoutError)
    except ImportError:
        pass
    try:
        import httpx

        timeout_types = (*timeout_types, httpx.TimeoutException)
    except ImportError:
        pass
    return isinstance(exc, timeout_types)
