from __future__ import annotations

import httpx

from job_scraper.llm import ChatCompletionsResumeLLM, ResumeLLMError
from job_scraper.config import AppSettings
from job_scraper.resume import SelectedBullet


def test_llm_uses_configured_chat_completions_base_url(monkeypatch) -> None:
    calls: list[dict[str, object]] = []

    def fake_post(url: str, **kwargs: object) -> httpx.Response:
        calls.append({"url": url, **kwargs})
        request = httpx.Request("POST", url)
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": "# Ada Candidate\n\n## Experience\nBuilt systems."}}]},
            request=request,
        )

    monkeypatch.setattr("job_scraper.llm.httpx.post", fake_post)
    llm = ChatCompletionsResumeLLM(
        "openrouter-key",
        "deepseek/deepseek-v4-pro",
        base_url="https://openrouter.ai/api/v1/",
    )

    result = llm.rewrite(
        draft_markdown="# Ada Candidate\n\n## Experience\nBuilt services.",
        job={"job_title": "Frontend Engineer"},
        selected_bullets=[SelectedBullet(section="Experience", item_title="Role", text="Built services.", score=3, matched_terms=("services",))],
    )

    assert result.startswith("# Ada Candidate")
    assert calls[0]["url"] == "https://openrouter.ai/api/v1/chat/completions"
    assert calls[0]["headers"] == {"Authorization": "Bearer openrouter-key", "Content-Type": "application/json"}
    assert calls[0]["json"]["model"] == "deepseek/deepseek-v4-pro"  # type: ignore[index]


def test_llm_error_message_is_provider_neutral(monkeypatch) -> None:
    def fake_post(url: str, **kwargs: object) -> httpx.Response:
        request = httpx.Request("POST", url)
        return httpx.Response(429, request=request)

    monkeypatch.setattr("job_scraper.llm.httpx.post", fake_post)
    llm = ChatCompletionsResumeLLM("key", "deepseek/deepseek-v4-pro", base_url="https://openrouter.ai/api/v1")

    try:
        llm.rewrite(
            draft_markdown="# Ada Candidate\n\n## Experience\nBuilt services.",
            job={},
            selected_bullets=[],
        )
    except ResumeLLMError as exc:
        assert str(exc) == "LLM request failed with status 429"
    else:
        raise AssertionError("Expected ResumeLLMError")


def test_settings_use_general_llm_env_names(monkeypatch) -> None:
    monkeypatch.setenv("LLM_API_KEY", "llm-key")
    monkeypatch.setenv("LLM_MODEL", "deepseek/deepseek-v4-pro")
    monkeypatch.setenv("LLM_BASE_URL", "https://openrouter.ai/api/v1")

    settings = AppSettings(_env_file=None)

    assert settings.llm_api_key == "llm-key"
    assert settings.llm_model == "deepseek/deepseek-v4-pro"
    assert settings.llm_base_url == "https://openrouter.ai/api/v1"


def test_settings_accept_legacy_openai_env_names(monkeypatch) -> None:
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    monkeypatch.delenv("LLM_MODEL", raising=False)
    monkeypatch.delenv("LLM_BASE_URL", raising=False)
    monkeypatch.setenv("OPENAI_API_KEY", "legacy-key")
    monkeypatch.setenv("OPENAI_MODEL", "gpt-4o-mini")
    monkeypatch.setenv("OPENAI_BASE_URL", "https://api.openai.com/v1")

    settings = AppSettings(_env_file=None)

    assert settings.llm_api_key == "legacy-key"
    assert settings.llm_model == "gpt-4o-mini"
    assert settings.llm_base_url == "https://api.openai.com/v1"
