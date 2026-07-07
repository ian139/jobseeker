from pathlib import Path

import pytest

from jobs_assistant.contracts import ExecutorAction, RunDecision, StepStatus
from jobs_assistant.executor import FakeActionTarget, check_value, execute_actions, execute_actions_with_records


def test_check_value_normalizes_common_checkbox_values():
    assert check_value("yes") is True
    assert check_value("no") is False
    assert check_value("maybe") == "maybe"


def test_executor_refuses_click_on_final_status():
    target = FakeActionTarget()
    decision = RunDecision(StepStatus.DRY_RUN_READY, "final", (ExecutorAction("click", "submit"),))
    executed, records = execute_actions_with_records(target, decision)
    assert executed == []
    assert target.calls == []
    assert records[-1].success is False
    assert records[-1].message == "executor_final_submit_refused"


def test_executor_allows_non_final_continue_click():
    target = FakeActionTarget()
    decision = RunDecision(StepStatus.CONTINUE, "next", (ExecutorAction("click", "next"),))
    execute_actions(target, decision)
    assert target.calls == [("click", "next", None)]


def test_executor_uploads_only_configured_resume(tmp_path: Path):
    resume = tmp_path / "resume.pdf"
    resume.write_text("pdf")
    other = tmp_path / "other.pdf"
    other.write_text("pdf")
    target = FakeActionTarget()
    with pytest.raises(ValueError):
        execute_actions(target, RunDecision(StepStatus.CONTINUE, "upload", (ExecutorAction("upload", "resume", str(other)),)), resume_path=resume)
    execute_actions(target, RunDecision(StepStatus.CONTINUE, "upload", (ExecutorAction("upload", "resume", str(resume)),)), resume_path=resume)
    assert target.calls == [("upload", "resume", str(resume.resolve()))]
