import json

import pytest

from jobs_assistant.contracts import PageSnapshot
from jobs_assistant.llm import UnconfiguredLLM, build_resolver_prompt


def test_llm_prompt_has_strict_json_contract():
    prompt = build_resolver_prompt(PageSnapshot(url="x"), facts={"full_name": "Ian"}, policy="never submit")
    payload = json.loads(prompt)
    assert payload["required_output"]["answers"] == "array of field_id/value pairs"
    assert payload["required_output"]["needs_review"].startswith("boolean")


def test_unconfigured_llm_fails_closed():
    with pytest.raises(RuntimeError):
        UnconfiguredLLM().resolve_json("{}")
