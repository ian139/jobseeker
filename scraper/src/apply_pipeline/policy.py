from __future__ import annotations

import re

from .contracts import ExecutorAction, PageSnapshot, ResolverOutput, RunDecision, StepStatus

FINAL_SUBMIT_RE = re.compile(r"\b(submit|send application|finish application|complete application)\b", re.IGNORECASE)
SAFE_NAV_RE = re.compile(r"\b(next|continue|save and continue|review)\b", re.IGNORECASE)
SENSITIVE_FIELD_RE = re.compile(
    r"\b(ssn|social security|date of birth|dob|gender|race|ethnicity|disability|veteran|signature|captcha|attest|attestation|legal|authorization|sponsorship)\b",
    re.IGNORECASE,
)
BLOCKER_RE = re.compile(r"\b(sign in|login|captcha|job no longer|expired|not found|assessment)\b", re.IGNORECASE)


def is_final_submit_text(text: str) -> bool:
    return bool(FINAL_SUBMIT_RE.search(text.strip()))


def is_safe_navigation_text(text: str) -> bool:
    stripped = text.strip()
    return bool(SAFE_NAV_RE.search(stripped)) and not is_final_submit_text(stripped)


def page_blockers(snapshot: PageSnapshot) -> tuple[str, ...]:
    blockers = list(snapshot.blockers)
    blockers.extend(error for error in snapshot.errors if BLOCKER_RE.search(error))
    return tuple(dict.fromkeys(blockers))


def plan_guarded_actions(snapshot: PageSnapshot, resolved: ResolverOutput) -> RunDecision:
    blockers = page_blockers(snapshot)
    if blockers:
        return RunDecision(StepStatus.BLOCKED, blockers[0])
    if resolved.needs_review:
        return RunDecision(StepStatus.NEEDS_REVIEW, resolved.needs_review[0])

    field_ids = {field.id: field for field in snapshot.fields}
    actions: list[ExecutorAction] = []
    for answer in resolved.answers:
        field = field_ids.get(answer.field_id)
        if field is None:
            return RunDecision(StepStatus.NEEDS_REVIEW, f"unknown field: {answer.field_id}")
        if SENSITIVE_FIELD_RE.search(field.label):
            return RunDecision(StepStatus.NEEDS_REVIEW, f"sensitive field: {field.label}")
        if field.kind == "file":
            actions.append(ExecutorAction("upload", field.id, answer.value))
        elif field.kind in {"checkbox", "radio"}:
            actions.append(ExecutorAction("check", field.id, answer.value))
        elif field.kind == "select":
            actions.append(ExecutorAction("select", field.id, answer.value))
        elif field.kind in {"text", "textarea", "typeahead"}:
            actions.append(ExecutorAction("fill", field.id, answer.value))
        else:
            return RunDecision(StepStatus.NEEDS_REVIEW, f"unsupported field kind: {field.kind}")

    buttons = {button.id: button for button in snapshot.buttons}
    if resolved.submit_button_id:
        submit = buttons.get(resolved.submit_button_id)
        if submit is None:
            return RunDecision(StepStatus.NEEDS_REVIEW, f"unknown submit button: {resolved.submit_button_id}")
        return RunDecision(StepStatus.DRY_RUN_READY, f"ready at final submit: {submit.text}", tuple(actions))

    if resolved.next_button_id:
        button = buttons.get(resolved.next_button_id)
        if button is None:
            return RunDecision(StepStatus.NEEDS_REVIEW, f"unknown next button: {resolved.next_button_id}")
        if button.disabled:
            return RunDecision(StepStatus.BLOCKED, f"navigation button disabled: {button.text}")
        if button.final_submit_candidate or is_final_submit_text(button.text):
            return RunDecision(StepStatus.DRY_RUN_READY, f"ready at final submit: {button.text}", tuple(actions))
        if not is_safe_navigation_text(button.text):
            return RunDecision(StepStatus.NEEDS_REVIEW, f"unsafe navigation button: {button.text}")
        actions.append(ExecutorAction("click", button.id))
        return RunDecision(StepStatus.CONTINUE, "safe actions planned", tuple(actions))

    return RunDecision(StepStatus.NEEDS_REVIEW, "resolver did not choose navigation")
