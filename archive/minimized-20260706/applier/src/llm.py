from __future__ import annotations

import json
from dataclasses import asdict
from typing import Protocol

from .contracts import PageSnapshot


class ResolverLLM(Protocol):
    def resolve_json(self, prompt: str) -> dict[str, object]: ...


class UnconfiguredLLM:
    def resolve_json(self, prompt: str) -> dict[str, object]:
        raise RuntimeError("LLM resolver is not configured; deterministic guardrails must handle the page or return needs_review/blocked.")


def build_resolver_prompt(snapshot: PageSnapshot, *, facts: dict[str, object], policy: str) -> str:
    payload = {
        "snapshot": asdict(snapshot),
        "facts": facts,
        "policy": policy,
        "required_output": {
            "answers": "array of field_id/value pairs",
            "next_button": "non-final navigation button id or null",
            "submit_button": "final submit button id or null",
            "needs_review": "boolean with reasons for unknown/sensitive/manual fields",
            "metadata": "object",
        },
    }
    return json.dumps(payload, sort_keys=True)
