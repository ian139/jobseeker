from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from .contracts import ExecutorAction, ExecutorActionRecord, RunDecision, StepStatus


class ActionTarget(Protocol):
    def fill(self, target_id: str, value: str) -> None: ...
    def select(self, target_id: str, value: str | list[str]) -> None: ...
    def check(self, target_id: str, value: bool | str) -> None: ...
    def upload(self, target_id: str, path: str) -> None: ...
    def click(self, target_id: str) -> None: ...


@dataclass
class FakeActionTarget:
    calls: list[tuple[str, str, object]] = field(default_factory=list)

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


def _execute_action(
    target: ActionTarget,
    action: ExecutorAction,
    *,
    resume_path: str | None = None,
) -> None:
    if action.kind == "fill":
        target.fill(action.target_id, str(action.value or ""))
    elif action.kind == "select":
        if not isinstance(action.value, (str, list)):
            raise ValueError(f"select action requires a string or list value: {action.target_id}")
        target.select(action.target_id, action.value)
    elif action.kind == "check":
        target.check(action.target_id, check_value(action.value))
    elif action.kind == "upload":
        if not resume_path:
            raise ValueError("resume_path is required for upload actions")
        requested = str(action.value or resume_path)
        if Path(requested).expanduser() != Path(resume_path).expanduser():
            raise ValueError("Refusing to upload a file other than the configured resume")
        target.upload(action.target_id, resume_path)
    elif action.kind == "click":
        target.click(action.target_id)
    else:
        raise ValueError(f"Unsupported executor action: {action.kind}")


def execute_actions_with_records(
    target: ActionTarget,
    decision: RunDecision,
    *,
    resume_path: str | None = None,
) -> tuple[list[ExecutorAction], list[ExecutorActionRecord]]:
    if decision.status not in {StepStatus.CONTINUE, StepStatus.DRY_RUN_READY, StepStatus.NEEDS_REVIEW}:
        return [], []
    executed: list[ExecutorAction] = []
    records: list[ExecutorActionRecord] = []
    for action in decision.actions:
        records.append(ExecutorActionRecord(action, "attempted"))
        if decision.status != StepStatus.CONTINUE and action.kind == "click":
            records.append(ExecutorActionRecord(action, "failed", "executor_final_submit_refused"))
            break
        try:
            _execute_action(target, action, resume_path=resume_path)
        except Exception as exc:
            records.append(ExecutorActionRecord(action, "failed", str(exc)))
            break
        records.append(ExecutorActionRecord(action, "succeeded"))
        executed.append(action)
    return executed, records

def execute_actions(
    target: ActionTarget,
    decision: RunDecision,
    *,
    resume_path: str | None = None,
) -> list[ExecutorAction]:
    if decision.status not in {StepStatus.CONTINUE, StepStatus.DRY_RUN_READY, StepStatus.NEEDS_REVIEW}:
        return []
    if decision.status != StepStatus.CONTINUE and any(action.kind == "click" for action in decision.actions):
        raise ValueError("Refusing to click on a terminal application decision")
    executed, records = execute_actions_with_records(target, decision, resume_path=resume_path)
    failures = [record for record in records if record.status == "failed"]
    if failures:
        raise ValueError(failures[0].reason or "executor_action_failed")
    return executed
