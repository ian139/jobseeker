from __future__ import annotations

import json
from dataclasses import asdict
from typing import Any, Mapping, Sequence

import httpx

from job_scraper.resume import ResumeLLM, SelectedBullet


class ResumeLLMError(RuntimeError):
    pass


class OpenAIResumeLLM:
    model_name: str

    def __init__(self, api_key: str, model: str, timeout_seconds: float = 30.0) -> None:
        self._api_key = api_key
        self.model_name = model
        self._timeout_seconds = timeout_seconds

    def rewrite(
        self,
        *,
        draft_markdown: str,
        job: Mapping[str, Any],
        selected_bullets: Sequence[SelectedBullet],
    ) -> str:
        payload = {
            "model": self.model_name,
            "temperature": 0.2,
            "messages": [
                {
                    "role": "system",
                    "content": (
                        "Rewrite this resume in Markdown using only the supplied resume facts and selected bullets. "
                        "Do not invent employers, metrics, tools, dates, education, certifications, or contact details. "
                        "Preserve Markdown section structure. Return Markdown only."
                    ),
                },
                {
                    "role": "user",
                    "content": (
                        "Draft Markdown:\n"
                        f"{draft_markdown}\n\n"
                        "Job JSON:\n"
                        f"{json.dumps(job, default=str, separators=(',', ':'), sort_keys=True)}\n\n"
                        "Selected bullet JSON:\n"
                        f"{json.dumps([asdict(bullet) for bullet in selected_bullets], separators=(',', ':'), sort_keys=True)}"
                    ),
                },
            ],
        }
        headers = {"Authorization": f"Bearer {self._api_key}", "Content-Type": "application/json"}
        try:
            response = httpx.post(
                "https://api.openai.com/v1/chat/completions",
                headers=headers,
                json=payload,
                timeout=self._timeout_seconds,
            )
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise ResumeLLMError(f"OpenAI request failed with status {exc.response.status_code}") from exc
        except httpx.HTTPError as exc:
            raise ResumeLLMError(str(exc)) from exc

        data = response.json()
        content = _message_content(data)
        if content is None:
            raise ResumeLLMError("OpenAI response did not contain Markdown resume content")
        return content


def _message_content(data: Mapping[str, Any]) -> str | None:
    choices = data.get("choices")
    if not isinstance(choices, list) or not choices:
        return None
    first = choices[0]
    if not isinstance(first, Mapping):
        return None
    message = first.get("message")
    if not isinstance(message, Mapping):
        return None
    content = message.get("content")
    if not isinstance(content, str):
        return None
    content = content.strip()
    if not content or "# " not in content or "## " not in content:
        return None
    return content


__all__ = ["OpenAIResumeLLM", "ResumeLLM", "ResumeLLMError"]
