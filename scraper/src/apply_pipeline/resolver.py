from __future__ import annotations

import re
from typing import Any

from .contracts import PageSnapshot, ResolvedAnswer, ResolverOutput
from .llm import LLMAnswerClient, llm_payload
from .policy import SENSITIVE_FIELD_RE, is_final_submit_text, is_safe_navigation_text

COMMON_FACT_KEYS = {
    "first name": "first_name",
    "last name": "last_name",
    "full name": "full_name",
    "name": "full_name",
    "email": "email",
    "phone": "phone",
    "mobile": "phone",
    "city": "city",
    "location": "location",
    "linkedin": "linkedin",
    "github": "github",
    "portfolio": "portfolio",
    "website": "portfolio",
}


def normalized(value: str) -> str:
    return " ".join(value.lower().split())


def fact_for_label(label: str, facts: dict[str, str]) -> str | None:
    label_norm = normalized(label)
    for phrase, fact_key in COMMON_FACT_KEYS.items():
        if phrase in label_norm and facts.get(fact_key):
            return facts[fact_key]
    return None


def llm_eligible_field_ids(snapshot: PageSnapshot, *, already_answered: set[str]) -> tuple[str, ...]:
    return tuple(
        field.id
        for field in snapshot.fields
        if field.required
        and field.id not in already_answered
        and field.kind != "file"
        and not SENSITIVE_FIELD_RE.search(field.label)
    )


def resolve_snapshot(
    snapshot: PageSnapshot,
    *,
    facts: dict[str, str],
    resume_path: str | None = None,
    job_description: str | None = None,
    llm_client: LLMAnswerClient | None = None,
) -> ResolverOutput:
    answers: list[ResolvedAnswer] = []
    needs_review: list[str] = []

    for field in snapshot.fields:
        if SENSITIVE_FIELD_RE.search(field.label):
            needs_review.append(f"sensitive field: {field.label}")
            continue
        if field.kind == "file":
            if resume_path and re.search(r"resume|cv", field.label, re.IGNORECASE):
                answers.append(ResolvedAnswer(field.id, resume_path))
            elif field.required:
                needs_review.append(f"unknown required file upload: {field.label}")
            continue
        value = fact_for_label(field.label, facts)
        if value is not None:
            answers.append(ResolvedAnswer(field.id, value))
        elif field.required:
            needs_review.append(f"unknown required field: {field.label}")

    if llm_client is not None and needs_review:
        llm_answers, llm_review = resolve_unknowns_with_llm(
            snapshot,
            facts=facts,
            job_description=job_description,
            llm_client=llm_client,
            already_answered={answer.field_id for answer in answers},
        )
        answers.extend(llm_answers)
        if llm_answers:
            fields_by_id = {field.id: field for field in snapshot.fields}
            answered_labels = {fields_by_id[answer.field_id].label for answer in llm_answers if answer.field_id in fields_by_id}
            needs_review = [reason for reason in needs_review if not any(label in reason for label in answered_labels)]
        needs_review.extend(llm_review)

    submit_button_id = None
    next_button_id = None
    for button in snapshot.buttons:
        if button.disabled:
            continue
        if button.final_submit_candidate or is_final_submit_text(button.text):
            submit_button_id = button.id
            break
        if next_button_id is None and is_safe_navigation_text(button.text):
            next_button_id = button.id

    return ResolverOutput(
        answers=tuple(answers),
        next_button_id=next_button_id,
        submit_button_id=submit_button_id,
        needs_review=tuple(dict.fromkeys(needs_review)),
    )


def resolve_unknowns_with_llm(
    snapshot: PageSnapshot,
    *,
    facts: dict[str, str],
    job_description: str | None,
    llm_client: LLMAnswerClient,
    already_answered: set[str],
) -> tuple[list[ResolvedAnswer], list[str]]:
    eligible_field_ids = set(llm_eligible_field_ids(snapshot, already_answered=already_answered))
    if not eligible_field_ids:
        return [], []
    fields_by_id = {field.id: field for field in snapshot.fields}
    try:
        raw = llm_client.resolve_answers(
            llm_payload(snapshot, facts=facts, job_description=job_description, eligible_field_ids=eligible_field_ids)
        )
    except Exception as exc:
        return [], [f"LLM resolver failed: {exc}"]

    answers: list[ResolvedAnswer] = []
    needs_review: list[str] = []
    for item in raw.get("answers", []):
        if not isinstance(item, dict):
            needs_review.append("LLM resolver returned malformed answer")
            continue
        field_id = str(item.get("field_id") or "")
        field = fields_by_id.get(field_id)
        if field is None or field_id in already_answered or field_id not in eligible_field_ids:
            continue
        if SENSITIVE_FIELD_RE.search(field.label):
            needs_review.append(f"sensitive field: {field.label}")
            continue
        if field.kind == "file":
            continue
        if str(item.get("confidence") or "").lower() not in {"high", "1", "true"}:
            needs_review.append(f"LLM low confidence field: {field.label}")
            continue
        value = item.get("value")
        if value is None or value == "":
            needs_review.append(f"LLM empty answer field: {field.label}")
            continue
        if field.options:
            allowed = {option.casefold() for option in field.options}
            values = value if isinstance(value, list) else [value]
            if any(str(choice).casefold() not in allowed for choice in values):
                needs_review.append(f"LLM answer outside options: {field.label}")
                continue
        answers.append(ResolvedAnswer(field_id, value))
    for item in raw.get("needs_review", []):
        if isinstance(item, str) and item.strip():
            needs_review.append(item.strip())
    return answers, needs_review

def profile_facts_from_mapping(raw: dict[str, Any]) -> dict[str, str]:
    return {str(key): str(value) for key, value in raw.items() if value is not None and str(value).strip()}
