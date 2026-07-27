"""Optional Ollama advisory ranking for deterministic resume generation.

The model may rank existing profile claim IDs only. It cannot provide resume text,
new skills, sources, or facts. Any missing credential, transport error, malformed
response, or unknown ID returns ``None`` so deterministic generation is unchanged.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Any, Mapping
from urllib.parse import urlsplit

import httpx

from .resume_generator import ResumeJob, ResumeProfile

_MAX_REQUEST_BYTES = 256 * 1024
_MAX_RESPONSE_BYTES = 1024 * 1024
_MAX_CLAIMS = 256
_MAX_COURSEWORK = 8
_DEFAULT_MODEL = "deepseek-v4-flash"
_DEFAULT_BASE_URL = "https://ollama.com"
_DEFAULT_THINK = "low"
_ENABLE_ENV = "RESUME_ADVISORY_ENABLED"

_ADVISORY_SYSTEM_PROMPT = (
    "You are a resume-ranking assistant. Return exactly one JSON object and no markdown. "
    "Use only the supplied job and profile claim IDs. Never invent facts, skills, text, "
    "sources, or IDs. Rank claims by relevance; omit claims that are not useful. "
    "Return exactly the keys version, ranked_claim_ids, and ranked_coursework."
)


@dataclass(frozen=True)
class ResumeAdvice:
    """Validated ordering hints containing only authoritative profile values."""

    ranked_claim_ids: tuple[str, ...]
    ranked_coursework: tuple[str, ...]

    @property
    def claim_rank(self) -> Mapping[str, int]:
        return {value: index for index, value in enumerate(self.ranked_claim_ids)}


def _enabled(api_key: str | None) -> bool:
    if api_key:
        return True
    return os.environ.get(_ENABLE_ENV, "").strip().casefold() in {"1", "true", "yes"}


def _claim_rows(profile: ResumeProfile) -> tuple[list[dict[str, Any]], set[str], tuple[str, ...]]:
    rows: list[dict[str, Any]] = []
    allowed: set[str] = set()
    coursework: list[str] = []

    def add_entry(entry: Any, category: str) -> None:
        allowed.add(entry.id)
        rows.append({
            "id": entry.id,
            "category": category,
            "title": getattr(entry, "title", getattr(entry, "name", "")),
            "keywords": list(getattr(entry, "keywords", ())),
            "text": " ".join(
                [
                    getattr(entry, "organization", ""),
                    getattr(entry, "name", ""),
                    *[bullet.text for bullet in getattr(entry, "bullets", ())],
                ]
            )[:4000],
        })
        for bullet in getattr(entry, "bullets", ()):
            allowed.add(bullet.id)
            rows.append({
                "id": bullet.id,
                "category": f"{category}_bullet",
                "keywords": list(bullet.keywords),
                "text": bullet.text[:2000],
            })

    for entry in profile.experience:
        add_entry(entry, "experience")
    for entry in profile.leadership:
        add_entry(entry, "leadership")
    for entry in profile.projects:
        add_entry(entry, "project")
    for category, entries in sorted(profile.skills.items(), key=lambda item: (item[0].casefold(), item[0])):
        for entry in entries:
            claim_id = f"skill:{category}:{entry.name}"
            allowed.add(claim_id)
            rows.append({
                "id": claim_id,
                "category": "skill",
                "title": entry.name,
                "keywords": list(entry.keywords),
            })
    for entry in profile.education:
        coursework.extend(entry.coursework)
    return rows, allowed, tuple(dict.fromkeys(coursework))


def build_resume_advisory_request(profile: ResumeProfile, job: ResumeJob) -> dict[str, Any]:
    rows, _allowed, coursework = _claim_rows(profile)
    return {
        "job": {
            "title": job.title,
            "company": job.company,
            "location": job.location or "",
            "description": job.description[:12000],
        },
        "claims": rows,
        "coursework": list(coursework),
    }


def _content(payload: Mapping[str, Any]) -> Any:
    message = payload.get("message")
    if isinstance(message, Mapping) and isinstance(message.get("content"), str):
        return json.loads(message["content"])
    choices = payload.get("choices")
    if isinstance(choices, list) and choices and isinstance(choices[0], Mapping):
        item = choices[0].get("message")
        if isinstance(item, Mapping) and isinstance(item.get("content"), str):
            return json.loads(item["content"])
    raise ValueError("missing model content")


def _endpoint(base_url: str) -> str:
    endpoint = base_url.rstrip("/") + "/api/chat"
    parsed = urlsplit(endpoint)
    local = parsed.hostname in {"localhost", "127.0.0.1", "::1"}
    if parsed.scheme != "https" and not (local and parsed.scheme == "http"):
        raise ValueError("unsafe Ollama endpoint")
    if parsed.username or parsed.password or not parsed.hostname or parsed.query or parsed.fragment:
        raise ValueError("unsafe Ollama endpoint")
    return endpoint


def _validate_advice(payload: Any, allowed: set[str], coursework: tuple[str, ...]) -> ResumeAdvice:
    if not isinstance(payload, Mapping) or set(payload) != {"version", "ranked_claim_ids", "ranked_coursework"}:
        raise ValueError("invalid advisory schema")
    if payload["version"] != 1:
        raise ValueError("unsupported advisory version")
    claim_ids = payload["ranked_claim_ids"]
    course_values = payload["ranked_coursework"]
    if not isinstance(claim_ids, list) or not isinstance(course_values, list):
        raise ValueError("invalid advisory arrays")
    if len(claim_ids) > _MAX_CLAIMS or len(course_values) > _MAX_COURSEWORK:
        raise ValueError("advisory arrays too large")
    if any(type(value) is not str or not value or value not in allowed for value in claim_ids):
        raise ValueError("unknown advisory claim")
    if len(set(claim_ids)) != len(claim_ids):
        raise ValueError("duplicate advisory claim")
    if any(type(value) is not str or value not in coursework for value in course_values):
        raise ValueError("unknown advisory coursework")
    if len(set(course_values)) != len(course_values):
        raise ValueError("duplicate advisory coursework")
    return ResumeAdvice(tuple(claim_ids), tuple(course_values))


def request_resume_advice(
    profile: ResumeProfile,
    job: ResumeJob,
    *,
    api_key: str | None = None,
    base_url: str | None = None,
    model: str | None = None,
) -> ResumeAdvice | None:
    """Request a bounded ranking hint; return ``None`` on every advisory failure."""
    if not _enabled(api_key):
        return None
    token = api_key or os.environ.get("OLLAMA_CLOUD_API_KEY") or os.environ.get("OLLAMA_API_KEY")
    if not token:
        return None
    try:
        request = build_resume_advisory_request(profile, job)
        body = {
            "model": model or os.environ.get("OLLAMA_CLOUD_MODEL") or os.environ.get("DEEPSEEK_MODEL") or _DEFAULT_MODEL,
            "messages": [
                {"role": "system", "content": _ADVISORY_SYSTEM_PROMPT},
                {"role": "user", "content": json.dumps(request, ensure_ascii=False, separators=(",", ":"))},
            ],
            "think": os.environ.get("OLLAMA_CLOUD_THINK") or os.environ.get("OLLAMA_CLOUD_REASONING") or _DEFAULT_THINK,
            "stream": False,
            "format": {
                "type": "object",
                "properties": {
                    "version": {"type": "integer"},
                    "ranked_claim_ids": {"type": "array", "items": {"type": "string"}},
                    "ranked_coursework": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["version", "ranked_claim_ids", "ranked_coursework"],
                "additionalProperties": False,
            },
        }
        encoded = json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        if len(encoded) > _MAX_REQUEST_BYTES:
            return None
        endpoint = _endpoint(base_url or os.environ.get("OLLAMA_CLOUD_BASE_URL") or _DEFAULT_BASE_URL)
        with httpx.Client(trust_env=False, follow_redirects=False, timeout=60.0) as client:
            response = client.post(
                endpoint,
                headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
                content=encoded,
            )
            response.raise_for_status()
            raw = response.content
            if len(raw) > _MAX_RESPONSE_BYTES:
                return None
            payload = response.json()
        _rows, allowed, coursework = _claim_rows(profile)
        return _validate_advice(_content(payload), allowed, coursework)
    except (httpx.HTTPError, OSError, TypeError, ValueError, json.JSONDecodeError):
        return None
