from jobs_assistant.contracts import ButtonSnapshot, FieldKind, FieldSnapshot, PageSnapshot, StepStatus
from jobs_assistant.resolver import resolve_snapshot


def field(id, label, kind=FieldKind.TEXT):
    return FieldSnapshot(id=id, kind=kind, label=label, required=True)


def test_resolver_refuses_sensitive_field():
    snapshot = PageSnapshot(url="x", fields=(field("ssn", "Social Security Number"),))
    decision = resolve_snapshot(snapshot, facts={"social_security_number": "123"})
    assert decision.status == StepStatus.NEEDS_REVIEW
    assert "sensitive_field" in decision.review_reasons[0]


def test_resolver_refuses_unknown_required_field():
    snapshot = PageSnapshot(url="x", fields=(field("github", "GitHub profile"),))
    decision = resolve_snapshot(snapshot, facts={})
    assert decision.status == StepStatus.NEEDS_REVIEW
    assert "unknown_required" in decision.review_reasons[0]


def test_resolver_blocks_page_blockers():
    decision = resolve_snapshot(PageSnapshot(url="x", blockers=("captcha",)), facts={})
    assert decision.status == StepStatus.BLOCKED


def test_resolver_answers_known_fields_and_continues():
    snapshot = PageSnapshot(
        url="x",
        fields=(field("name", "Full name"),),
        buttons=(ButtonSnapshot(id="next", text="Continue", type="button", disabled=False, final_submit_candidate=False),),
    )
    decision = resolve_snapshot(snapshot, facts={"full_name": "Ian"})
    assert decision.status == StepStatus.CONTINUE
    assert decision.answers[0].value == "Ian"
    assert decision.next_button == "next"


def test_resolver_stops_at_final_submit():
    snapshot = PageSnapshot(url="x", buttons=(ButtonSnapshot(id="submit", text="Submit application", type="submit", disabled=False, final_submit_candidate=True),))
    decision = resolve_snapshot(snapshot, facts={})
    assert decision.status == StepStatus.DRY_RUN_READY
    assert decision.submit_button == "submit"
