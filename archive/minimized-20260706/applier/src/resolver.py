from __future__ import annotations

import re
from dataclasses import asdict
from typing import Any

from .contracts import Answer, ButtonSnapshot, FieldKind, FieldSnapshot, PageSnapshot, ResolverDecision, StepStatus

SENSITIVE_FIELD_RE = re.compile(
    r"\b(ssn|social security|date of birth|dob|gender|race|ethnicity|veteran|disability|sponsor|visa|work authorization|salary|criminal|felony|passport|driver.?s license|government id|legal)\b",
    re.I,
)
MANUAL_FIELD_RE = re.compile(r"\b(signature|captcha|assessment|test|portfolio upload|cover letter file|video|recording|manual)\b", re.I)
NEXT_RE = re.compile(r"\b(next|continue|start|apply|save and continue)\b", re.I)


def _normalize_label(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", value.lower()).strip("_")


def fact_key_for_label(label: str) -> str:
    return _normalize_label(label)


def _fact_for(field: FieldSnapshot, facts: dict[str, Any]) -> Any | None:
    keys = [field.id, field.label, fact_key_for_label(field.label)]
    normalized = {_normalize_label(str(key)): value for key, value in facts.items()}
    for key in keys:
        if key in facts:
            return facts[key]
        norm = _normalize_label(str(key))
        if norm in normalized:
            return normalized[norm]
    return None


def _safe_next_button(buttons: tuple[ButtonSnapshot, ...]) -> ButtonSnapshot | None:
    for button in buttons:
        if button.disabled or not button.visible or button.final_submit_candidate:
            continue
        if NEXT_RE.search(button.text):
            return button
    return None


def _submit_button(buttons: tuple[ButtonSnapshot, ...]) -> ButtonSnapshot | None:
    for button in buttons:
        if not button.disabled and button.visible and button.final_submit_candidate:
            return button
    return None


def resolve_snapshot(snapshot: PageSnapshot, *, facts: dict[str, Any], resume_path: str | None = None) -> ResolverDecision:
    if snapshot.blockers:
        return ResolverDecision(StepStatus.BLOCKED, review_reasons=tuple(f"blocker:{item}" for item in snapshot.blockers), metadata={"errors": list(snapshot.errors)})

    answers: list[Answer] = []
    review_reasons: list[str] = []
    for field in snapshot.fields:
        if not field.visible or not field.required:
            continue
        label = field.label or field.id
        if SENSITIVE_FIELD_RE.search(label):
            review_reasons.append(f"sensitive_field:{field.id}:{label}")
            continue
        if MANUAL_FIELD_RE.search(label):
            review_reasons.append(f"manual_field:{field.id}:{label}")
            continue
        if field.kind == FieldKind.FILE:
            if resume_path:
                answers.append(Answer(field.id, resume_path))
            else:
                review_reasons.append(f"missing_resume:{field.id}:{label}")
            continue
        value = _fact_for(field, facts)
        if value is None or value == "":
            review_reasons.append(f"unknown_required:{field.id}:{label}")
            continue
        if field.kind == FieldKind.SELECT and field.options:
            option_map = {option.lower(): option for option in field.options}
            selected = option_map.get(str(value).lower())
            if selected is None:
                review_reasons.append(f"unsupported_option:{field.id}:{label}")
                continue
            value = selected
        answers.append(Answer(field.id, bool(value) if field.kind in {FieldKind.CHECKBOX, FieldKind.RADIO} else str(value)))

    submit = _submit_button(snapshot.buttons)
    next_button = _safe_next_button(snapshot.buttons)
    if review_reasons:
        return ResolverDecision(StepStatus.NEEDS_REVIEW, answers=tuple(answers), next_button=next_button.id if next_button else None, submit_button=submit.id if submit else None, review_reasons=tuple(review_reasons))
    if submit:
        return ResolverDecision(StepStatus.DRY_RUN_READY, answers=tuple(answers), submit_button=submit.id, review_reasons=("final_submit_boundary",))
    if next_button:
        return ResolverDecision(StepStatus.CONTINUE, answers=tuple(answers), next_button=next_button.id)
    if snapshot.fields:
        return ResolverDecision(StepStatus.NEEDS_REVIEW, answers=tuple(answers), review_reasons=("no_safe_navigation",))
    return ResolverDecision(StepStatus.BLOCKED, review_reasons=("no_form",))


def decision_to_json(decision: ResolverDecision) -> dict[str, Any]:
    data = asdict(decision)
    data["status"] = decision.status.value
    return data
