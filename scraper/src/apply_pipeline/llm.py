from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

import httpx

from .contracts import PageSnapshot

DEFAULT_OLLAMA_CLOUD_BASE_URL = "https://ollama.com"
DEFAULT_OLLAMA_CLOUD_MODEL = "deepseek-v4-pro"
APPLY_LLM_SKILL_PATH_ENV = "APPLY_LLM_SKILL_PATH"
DEFAULT_APPLY_LLM_SKILL_RELATIVE_PATH = Path("skills") / "SKILL.md"


def default_apply_llm_skill_path() -> Path:
    for parent in Path(__file__).resolve().parents:
        candidate = parent / DEFAULT_APPLY_LLM_SKILL_RELATIVE_PATH
        if candidate.exists():
            return candidate
    return Path("/app") / DEFAULT_APPLY_LLM_SKILL_RELATIVE_PATH


def load_apply_llm_skill(path: str | Path | None = None) -> str:
    skill_path = Path(path) if path is not None else default_apply_llm_skill_path()
    return skill_path.read_text(encoding="utf-8").rstrip()


class LLMAnswerClient(Protocol):
    def resolve_answers(self, payload: dict[str, Any]) -> dict[str, Any]: ...


@dataclass(frozen=True)
class OllamaCloudConfig:
    api_key: str
    base_url: str = DEFAULT_OLLAMA_CLOUD_BASE_URL
    model: str = DEFAULT_OLLAMA_CLOUD_MODEL
    timeout_seconds: float = 90.0
    skill_text: str = ""


class OllamaCloudAnswerClient:
    def __init__(self, config: OllamaCloudConfig) -> None:
        self.config = config

    def _messages(self, payload: dict[str, Any]) -> list[dict[str, str]]:
        system_prompt = (
            "You map a normalized job application DOM snapshot to field answers and safe navigation choices. "
            "Use only the supplied applicant facts, resume_summary, skills, and job description. "
            "Answer only fields marked eligible_for_answer. Treat typeahead fields with empty options as fillable free-text controls. "
            "For non-sensitive experience-year fields, derive concise numeric answers from supplied resume date ranges when clear. "
            "Choose next_button_id for non-final navigation only. Do not guess sensitive/legal answers and never perform final submission. "
            "If uncertain, mark needs_review."
        )
        if self.config.skill_text:
            system_prompt = (
                f"{system_prompt}\n\n"
                "<live-proof-routing-skill>\n"
                f"{self.config.skill_text}\n"
                "</live-proof-routing-skill>"
            )
        system_prompt = (
            f"{system_prompt}\n\n"
            "Return only JSON matching the payload required_output_schema; never produce prose."
        )
        return [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": json.dumps(payload, sort_keys=True)},
        ]

    def _uses_native_ollama_cloud_api(self) -> bool:
        base_url = self.config.base_url.rstrip("/")
        return "ollama.com" in base_url and not base_url.endswith("/v1")

    def _native_ollama_cloud_url(self) -> str:
        base_url = self.config.base_url.rstrip("/")
        if base_url.endswith("/api"):
            return f"{base_url}/chat"
        return f"{base_url}/api/chat"

    def _request_model(self) -> str:
        if self._uses_native_ollama_cloud_api() and "/" in self.config.model:
            return self.config.model.rsplit("/", 1)[-1]
        return self.config.model

    def resolve_answers(self, payload: dict[str, Any]) -> dict[str, Any]:
        if self._uses_native_ollama_cloud_api():
            response = httpx.post(
                self._native_ollama_cloud_url(),
                headers={"Authorization": f"Bearer {self.config.api_key}", "Content-Type": "application/json"},
                json={
                    "model": self._request_model(),
                    "messages": self._messages(payload),
                    "stream": False,
                    "format": "json",
                    "options": {"temperature": 0},
                },
                timeout=self.config.timeout_seconds,
            )
            response.raise_for_status()
            raw = response.json()["message"]["content"]
        else:
            response = httpx.post(
                f"{self.config.base_url.rstrip('/')}/v1/chat/completions",
                headers={"Authorization": f"Bearer {self.config.api_key}", "Content-Type": "application/json"},
                json={
                    "model": self._request_model(),
                    "temperature": 0,
                    "response_format": {"type": "json_object"},
                    "messages": self._messages(payload),
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
    skill_path = os.environ.get(APPLY_LLM_SKILL_PATH_ENV)
    return OllamaCloudAnswerClient(
        OllamaCloudConfig(
            api_key=api_key,
            base_url=os.environ.get("OLLAMA_CLOUD_BASE_URL", DEFAULT_OLLAMA_CLOUD_BASE_URL),
            model=os.environ.get("OLLAMA_CLOUD_MODEL") or os.environ.get("DEEPSEEK_MODEL", DEFAULT_OLLAMA_CLOUD_MODEL),
            timeout_seconds=float(os.environ.get("OLLAMA_CLOUD_TIMEOUT_SECONDS", "90")),
            skill_text=load_apply_llm_skill(skill_path),
        )
    )


def llm_payload(
    snapshot: PageSnapshot,
    *,
    facts: dict[str, str],
    job_description: str | None,
    eligible_field_ids: set[str] | frozenset[str] | None = None,
) -> dict[str, Any]:
    eligible = None if eligible_field_ids is None else set(eligible_field_ids)
    return {
        "policy": {
            "answer_only_from_supplied_context": True,
            "return_needs_review_when_uncertain": True,
            "never_answer_sensitive_or_legal_fields": True,
            "never_click_final_submit": True,
        },
        "job_description": job_description or "",
        "applicant_facts": facts,
        "eligible_field_ids": sorted(eligible) if eligible is not None else [field.id for field in snapshot.fields],
        "fields": [
            {
                "id": field.id,
                "kind": field.kind,
                "label": field.label,
                "required": field.required,
                "options": list(field.options),
                "value": field.value,
                "disabled": field.disabled,
                "visible": field.visible,
                "frame": field.frame,
                "eligible_for_answer": True if eligible is None else field.id in eligible,
            }
            for field in snapshot.fields
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
            "answers": [{"field_id": "string", "value": "string|boolean|array", "confidence": "high|low", "source_reason": "string"}],
            "next_button_id": "string|null",
            "submit_button_id": "string|null",
            "needs_review": [{"field_id": "string", "reason": "string"}],
        },
    }
