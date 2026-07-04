from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from .contracts import ActionAttempt, ExecutorAction, RunDecision, StepStatus


class ActionTarget(Protocol):
    def fill(self, target_id: str, value: str) -> None: ...
    def select(self, target_id: str, value: str | list[str]) -> None: ...
    def check(self, target_id: str, value: bool | str) -> None: ...
    def upload(self, target_id: str, path: str) -> None: ...
    def click(self, target_id: str) -> None: ...


@dataclass
class FakeActionTarget:
    calls: list[tuple[str, str, object | None]] = field(default_factory=list)

    def fill(self, target_id: str, value: str) -> None:
        self.calls.append(("fill", target_id, value))

    def select(self, target_id: str, value: str | list[str]) -> None:
        self.calls.append(("select", target_id, value))

    def check(self, target_id: str, value: bool | str) -> None:
        self.calls.append(("check", target_id, value))

    def upload(self, target_id: str, path: str) -> None:
        self.calls.append(("upload", target_id, path))

    def click(self, target_id: str) -> None:
        self.calls.append(("click", target_id, None))


def check_value(value: object) -> bool | str:
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"1", "true", "yes", "on", "checked"}:
            return True
        if normalized in {"0", "false", "no", "off", "unchecked"}:
            return False
        return value
    return bool(value)


def execute_action(target: ActionTarget, action: ExecutorAction, *, resume_path: str | Path | None = None) -> None:
    if action.kind == "fill":
        target.fill(action.target_id, str(action.value or ""))
        return
    if action.kind == "select":
        if not isinstance(action.value, (str, list)):
            raise ValueError(f"select action requires a string or list value: {action.target_id}")
        target.select(action.target_id, action.value)
        return
    if action.kind == "check":
        target.check(action.target_id, check_value(action.value))
        return
    if action.kind == "upload":
        if not resume_path:
            raise ValueError("resume_path is required for upload actions")
        requested = Path(str(action.value or resume_path)).expanduser().resolve()
        configured = Path(resume_path).expanduser().resolve()
        if requested != configured:
            raise ValueError("Refusing to upload a file other than the configured resume")
        target.upload(action.target_id, str(configured))
        return
    if action.kind == "click":
        target.click(action.target_id)
        return
    raise ValueError(f"Unsupported executor action: {action.kind}")


CLICK_ALLOWED_STATUSES = frozenset({StepStatus.CONTINUE})


def execute_actions_with_records(
    target: ActionTarget,
    decision: RunDecision,
    *,
    resume_path: str | Path | None = None,
) -> tuple[list[ExecutorAction], list[ActionAttempt]]:
    if decision.status in {StepStatus.BLOCKED, StepStatus.FAILED}:
        return [], []

    executed: list[ExecutorAction] = []
    records: list[ActionAttempt] = []
    for action in decision.actions:
        if action.kind == "click" and decision.status not in CLICK_ALLOWED_STATUSES:
            records.append(ActionAttempt(action.kind, action.target_id, action.value, False, "executor_final_submit_refused"))
            break
        try:
            execute_action(target, action, resume_path=resume_path)
        except Exception as exc:
            records.append(ActionAttempt(action.kind, action.target_id, action.value, False, str(exc)))
            break
        records.append(ActionAttempt(action.kind, action.target_id, action.value, True, "succeeded"))
        executed.append(action)
    return executed, records


def execute_actions(target: ActionTarget, decision: RunDecision, *, resume_path: str | Path | None = None) -> list[ExecutorAction]:
    executed, records = execute_actions_with_records(target, decision, resume_path=resume_path)
    failures = [record for record in records if not record.success]
    if failures:
        raise ValueError(failures[0].message or "executor_action_failed")
    return executed
