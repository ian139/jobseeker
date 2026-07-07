from __future__ import annotations

from dataclasses import asdict
from pathlib import Path
from typing import Any, Iterable

from .contracts import ActionAttempt, ExecutorAction, FieldKind, ResolverDecision, RunDecision, RunStatus, StepStatus
from .db import encode_json, finish_application_run, record_application_page, start_application_run
from .executor import ActionTarget, FakeActionTarget, execute_actions_with_records
from .observer import observe_static_html
from .resolver import decision_to_json, resolve_snapshot


def actions_from_decision(decision: ResolverDecision, fields_by_id: dict[str, FieldKind]) -> tuple[ExecutorAction, ...]:
    actions: list[ExecutorAction] = []
    for answer in decision.answers:
        kind = fields_by_id.get(answer.field_id)
        if kind == FieldKind.SELECT:
            actions.append(ExecutorAction("select", answer.field_id, answer.value))
        elif kind in {FieldKind.CHECKBOX, FieldKind.RADIO}:
            actions.append(ExecutorAction("check", answer.field_id, answer.value))
        elif kind == FieldKind.FILE:
            actions.append(ExecutorAction("upload", answer.field_id, answer.value))
        else:
            actions.append(ExecutorAction("fill", answer.field_id, answer.value))
    if decision.status == StepStatus.CONTINUE and decision.next_button:
        actions.append(ExecutorAction("click", decision.next_button))
    return tuple(actions)


def _terminal_reason(decision: ResolverDecision) -> str:
    if decision.review_reasons:
        return ";".join(decision.review_reasons)
    return decision.status.value


def run_static_dry_run(
    conn: Any,
    *,
    job_id: int,
    html_pages: Iterable[str],
    start_url: str,
    facts: dict[str, Any],
    resume_path: str | Path,
    target: ActionTarget | None = None,
    max_pages: int = 5,
) -> tuple[int, RunStatus, list[ActionAttempt]]:
    run_id = start_application_run(conn, job_id)
    all_actions: list[ActionAttempt] = []
    target = target or FakeActionTarget()
    pages = list(html_pages)
    if not pages:
        finish_application_run(conn, run_id, status=RunStatus.FAILED, reason="no_pages", final_url=start_url, actions=[])
        return run_id, RunStatus.FAILED, []
    final_url = start_url
    for index, html in enumerate(pages[:max_pages]):
        snapshot = observe_static_html(html, url=final_url)
        decision = resolve_snapshot(snapshot, facts=facts, resume_path=str(resume_path))
        record_application_page(conn, run_id, index, url=snapshot.url, snapshot_json=encode_json(asdict(snapshot)), resolver_json=encode_json(decision_to_json(decision)))
        fields_by_id = {field.id: field.kind for field in snapshot.fields}
        run_decision = RunDecision(decision.status, _terminal_reason(decision), actions_from_decision(decision, fields_by_id))
        _, records = execute_actions_with_records(target, run_decision, resume_path=resume_path)
        all_actions.extend(records)
        failures = [record for record in records if not record.success]
        if failures:
            finish_application_run(conn, run_id, status=RunStatus.FAILED, reason=failures[0].message, final_url=snapshot.url, actions=all_actions)
            return run_id, RunStatus.FAILED, all_actions
        terminal = decision.status.terminal()
        if terminal is not None:
            finish_application_run(conn, run_id, status=terminal, reason=_terminal_reason(decision), final_url=snapshot.url, actions=all_actions)
            return run_id, terminal, all_actions
    finish_application_run(conn, run_id, status=RunStatus.FAILED, reason="max_pages_exceeded", final_url=final_url, actions=all_actions)
    return run_id, RunStatus.FAILED, all_actions
