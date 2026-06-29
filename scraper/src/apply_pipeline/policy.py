from __future__ import annotations

import re

from .contracts import ExecutorAction, PageSnapshot, ResolverOutput, RunDecision, StepStatus

FINAL_SUBMIT_RE = re.compile(r"\b(submit|send application|finish application|complete application)\b", re.IGNORECASE)
SAFE_NAV_RE = re.compile(r"\b(next|continue|save and continue|review)\b", re.IGNORECASE)
APPLY_NAV_RE = re.compile(r"\b(apply|apply now|apply here|start application|begin application)\b", re.IGNORECASE)
SENSITIVE_FIELD_RE = re.compile(
    r"\b(ssn|social security|date of birth|dob|gender|race|ethnicity|hispanic|latino|disability|veteran|signature|captcha|attest|attestation|affirm|truthful|falsification|misrepresentation|omission|legal|authorization|sponsorship)\b",
    re.IGNORECASE,
)
BLOCKER_RE = re.compile(r"\b(sign in|sign-in|log in|login|captcha|job no longer|expired|not\s*found|notfound|payment|assessment|identity verification|identity|email verification|verify email)\b", re.IGNORECASE)
LOGIN_FIELD_RE = re.compile(r"\b(password|one-time code|verification code|2fa|mfa)\b", re.IGNORECASE)
LOGIN_BUTTON_RE = re.compile(r"\b(sign in|sign-in|log in|login)\b", re.IGNORECASE)


def is_final_submit_text(text: str) -> bool:
    return bool(FINAL_SUBMIT_RE.search(text.strip()))


def is_apply_navigation_text(text: str) -> bool:
    stripped = text.strip()
    return bool(APPLY_NAV_RE.search(stripped)) and not is_final_submit_text(stripped)


def is_safe_navigation_text(text: str, *, allow_apply: bool = False) -> bool:
    stripped = text.strip()
    safe_nav = bool(SAFE_NAV_RE.search(stripped))
    safe_apply = allow_apply and is_apply_navigation_text(stripped)
    return (safe_nav or safe_apply) and not is_final_submit_text(stripped)


def page_blockers(snapshot: PageSnapshot) -> tuple[str, ...]:
    blockers = list(snapshot.blockers)
    blockers.extend(error for error in snapshot.errors if BLOCKER_RE.search(error))
    blockers.extend(f"login/sign-in field present: {field.label}" for field in snapshot.fields if LOGIN_FIELD_RE.search(field.label))
    blockers.extend(f"login/sign-in action present: {button.text}" for button in snapshot.buttons if LOGIN_BUTTON_RE.search(button.text))
    if BLOCKER_RE.search(snapshot.url):
        blockers.append(f"page blocker URL: {snapshot.url}")
    return tuple(dict.fromkeys(blockers))


def blocker_reason(blocker: str) -> str:
    lowered = blocker.lower()
    if re.search(r"\b(log\s*in|login|sign[-\s]?in)\b", lowered):
        return "blocked_sign_in"
    if "captcha" in lowered:
        return "blocked_captcha"
    if "notfound" in lowered or "not found" in lowered or "job no longer" in lowered or "expired" in lowered:
        return "blocked_job_gone"
    if "payment" in lowered:
        return "blocked_payment"
    if "assessment" in lowered:
        return "blocked_assessment"
    if "identity" in lowered:
        return "blocked_identity_verification"
    if "email verification" in lowered or "verify email" in lowered:
        return "blocked_email_verification"
    return blocker



def plan_guarded_actions(snapshot: PageSnapshot, resolved: ResolverOutput) -> RunDecision:
    blockers = page_blockers(snapshot)
    if blockers:
        return RunDecision(StepStatus.BLOCKED, blocker_reason(blockers[0]))

    field_ids = {field.id: field for field in snapshot.fields}
    resolved_metadata = getattr(resolved, "metadata", {})
    explicit_sensitive_answer_ids = set(resolved_metadata.get("explicit_sensitive_answer_field_ids", ())) if isinstance(resolved_metadata, dict) else set()
    ignored_non_application_field_ids = set(resolved_metadata.get("ignored_non_application_field_ids", ())) if isinstance(resolved_metadata, dict) else set()
    effective_field_ids = set(field_ids) - ignored_non_application_field_ids
    actions: list[ExecutorAction] = []
    for answer in resolved.answers:
        field = field_ids.get(answer.field_id)
        if field is None:
            return RunDecision(StepStatus.NEEDS_REVIEW, "executor_unknown_field_id")
        if SENSITIVE_FIELD_RE.search(field.label) and field.id not in explicit_sensitive_answer_ids:
            return RunDecision(StepStatus.NEEDS_REVIEW, "resolver_sensitive_field")
        if field.kind == "file":
            actions.append(ExecutorAction("upload", field.id, answer.value))
        elif field.kind in {"checkbox", "radio"}:
            actions.append(ExecutorAction("check", field.id, answer.value))
        elif field.kind == "select":
            actions.append(ExecutorAction("select", field.id, answer.value))
        elif field.kind in {"text", "textarea", "typeahead"}:
            actions.append(ExecutorAction("fill", field.id, answer.value))
        else:
            return RunDecision(StepStatus.NEEDS_REVIEW, "executor_unsupported_field_kind")

    if resolved.needs_review:
        return RunDecision(StepStatus.NEEDS_REVIEW, resolved.needs_review[0], tuple(actions))

    buttons = {button.id: button for button in snapshot.buttons}
    if resolved.submit_button_id:
        submit = buttons.get(resolved.submit_button_id)
        if submit is None:
            return RunDecision(StepStatus.NEEDS_REVIEW, "llm_invalid_button_id")
        return RunDecision(StepStatus.DRY_RUN_READY, "dry_run_final_submit_boundary", tuple(actions))

    if resolved.next_button_id:
        button = buttons.get(resolved.next_button_id)
        if button is None:
            return RunDecision(StepStatus.NEEDS_REVIEW, "llm_invalid_button_id")
        if button.disabled:
            return RunDecision(StepStatus.BLOCKED, "executor_disabled_navigation")
        if button.final_submit_candidate or is_final_submit_text(button.text):
            return RunDecision(StepStatus.DRY_RUN_READY, "dry_run_final_submit_boundary", tuple(actions))
        allow_apply = not effective_field_ids
        if not is_safe_navigation_text(button.text, allow_apply=allow_apply):
            return RunDecision(StepStatus.NEEDS_REVIEW, "executor_unsafe_navigation")
        actions.append(ExecutorAction("click", button.id))
        return RunDecision(StepStatus.CONTINUE, "safe actions planned", tuple(actions))

    return RunDecision(StepStatus.NEEDS_REVIEW, "resolver_no_navigation")
