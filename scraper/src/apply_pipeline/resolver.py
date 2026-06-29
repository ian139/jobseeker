from __future__ import annotations

import re
from typing import Any

from .contracts import ObservedField, PageSnapshot, ResolvedAnswer, ResolverOutput
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
    "country": "country",
    "linkedin": "linkedin",
    "github": "github",
    "portfolio": "portfolio",
    "sponsorship": "sponsorship",
    "hybrid work schedule": "hybrid_schedule",
    "work on this schedule": "hybrid_schedule",
    "years of relevant work experience": "relevant_experience_years",
    "relevant work experience": "relevant_experience_years",
    "affirm": "application_attestation",
    "truthful": "application_attestation",
    "gender": "gender",
    "hispanic/latino": "hispanic_latino",
    "hispanic": "hispanic_latino",
    "latino": "hispanic_latino",
    "race": "race",
    "veteran status": "veteran_status",
    "disability status": "disability_status",
    "website": "portfolio",
}

JOB_SEARCH_FIELD_RE = re.compile(r"\b(search|keyword|search jobs|search by keyword)\b", re.IGNORECASE)


def is_non_application_field(field: ObservedField) -> bool:
    text = f"{field.id} {field.label}".strip()
    return field.kind == "text" and not field.required and bool(JOB_SEARCH_FIELD_RE.search(text))


def effective_application_fields(snapshot: PageSnapshot) -> tuple[ObservedField, ...]:
    return tuple(field for field in snapshot.fields if not is_non_application_field(field))




def normalized(value: str) -> str:
    return " ".join(value.lower().split())


def fact_key_for_label(label: str) -> str | None:
    label_norm = normalized(label)
    for phrase, fact_key in COMMON_FACT_KEYS.items():
        if phrase in label_norm:
            return fact_key
    return None


def fact_for_label(label: str, facts: dict[str, str]) -> str | None:
    fact_key = fact_key_for_label(label)
    if fact_key is not None:
        return facts.get(fact_key)
    return None

def value_matches_field_options(field: ObservedField, value: object) -> bool:
    if not field.options:
        return True
    value_text = str(value).casefold()
    return value_text in {option.casefold() for option in field.options}


