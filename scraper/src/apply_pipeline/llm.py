from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Any, Protocol

import httpx

from .contracts import PageSnapshot

DEFAULT_OLLAMA_CLOUD_BASE_URL = "https://ollama.com"
DEFAULT_OLLAMA_CLOUD_MODEL = "deepseek-v4-pro"


class LLMAnswerClient(Protocol):
    def resolve_answers(self, payload: dict[str, Any]) -> dict[str, Any]: ...


@dataclass(frozen=True)
class OllamaCloudConfig:
    api_key: str
    base_url: str = DEFAULT_OLLAMA_CLOUD_BASE_URL
    model: str = DEFAULT_OLLAMA_CLOUD_MODEL
    timeout_seconds: float = 30.0


class OllamaCloudAnswerClient:
    def __init__(self, config: OllamaCloudConfig) -> None:
        self.config = config

    def resolve_answers(self, payload: dict[str, Any]) -> dict[str, Any]:
        response = httpx.post(
            f"{self.config.base_url.rstrip('/')}/v1/chat/completions",
            headers={"Authorization": f"Bearer {self.config.api_key}", "Content-Type": "application/json"},
            json={
                "model": self.config.model,
                "temperature": 0,
                "response_format": {"type": "json_object"},
                "messages": [
                    {
                        "role": "system",
                        "content": (
                            "You map job application form fields to answers. Return only JSON. "
                            "Use only the supplied applicant facts, resume_summary, skills, and job description. "
                            "Answer identity/contact fields only when the value is explicitly present in supplied context. Do not guess sensitive/legal answers. If uncertain, mark needs_review."
                        ),
                    },
                    {"role": "user", "content": json.dumps(payload, sort_keys=True)},
                ],
            },
            timeout=self.config.timeout_seconds,
        )
        response.raise_for_status()
        raw = response.json()["choices"][0]["message"]["content"]
        parsed = json.loads(raw)
        if not isinstance(parsed, dict):
            raise ValueError("LLM resolver returned non-object JSON")
        return parsed


def ollama_cloud_client_from_env() -> OllamaCloudAnswerClient | None:
    api_key = os.environ.get("OLLAMA_CLOUD_API_KEY") or os.environ.get("OLLAMA_API_KEY")
    if not api_key:
        return None
    return OllamaCloudAnswerClient(
        OllamaCloudConfig(
            api_key=api_key,
            base_url=os.environ.get("OLLAMA_CLOUD_BASE_URL", DEFAULT_OLLAMA_CLOUD_BASE_URL),
            model=os.environ.get("OLLAMA_CLOUD_MODEL") or os.environ.get("DEEPSEEK_MODEL", DEFAULT_OLLAMA_CLOUD_MODEL),
            timeout_seconds=float(os.environ.get("OLLAMA_CLOUD_TIMEOUT_SECONDS", "30")),
        )
    )


def llm_payload(
    snapshot: PageSnapshot,
    *,
    facts: dict[str, str],
    job_description: str | None,
    eligible_field_ids: set[str] | frozenset[str] | None = None,
) -> dict[str, Any]:
    fields = snapshot.fields if eligible_field_ids is None else tuple(field for field in snapshot.fields if field.id in eligible_field_ids)
    return {
        "policy": {
            "answer_only_from_supplied_context": True,
            "return_needs_review_when_uncertain": True,
            "never_answer_sensitive_or_legal_fields": True,
            "never_click_final_submit": True,
        },
        "job_description": job_description or "",
        "applicant_facts": facts,
        "fields": [
            {
                "id": field.id,
                "kind": field.kind,
                "label": field.label,
                "required": field.required,
                "options": list(field.options),
            }
            for field in fields
        ],
        "buttons": [
            {
                "id": button.id,
                "text": button.text,
                "type": button.type,
                "disabled": button.disabled,
                "final_submit_candidate": button.final_submit_candidate,
            }
            for button in snapshot.buttons
        ],
        "required_output_schema": {
            "answers": [{"field_id": "string", "value": "string|boolean|array", "confidence": "high|low"}],
            "needs_review": ["string"],
        },
    }