def llm_eligible_field_ids(snapshot: PageSnapshot, *, already_answered: set[str]) -> tuple[str, ...]:
    return tuple(
        field.id
        for field in effective_application_fields(snapshot)
        if field.id not in already_answered
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
    llm_enabled: bool = True,
) -> ResolverOutput:
    answers: list[ResolvedAnswer] = []
    needs_review: list[str] = []
    reason_codes: list[str] = []
    deterministic_answer_ids: list[str] = []
    explicit_sensitive_answer_ids: list[str] = []
    filtered_from_llm: list[dict[str, str]] = []
    ignored_non_application_field_ids = [field.id for field in snapshot.fields if is_non_application_field(field)]
    effective_fields = effective_application_fields(snapshot)


    for field in effective_fields:
        if SENSITIVE_FIELD_RE.search(field.label):
            value = facts.get(field.id) or fact_for_label(field.label, facts)
            if value is not None and value_matches_field_options(field, value):
                answers.append(ResolvedAnswer(field.id, value))
                deterministic_answer_ids.append(field.id)
                explicit_sensitive_answer_ids.append(field.id)
            elif value is not None:
                needs_review.append(f"resolver_sensitive_answer_not_in_options: {field.label}")
                reason_codes.append("resolver_sensitive_answer_not_in_options")
            elif field.required:
                needs_review.append(f"resolver_sensitive_field: {field.label}")
                reason_codes.append("resolver_sensitive_field")
            filtered_from_llm.append({"field_id": field.id, "reason": "resolver_sensitive_field"})
            continue
        if field.kind == "file":
            if resume_path and re.search(r"resume|cv", field.label, re.IGNORECASE):
                answers.append(ResolvedAnswer(field.id, resume_path))
                deterministic_answer_ids.append(field.id)
            elif field.required:
                needs_review.append(f"resolver_unknown_required_file: {field.label}")
                reason_codes.append("resolver_unknown_required_file")
                filtered_from_llm.append({"field_id": field.id, "reason": "resolver_unknown_required_file"})
            continue
        if field.kind in {"checkbox", "radio"}:
            value = facts.get(field.id) or fact_for_label(field.label, facts)
            if value is not None and value_matches_field_options(field, value):
                answers.append(ResolvedAnswer(field.id, value))
                deterministic_answer_ids.append(field.id)
            elif field.required:
                needs_review.append(f"resolver_unknown_required_after_llm: {field.label}")
            continue
        value = facts.get(field.id) or fact_for_label(field.label, facts)
        if value is not None and value_matches_field_options(field, value):
            answers.append(ResolvedAnswer(field.id, value))
            deterministic_answer_ids.append(field.id)
        elif field.required:
            needs_review.append(f"resolver_unknown_required_after_llm: {field.label}")

    answered_ids = {answer.field_id for answer in answers}
    unresolved_before_llm = [field.id for field in effective_fields if field.required and field.id not in answered_ids]
    eligible_field_ids = list(llm_eligible_field_ids(snapshot, already_answered=answered_ids))
    for field in snapshot.fields:
        if field.required and field.id not in answered_ids and field.id not in eligible_field_ids and not any(item["field_id"] == field.id for item in filtered_from_llm):
            filtered_from_llm.append({"field_id": field.id, "reason": "llm_ineligible_field"})

    llm_answers: list[ResolvedAnswer] = []
    llm_review: list[str] = []
    llm_called = False
    llm_parse_ok: bool | None = None
    llm_next_button_id: str | None = None
    llm_submit_button_id: str | None = None
    navigation_only_buttons = snapshot.buttons and not effective_fields and any(
        not button.disabled and not button.final_submit_candidate and not is_final_submit_text(button.text) for button in snapshot.buttons
    )
    should_call_llm = bool(eligible_field_ids or navigation_only_buttons)
    if should_call_llm:
        if not llm_enabled:
            reason_codes.append("llm_disabled_by_flag")
        elif llm_client is None:
            reason_codes.append("llm_not_configured")
        else:
            llm_called = True
            llm_answers, llm_review, llm_reason_codes, llm_parse_ok, llm_next_button_id, llm_submit_button_id = resolve_unknowns_with_llm(
                snapshot,
                facts=facts,
                job_description=job_description,
                llm_client=llm_client,
                already_answered=answered_ids,
                eligible_field_ids=set(eligible_field_ids),
                ignored_field_ids=set(ignored_non_application_field_ids),
            )
            reason_codes.extend(llm_reason_codes)
            answers.extend(llm_answers)
            fields_by_id = {field.id: field for field in snapshot.fields}
            if llm_answers:
                answered_llm_ids = {answer.field_id for answer in llm_answers}
                answered_labels = {fields_by_id[field_id].label for field_id in answered_llm_ids if field_id in fields_by_id}
                needs_review = [reason for reason in needs_review if not any(label in reason for label in answered_labels)]
            if llm_review:
                reviewed_llm_ids = {reason.split(":", 1)[0] for reason in llm_review if reason.split(":", 1)[0] in fields_by_id}
                reviewed_labels = {fields_by_id[field_id].label for field_id in reviewed_llm_ids}
                needs_review = [reason for reason in needs_review if not any(label in reason for label in reviewed_labels)]
            needs_review.extend(llm_review)
    else:
        reason_codes.append("llm_no_eligible_fields")

    if any(reason.startswith("resolver_unknown_required_after_llm:") for reason in needs_review):
        reason_codes.append("resolver_unknown_required_after_llm")

    submit_button_id = None
    next_button_id = None
    for button in snapshot.buttons:
        if button.disabled:
            continue
        if button.final_submit_candidate or is_final_submit_text(button.text):
            submit_button_id = button.id
            break
        if next_button_id is None and is_safe_navigation_text(button.text, allow_apply=not effective_fields):
            next_button_id = button.id
    if llm_submit_button_id:
        submit_button_id = llm_submit_button_id
    elif llm_next_button_id and submit_button_id != llm_next_button_id:
        llm_next_button = {button.id: button for button in snapshot.buttons}.get(llm_next_button_id)
        if llm_next_button is not None and is_safe_navigation_text(llm_next_button.text, allow_apply=not effective_fields):
            next_button_id = llm_next_button_id
        else:
            reason_codes.append("llm_unsafe_navigation_button")

    metadata = {
        "observed_field_count": len(snapshot.fields),
        "observed_button_count": len(snapshot.buttons),
        "field_ids": [field.id for field in snapshot.fields],
        "field_kinds": {field.id: field.kind for field in snapshot.fields},
        "field_labels": {field.id: field.label for field in snapshot.fields},
        "field_required": {field.id: field.required for field in snapshot.fields},
        "field_options": {field.id: list(field.options) for field in snapshot.fields},
        "field_visible": {field.id: field.visible for field in snapshot.fields},
        "field_frames": {field.id: field.frame for field in snapshot.fields},
        "deterministic_answer_field_ids": deterministic_answer_ids,
        "explicit_sensitive_answer_field_ids": explicit_sensitive_answer_ids,
        "unresolved_before_llm": unresolved_before_llm,
        "ignored_non_application_field_ids": ignored_non_application_field_ids,
        "effective_application_field_ids": [field.id for field in effective_fields],
        "eligible_for_llm": eligible_field_ids,
        "filtered_from_llm": filtered_from_llm,
        "llm_configured": llm_client is not None,
        "llm_enabled": llm_enabled,
        "llm_called": llm_called,
        "llm_request_field_ids": eligible_field_ids if llm_called else [],
        "llm_parse_ok": llm_parse_ok,
        "llm_navigation_button_id": llm_next_button_id or llm_submit_button_id,
        "merged_answer_field_ids": [answer.field_id for answer in answers],
        "reason_codes": list(dict.fromkeys(reason_codes)),
    }
    return ResolverOutput(
        answers=tuple(answers),
        next_button_id=next_button_id,
        submit_button_id=submit_button_id,
        needs_review=tuple(dict.fromkeys(needs_review)),
        metadata=metadata,
    )


def resolve_unknowns_with_llm(
    snapshot: PageSnapshot,
    *,
    facts: dict[str, str],
    job_description: str | None,
    llm_client: LLMAnswerClient,
    already_answered: set[str],
    eligible_field_ids: set[str] | None = None,
    ignored_field_ids: set[str] | None = None,
) -> tuple[list[ResolvedAnswer], list[str], list[str], bool, str | None, str | None]:
    eligible_field_ids = set(llm_eligible_field_ids(snapshot, already_answered=already_answered)) if eligible_field_ids is None else eligible_field_ids
    ignored_field_ids = set() if ignored_field_ids is None else ignored_field_ids
    fields_by_id = {field.id: field for field in snapshot.fields}
    buttons_by_id = {button.id: button for button in snapshot.buttons}
    try:
        raw = llm_client.resolve_answers(
            llm_payload(snapshot, facts=facts, job_description=job_description, eligible_field_ids=eligible_field_ids)
        )
    except Exception as exc:
        return [], [f"llm_call_failed: {exc}"], ["llm_call_failed"], False, None, None

    reason_codes: list[str] = []
    if not isinstance(raw, dict) or not isinstance(raw.get("answers"), list) or not isinstance(raw.get("needs_review", []), list):
        return [], ["llm_schema_invalid"], ["llm_schema_invalid"], False, None, None

    answers: list[ResolvedAnswer] = []
    needs_review: list[str] = []
    for item in raw["answers"]:
        if not isinstance(item, dict):
            needs_review.append("llm_schema_invalid")
            reason_codes.append("llm_schema_invalid")
            continue
        field_id = str(item.get("field_id") or "")
        field = fields_by_id.get(field_id)
        if field is None:
            reason_codes.append("llm_invalid_field_id")
            continue
        if field_id in ignored_field_ids:
            reason_codes.append("llm_ignored_non_application_field")
            continue
        if field_id in already_answered or field_id not in eligible_field_ids:
            reason_codes.append("llm_invalid_field_id")
            continue
        if SENSITIVE_FIELD_RE.search(field.label):
            needs_review.append(f"resolver_sensitive_field: {field.label}")
            reason_codes.append("resolver_sensitive_field")
            continue
        if field.kind == "file":
            needs_review.append(f"llm_unsupported_field_kind: {field.label}")
            reason_codes.append("llm_unsupported_field_kind")
            continue
        if str(item.get("confidence") or "").lower() not in {"high", "1", "true"}:
            needs_review.append(f"llm_low_confidence: {field.label}")
            reason_codes.append("llm_low_confidence")
            continue
        value = item.get("value")
        if value is None or value == "":
            needs_review.append(f"llm_empty_answer: {field.label}")
            reason_codes.append("llm_empty_answer")
            continue
        if field.options:
            allowed = {option.casefold() for option in field.options}
            values = value if isinstance(value, list) else [value]
            if any(str(choice).casefold() not in allowed for choice in values):
                needs_review.append(f"llm_answer_outside_options: {field.label}")
                reason_codes.append("llm_answer_outside_options")
                continue
        answers.append(ResolvedAnswer(field_id, value))
    for item in raw.get("needs_review", []):
        if isinstance(item, dict):
            field_id = str(item.get("field_id") or "")
            reason = str(item.get("reason") or "").strip()
            if field_id in ignored_field_ids or (field_id and field_id not in eligible_field_ids):
                reason_codes.append("llm_ignored_ineligible_review")
                continue
            if field_id and reason:
                needs_review.append(f"{field_id}: {reason}")
        elif isinstance(item, str) and item.strip():
            needs_review.append(item.strip())
    llm_next_button_id = None
    llm_submit_button_id = None
    for key, target_name in (("next_button_id", "llm_next_button_id"), ("submit_button_id", "llm_submit_button_id")):
        raw_button_id = raw.get(key)
        if raw_button_id is None or raw_button_id == "":
            continue
        button_id = str(raw_button_id)
        if button_id not in buttons_by_id:
            needs_review.append(f"llm_invalid_button_id: {button_id}")
            reason_codes.append("llm_invalid_button_id")
            continue
        if target_name == "llm_next_button_id":
            llm_next_button_id = button_id
        else:
            llm_submit_button_id = button_id
    return answers, needs_review, reason_codes, True, llm_next_button_id, llm_submit_button_id

def profile_facts_from_mapping(raw: dict[str, Any]) -> dict[str, str]:
    return {str(key): str(value) for key, value in raw.items() if value is not None and str(value).strip()}
