import json
import sqlite3

import pytest

from apply_pipeline.backlog import job_application_url, next_backlog_jobs
from apply_pipeline.contracts import ExecutorAction, ObservedButton, ObservedField, PageSnapshot, ResolvedAnswer, ResolverOutput, RunDecision, StepStatus
from apply_pipeline.executor import FakeActionTarget, execute_actions
from apply_pipeline.llm import llm_payload, ollama_cloud_client_from_env
from apply_pipeline.observer import observe_html, observe_page
from apply_pipeline.policy import plan_guarded_actions
from apply_pipeline.resolver import resolve_snapshot
from apply_pipeline.runner import ApplicantProfile, default_applicant_profile, load_applicant_profile, resume_facts_from_path, run_application_once, run_backlog_applications
from apply_pipeline.runs import finish_application_run, record_application_page, start_application_run
from sync.jobs import initialize_database, sample_application_failures, upsert_job


def memory_db() -> sqlite3.Connection:
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    initialize_database(connection)
    return connection


def test_guarded_actions_stop_at_final_submit() -> None:
    snapshot = PageSnapshot(
        url="https://example.com/apply",
        fields=(ObservedField("first_name", "text", "First name", required=True),),
        buttons=(ObservedButton("submit", "Submit application", final_submit_candidate=True),),
    )
    resolved = ResolverOutput(
        answers=(ResolvedAnswer("first_name", "Ian"),),
        submit_button_id="submit",
    )
    decision = plan_guarded_actions(snapshot, resolved)
    assert decision.status == StepStatus.DRY_RUN_READY
    assert [action.kind for action in decision.actions] == ["fill"]


def test_guarded_actions_review_sensitive_fields() -> None:
    snapshot = PageSnapshot(
        url="https://example.com/apply",
        fields=(ObservedField("ssn", "text", "Social Security Number", required=True),),
        buttons=(ObservedButton("next", "Continue"),),
    )
    resolved = ResolverOutput(answers=(ResolvedAnswer("ssn", "123"),), next_button_id="next")
    decision = plan_guarded_actions(snapshot, resolved)
    assert decision.status == StepStatus.NEEDS_REVIEW
    assert "Social Security" in decision.reason


def test_guarded_actions_allow_safe_next_navigation() -> None:
    snapshot = PageSnapshot(
        url="https://example.com/apply",
        fields=(ObservedField("email", "text", "Email", required=True),),
        buttons=(ObservedButton("next", "Continue"),),
    )
    resolved = ResolverOutput(answers=(ResolvedAnswer("email", "me@example.com"),), next_button_id="next")
    decision = plan_guarded_actions(snapshot, resolved)
    assert decision.status == StepStatus.CONTINUE
    assert [action.kind for action in decision.actions] == ["fill", "click"]


def test_guarded_actions_do_not_treat_plain_apply_as_safe_navigation() -> None:
    snapshot = PageSnapshot(
        url="https://example.com/apply",
        fields=(),
        buttons=(ObservedButton("apply", "Apply"),),
    )
    resolved = ResolverOutput(next_button_id="apply")
    decision = plan_guarded_actions(snapshot, resolved)
    assert decision.status == StepStatus.NEEDS_REVIEW
    assert decision.reason == "unsafe navigation button: Apply"


def test_guarded_actions_block_login_forms_before_llm_or_fill() -> None:
    snapshot = observe_html(
        """
        <form>
          <label for="email">Email</label><input id="email" required>
          <label for="password">Password</label><input id="password" type="password" required>
          <button id="login">Log in</button>
        </form>
        """,
        url="https://example.com/login",
    )
    decision = plan_guarded_actions(snapshot, ResolverOutput())

    assert decision.status == StepStatus.BLOCKED
    assert "login" in decision.reason.lower() or "sign-in" in decision.reason.lower()


def test_observer_ignores_script_text_for_blockers() -> None:
    snapshot = observe_html(
        """
        <script>{"description":"assessment required in backend JSON"}</script>
        <form>
          <label for="email">Email</label><input id="email" required>
          <button id="next">Continue</button>
        </form>
        """,
        url="https://example.com/apply",
    )

    assert snapshot.blockers == ()
    assert snapshot.errors == ()
    assert snapshot.fields[0].label == "Email"
    assert snapshot.buttons[0].text == "Continue"


def test_backlog_skips_jobs_with_terminal_application_runs() -> None:
    connection = memory_db()
    first = upsert_job(connection, {"id": "ts-1", "job_title": "Software Engineer", "url": "https://example.com/1"})
    second = upsert_job(connection, {"id": "ts-2", "job_title": "Data Engineer", "url": "https://example.com/2"})
    assert first.status == "inserted"
    assert second.status == "inserted"
    connection.execute(
        """
        INSERT INTO application_runs (job_id, status, reason, started_at)
        VALUES ((SELECT id FROM jobs WHERE theirstack_job_id = 'ts-1'), 'dry_run_ready', 'ready', '2026-01-01T00:00:00+00:00')
        """
    )
    rows = next_backlog_jobs(connection, limit=10)
    assert [row["theirstack_job_id"] for row in rows] == ["ts-2"]
    assert job_application_url(rows[0]) == "https://example.com/2"


def test_backlog_retries_failed_application_runs() -> None:
    connection = memory_db()
    upsert_job(connection, {"id": "ts-1", "job_title": "Software Engineer", "url": "https://example.com/1"})
    connection.execute(
        """
        INSERT INTO application_runs (job_id, status, reason, started_at)
        VALUES ((SELECT id FROM jobs WHERE theirstack_job_id = 'ts-1'), 'failed', 'browser missing', '2026-01-01T00:00:00+00:00')
        """
    )
    rows = next_backlog_jobs(connection, limit=10)
    assert [row["theirstack_job_id"] for row in rows] == ["ts-1"]


def test_run_storage_rejects_transient_continue_status_and_records_pages() -> None:
    connection = memory_db()
    upsert_job(connection, {"id": "ts-1", "job_title": "Software Engineer", "url": "https://example.com/1"})
    job_id = connection.execute("SELECT id FROM jobs").fetchone()["id"]
    run_id = start_application_run(connection, job_id=job_id, started_at="2026-01-01T00:00:00+00:00")
    snapshot = PageSnapshot(url="https://example.com/1", fields=(ObservedField("email", "text", "Email"),))
    record_application_page(
        connection,
        run_id=run_id,
        page_index=0,
        url=snapshot.url,
        snapshot=snapshot,
        created_at="2026-01-01T00:00:01+00:00",
    )
    with pytest.raises(ValueError, match="Invalid terminal application status: continue"):
        finish_application_run(
            connection,
            run_id=run_id,
            status=StepStatus.CONTINUE,
            reason="not terminal",
            finished_at="2026-01-01T00:00:02+00:00",
        )
    finish_application_run(
        connection,
        run_id=run_id,
        status=StepStatus.NEEDS_REVIEW,
        reason="manual field",
        finished_at="2026-01-01T00:00:03+00:00",
        final_url="https://example.com/1",
        actions=[],
    )
    row = connection.execute("SELECT status, reason FROM application_runs WHERE id = ?", (run_id,)).fetchone()
    page = connection.execute("SELECT snapshot_json FROM application_pages WHERE run_id = ?", (run_id,)).fetchone()
    assert dict(row) == {"status": "needs_review", "reason": "manual field"}
    assert json.loads(page["snapshot_json"])["url"] == "https://example.com/1"



def test_failure_sampler_includes_reason_group_and_apply_host() -> None:
    connection = memory_db()
    upsert_job(connection, {"id": "ts-1", "job_title": "Software Engineer", "url": "https://boards.example.com/apply"})
    job_id = connection.execute("SELECT id FROM jobs").fetchone()["id"]
    run_id = start_application_run(connection, job_id=job_id, started_at="2026-01-01T00:00:00+00:00")
    finish_application_run(
        connection,
        run_id=run_id,
        status=StepStatus.BLOCKED,
        reason="captcha required",
        finished_at="2026-01-01T00:01:00+00:00",
        final_url="https://boards.example.com/apply/step",
    )

    samples = sample_application_failures(connection, status="blocked", limit=5)

    assert samples[0]["apply_host"] == "boards.example.com"
    assert samples[0]["reason_group"] == "captcha"

def test_observer_normalizes_static_html_fields_buttons_and_errors() -> None:
    snapshot = observe_html(
        """
        <form>
          <label for="email">Email</label><input id="email" required>
          <label for="resume">Resume</label><input id="resume" type="file">
          <select id="state"><option>CA</option><option>NY</option></select>
          <button id="next">Continue</button>
          <button id="submit">Submit application</button>
          <div>Required field missing</div>
        </form>
        """,
        url="https://example.com/apply",
    )
    assert [(field.id, field.kind, field.label, field.required) for field in snapshot.fields][:2] == [
        ("email", "text", "Email", True),
        ("resume", "file", "Resume", False),
    ]
    assert snapshot.fields[2].options == ("CA", "NY")
    assert snapshot.buttons[-1].final_submit_candidate is True
    assert "Required field missing" in snapshot.errors


def test_observer_handles_parent_labels_links_submit_inputs_and_frames() -> None:
    snapshot = observe_html(
        """
        <label>Portfolio <div contenteditable="true" id="portfolio"></div></label>
        <input type="submit" id="submit-button" value="Submit application">
        <a href="/apply/next">Continue</a>
        <a href="/privacy">Privacy policy</a>
        """
    )
    assert [(field.id, field.kind, field.label) for field in snapshot.fields] == [
        ("portfolio", "typeahead", "Portfolio")
    ]
    assert [(button.id, button.text, button.type, button.final_submit_candidate) for button in snapshot.buttons] == [
        ("submit-button", "Submit application", "submit", True),
        ("a_1", "Continue", "link", False),
    ]

    class Frame:
        def content(self) -> str:
            return '<label for="resume">Resume</label><input id="resume" type="file">'

    class FramePage:
        url = "https://example.com/apply"

        def __init__(self) -> None:
            self.frames = [self, Frame()]

        def content(self) -> str:
            return '<label for="name">Full name</label><input id="name">'

    framed = observe_page(FramePage())
    assert [(field.id, field.kind, field.label) for field in framed.fields] == [
        ("name", "text", "Full name"),
        ("resume", "file", "Resume"),
    ]


def test_resolver_answers_known_fields_uploads_resume_and_refuses_unknown() -> None:
    snapshot = PageSnapshot(
        url="https://example.com/apply",
        fields=(
            ObservedField("email", "text", "Email", required=True),
            ObservedField("resume", "file", "Resume", required=True),
            ObservedField("why", "textarea", "Why are you a fit?", required=True),
        ),
        buttons=(ObservedButton("next", "Continue"),),
    )
    resolved = resolve_snapshot(snapshot, facts={"email": "me@example.com"}, resume_path="resume.pdf")
    assert resolved.answers == (ResolvedAnswer("email", "me@example.com"), ResolvedAnswer("resume", "resume.pdf"))
    assert resolved.next_button_id == "next"
    assert resolved.needs_review == ("unknown required field: Why are you a fit?",)


def test_resolver_uses_llm_for_unknown_required_fields_without_sensitive_bypass() -> None:
    class FakeLLMClient:
        def __init__(self) -> None:
            self.payloads: list[dict[str, object]] = []

        def resolve_answers(self, payload: dict[str, object]) -> dict[str, object]:
            self.payloads.append(payload)
            return {
                "answers": [
                    {"field_id": "why", "value": "I have built relevant data systems.", "confidence": "high"},
                    {"field_id": "dob", "value": "2000-01-01", "confidence": "high"},
                ],
                "needs_review": [],
            }

    client = FakeLLMClient()
    snapshot = PageSnapshot(
        url="https://example.com/apply",
        fields=(
            ObservedField("why", "textarea", "Why are you a fit?", required=True),
            ObservedField("dob", "text", "Date of birth", required=True),
        ),
        buttons=(ObservedButton("next", "Continue"),),
    )

    resolved = resolve_snapshot(
        snapshot,
        facts={"email": "me@example.com"},
        job_description="Build data pipelines for co-op teams.",
        llm_client=client,
    )

    assert ResolvedAnswer("why", "I have built relevant data systems.") in resolved.answers
    assert all(answer.field_id != "dob" for answer in resolved.answers)
    assert resolved.needs_review == ("sensitive field: Date of birth",)
    assert client.payloads[0]["job_description"] == "Build data pipelines for co-op teams."
    assert [field["id"] for field in client.payloads[0]["fields"]] == ["why"]


def test_resolver_does_not_call_llm_for_sensitive_only_review() -> None:
    class FailingLLMClient:
        def resolve_answers(self, payload: dict[str, object]) -> dict[str, object]:
            raise AssertionError("LLM should not receive sensitive-only pages")

    snapshot = PageSnapshot(
        url="https://example.com/apply",
        fields=(ObservedField("dob", "text", "Date of birth", required=True),),
        buttons=(ObservedButton("next", "Continue"),),
    )

    resolved = resolve_snapshot(snapshot, facts={}, llm_client=FailingLLMClient())

    assert resolved.needs_review == ("sensitive field: Date of birth",)


def test_ollama_cloud_client_uses_deepseek_v4_pro_env_defaults(monkeypatch) -> None:
    monkeypatch.setenv("OLLAMA_CLOUD_API_KEY", "test-key")
    monkeypatch.delenv("OLLAMA_CLOUD_BASE_URL", raising=False)
    monkeypatch.delenv("OLLAMA_CLOUD_MODEL", raising=False)
    monkeypatch.delenv("DEEPSEEK_MODEL", raising=False)

    client = ollama_cloud_client_from_env()

    assert client is not None
    assert client.config.base_url == "https://ollama.com"
    assert client.config.model == "deepseek-v4-pro"


def test_default_applicant_profile_reads_agents_reference(tmp_path) -> None:
    agents_path = tmp_path / "AGENTS.md"
    agents_path.write_text(
        """
## Applicant reference

- Resume file: `Main_Resume.pdf`
- LinkedIn: `https://www.linkedin.com/in/ianrapko`
- Personal site: `https://immemorized.com`

## Safety policy
"""
    )
    profile = default_applicant_profile(agents_path=agents_path)
    assert profile.facts["linkedin"] == "https://www.linkedin.com/in/ianrapko"
    assert profile.facts["portfolio"] == "https://immemorized.com"
    assert profile.facts["website"] == "https://immemorized.com"
    assert profile.resume_path is not None
    assert profile.resume_path.endswith("Main_Resume.pdf")


def test_profile_json_and_resume_cli_override_defaults(tmp_path) -> None:
    agents_path = tmp_path / "AGENTS.md"
    agents_path.write_text(
        """
## Applicant reference

- Resume file: `Main_Resume.pdf`
- LinkedIn: `https://www.linkedin.com/in/ianrapko`
- Personal site: `https://immemorized.com`

## Safety policy
"""
    )
    profile_json = tmp_path / "profile.json"
    profile_json.write_text('{"linkedin": "https://example.com/override", "resume_path": "profile.pdf"}')

    profile = load_applicant_profile(profile_json, resume_path="cli.pdf", agents_path=agents_path)

    assert profile.facts["linkedin"] == "https://example.com/override"
    assert profile.facts["portfolio"] == "https://immemorized.com"
    assert profile.resume_path == "cli.pdf"
    profile_with_json_resume = load_applicant_profile(profile_json, agents_path=agents_path)
    assert profile_with_json_resume.resume_path is not None
    assert profile_with_json_resume.resume_path.endswith("Main_Resume.pdf")
    assert profile_with_json_resume.facts["resume_path"] == "profile.pdf"

    profile_without_linkedin = load_applicant_profile(
        profile_json,
        agents_path=agents_path,
        exclude_facts=("linkedin",),
    )
    assert "linkedin" not in profile_without_linkedin.facts
    assert profile_without_linkedin.facts["portfolio"] == "https://immemorized.com"

    empty_agents = tmp_path / "EMPTY_AGENTS.md"
    empty_agents.write_text("# Agent Operating Notes\n")
    empty_profile = default_applicant_profile(agents_path=empty_agents)
    assert empty_profile.facts == {}
    assert empty_profile.resume_path is None


def test_resume_text_is_extracted_into_profile_facts_and_llm_payload(tmp_path) -> None:
    resume = tmp_path / "resume.txt"
    resume.write_text(
        """
Ian Rapko
ian@example.com | 416-555-0100
Software engineer building Python and Playwright automation.

Skills:
Python, Playwright, SQLite, React

Experience:
Built job application tooling.
"""
    )
    facts = resume_facts_from_path(resume)
    assert "Ian Rapko" in facts["resume_summary"]
    assert facts["skills"] == "Python, Playwright, SQLite, React"
    assert facts["first_name"] == "Ian"
    assert facts["last_name"] == "Rapko"
    assert facts["email"] == "ian@example.com"
    assert facts["phone"] == "416-555-0100"

    profile = load_applicant_profile(None, resume_path=str(resume), include_defaults=False)
    payload = llm_payload(
        PageSnapshot(url="https://example.com", fields=(ObservedField("first", "text", "First Name *", required=True),), buttons=()),
        facts=profile.facts,
        job_description="Python automation role",
        eligible_field_ids={"first"},
    )

    assert "resume_summary" in payload["applicant_facts"]
    assert payload["applicant_facts"]["skills"] == "Python, Playwright, SQLite, React"
    assert payload["applicant_facts"]["first_name"] == "Ian"


def test_default_resume_reaches_resolver_without_sensitive_inference(tmp_path) -> None:
    agents_path = tmp_path / "AGENTS.md"
    agents_path.write_text(
        """
## Applicant reference

- Resume file: `Main_Resume.pdf`
- LinkedIn: `https://www.linkedin.com/in/ianrapko`
- Personal site: `https://immemorized.com`

## Safety policy
"""
    )
    profile = default_applicant_profile(agents_path=agents_path)
    snapshot = PageSnapshot(
        url="https://example.com/apply",
        fields=(
            ObservedField("linkedin", "text", "LinkedIn", required=True),
            ObservedField("portfolio", "text", "Portfolio", required=True),
            ObservedField("resume", "file", "Resume", required=True),
            ObservedField("dob", "text", "Date of birth", required=True),
        ),
        buttons=(ObservedButton("next", "Continue"),),
    )

    resolved = resolve_snapshot(snapshot, facts=profile.facts, resume_path=profile.resume_path)

    assert ResolvedAnswer("linkedin", "https://www.linkedin.com/in/ianrapko") in resolved.answers
    assert ResolvedAnswer("portfolio", "https://immemorized.com") in resolved.answers
    assert any(answer.field_id == "resume" and str(answer.value).endswith("Main_Resume.pdf") for answer in resolved.answers)
    assert resolved.needs_review == ("sensitive field: Date of birth",)


def test_executor_runs_continue_actions_and_refuses_final_click_or_wrong_upload() -> None:
    target = FakeActionTarget()
    continue_decision = plan_guarded_actions(
        PageSnapshot(
            url="https://example.com/apply",
            fields=(ObservedField("email", "text", "Email"),),
            buttons=(ObservedButton("next", "Continue"),),
        ),
        ResolverOutput(answers=(ResolvedAnswer("email", "me@example.com"),), next_button_id="next"),
    )
    execute_actions(target, continue_decision)
    assert target.calls == [("fill", "email", "me@example.com"), ("click", "next", None)]

    radio_decision = plan_guarded_actions(
        PageSnapshot(
            url="https://example.com/apply",
            fields=(ObservedField("contact", "radio", "Contact Method"),),
            buttons=(ObservedButton("next", "Continue"),),
        ),
        ResolverOutput(answers=(ResolvedAnswer("contact", "Email"),), next_button_id="next"),
    )
    execute_actions(target, radio_decision)
    assert ("check", "contact", "Email") in target.calls

    final_target = FakeActionTarget()
    final_decision = plan_guarded_actions(
        PageSnapshot(
            url="https://example.com/apply",
            fields=(ObservedField("email", "text", "Email"),),
            buttons=(ObservedButton("submit", "Submit application"),),
        ),
        ResolverOutput(answers=(ResolvedAnswer("email", "me@example.com"),), submit_button_id="submit"),
    )
    assert final_decision.status == StepStatus.DRY_RUN_READY
    assert execute_actions(final_target, final_decision) == [ExecutorAction("fill", "email", "me@example.com")]
    assert final_target.calls == [("fill", "email", "me@example.com")]

    review_target = FakeActionTarget()
    review_decision = plan_guarded_actions(
        PageSnapshot(
            url="https://example.com/apply",
            fields=(
                ObservedField("email", "text", "Email", required=True),
                ObservedField("why", "textarea", "Why are you a fit?", required=True),
            ),
            buttons=(ObservedButton("next", "Continue"),),
        ),
        ResolverOutput(
            answers=(ResolvedAnswer("email", "me@example.com"),),
            next_button_id="next",
            needs_review=("unknown required field: Why are you a fit?",),
        ),
    )
    assert review_decision.status == StepStatus.NEEDS_REVIEW
    assert execute_actions(review_target, review_decision) == [ExecutorAction("fill", "email", "me@example.com")]
    assert review_target.calls == [("fill", "email", "me@example.com")]


    malformed_target = FakeActionTarget()
    malformed_final_click = RunDecision(
        StepStatus.DRY_RUN_READY,
        "ready",
        (ExecutorAction("fill", "email", "me@example.com"), ExecutorAction("click", "submit")),
    )
    with pytest.raises(ValueError, match="Refusing to click"):
        execute_actions(malformed_target, malformed_final_click)
    assert malformed_target.calls == []
    upload_decision = ResolverOutput(answers=(ResolvedAnswer("resume", "other.pdf"),), next_button_id="next")
    decision = plan_guarded_actions(
        PageSnapshot(
            url="https://example.com/apply",
            fields=(ObservedField("resume", "file", "Resume"),),
            buttons=(ObservedButton("next", "Continue"),),
        ),
        upload_decision,
    )
    with pytest.raises(ValueError, match="Refusing to upload"):
        execute_actions(target, decision, resume_path="resume.pdf")


class FakePage:
    def __init__(self, pages: list[str]) -> None:
        self.pages = pages
        self.index = 0
        self.url = "about:blank"
        self.visited: list[str] = []

    def goto(self, url: str, *, wait_until: str = "domcontentloaded") -> None:
        self.url = url
        self.visited.append(url)

    def content(self) -> str:
        return self.pages[self.index]

    def wait_for_load_state(self, state: str = "domcontentloaded") -> None:
        if state == "networkidle":
            return
        if self.index + 1 < len(self.pages):
            self.index += 1
            self.url = f"https://example.com/apply/page-{self.index + 1}"


class AdvancingTarget(FakeActionTarget):
    def __init__(self, page: FakePage) -> None:
        super().__init__()
        self.page = page

    def click(self, target_id: str) -> None:
        super().click(target_id)
        self.page.wait_for_load_state()

class FakeBrowser:
    def __init__(self, pages: list[str]) -> None:
        self.pages = pages
        self.closed = False
        self.created_pages = 0

    def new_page(self) -> FakePage:
        self.created_pages += 1
        return FakePage(self.pages)

    def close(self) -> None:
        self.closed = True


def fixed_now() -> str:
    return "2026-01-01T00:00:00+00:00"


def test_runner_loops_until_final_submit_without_clicking_it() -> None:
    connection = memory_db()
    upsert_job(connection, {"id": "ts-1", "job_title": "Software Engineer", "url": "https://example.com/apply"})
    job = connection.execute("SELECT * FROM jobs").fetchone()
    page = FakePage(
        [
            '<label for="email">Email</label><input id="email" required><button id="next">Continue</button>',
            '<label for="full_name">Full name</label><input id="full_name" required><button id="submit">Submit application</button>',
        ]
    )
    target = AdvancingTarget(page)

    run_id, status = run_application_once(
        connection,
        job=job,
        page=page,
        action_target=target,
        profile=ApplicantProfile(facts={"email": "me@example.com", "full_name": "Ada Lovelace"}),
        now=fixed_now,
        max_pages=3,
    )

    assert status == StepStatus.DRY_RUN_READY
    assert target.calls == [("fill", "email", "me@example.com"), ("click", "next", None), ("fill", "full_name", "Ada Lovelace")]
    run = connection.execute("SELECT status, reason FROM application_runs WHERE id = ?", (run_id,)).fetchone()
    assert dict(run) == {"status": "dry_run_ready", "reason": "ready at final submit: Submit application"}
    assert connection.execute("SELECT COUNT(*) AS count FROM application_pages WHERE run_id = ?", (run_id,)).fetchone()["count"] == 2


def test_runner_fills_known_fields_before_needs_review_handoff() -> None:
    connection = memory_db()
    upsert_job(connection, {"id": "ts-1", "job_title": "Software Engineer", "url": "https://example.com/apply"})
    job = connection.execute("SELECT * FROM jobs").fetchone()
    page = FakePage(['<label for="email">Email</label><input id="email" required><label for="why">Why?</label><textarea id="why" required></textarea><button id="next">Continue</button>'])
    target = AdvancingTarget(page)

    run_id, status = run_application_once(
        connection,
        job=job,
        page=page,
        action_target=target,
        profile=ApplicantProfile(facts={"email": "me@example.com"}),
        now=fixed_now,
        max_pages=1,
    )

    assert status == StepStatus.NEEDS_REVIEW
    assert target.calls == [("fill", "email", "me@example.com")]
    run = connection.execute("SELECT actions_json FROM application_runs WHERE id = ?", (run_id,)).fetchone()
    assert json.loads(run["actions_json"]) == [{"kind": "fill", "target_id": "email", "value": "me@example.com"}]


def test_runner_marks_max_pages_as_needs_review_and_closes_browser() -> None:
    connection = memory_db()
    upsert_job(connection, {"id": "ts-1", "job_title": "Software Engineer", "url": "https://example.com/apply"})
    browser = FakeBrowser(['<button id="next">Continue</button>'])

    result = run_backlog_applications(
        connection,
        browser=browser,
        action_target_factory=AdvancingTarget,
        profile=ApplicantProfile(facts={}),
        now=fixed_now,
        limit=1,
        max_pages=1,
    )

    assert result["attempted"] == 1
    assert result["needs_review"] == 1
    assert browser.closed is True



def test_runner_handoff_keeps_browser_open_on_terminal_review_status() -> None:
    connection = memory_db()
    upsert_job(connection, {"id": "ts-1", "job_title": "Software Engineer", "url": "https://example.com/apply"})
    browser = FakeBrowser(["<button id=\"submit\">Submit application</button>"])
    handed_off: list[tuple[int, StepStatus, str]] = []

    result = run_backlog_applications(
        connection,
        browser=browser,
        action_target_factory=AdvancingTarget,
        profile=ApplicantProfile(facts={}),
        now=fixed_now,
        limit=1,
        max_pages=1,
        handoff_callback=lambda run_id, status, page: handed_off.append((run_id, status, page.url)),
        close_browser=False,
    )

    assert result["attempted"] == 1
    assert result["dry_run_ready"] == 1
    assert handed_off == [(result["run_ids"][0], StepStatus.DRY_RUN_READY, "https://example.com/apply")]
    assert browser.closed is False


def test_runner_handoff_includes_failed_status_for_debugging() -> None:
    class FailingTarget(FakeActionTarget):
        def fill(self, target_id: str, value: str) -> None:
            raise RuntimeError("field detached")

    connection = memory_db()
    upsert_job(connection, {"id": "ts-1", "job_title": "Software Engineer", "url": "https://example.com/apply"})
    browser = FakeBrowser(['<label for="email">Email</label><input id="email" required><button id="next">Continue</button>'])
    handed_off: list[StepStatus] = []

    result = run_backlog_applications(
        connection,
        browser=browser,
        action_target_factory=lambda page: FailingTarget(),
        profile=ApplicantProfile(facts={"email": "me@example.com"}),
        now=fixed_now,
        limit=1,
        max_pages=1,
        handoff_callback=lambda run_id, status, page: handed_off.append(status),
        close_browser=False,
    )

    assert result["failed"] == 1
    assert handed_off == [StepStatus.FAILED]
    assert browser.closed is False


def test_runner_blocks_linkedin_jobs_without_opening_browser_or_handoff() -> None:
    connection = memory_db()
    upsert_job(connection, {"id": "ts-1", "job_title": "Software Engineer", "url": "https://www.linkedin.com/jobs/view/123"})
    browser = FakeBrowser(["<button id=\"submit\">Submit application</button>"])
    handed_off: list[StepStatus] = []

    result = run_backlog_applications(
        connection,
        browser=browser,
        action_target_factory=AdvancingTarget,
        profile=ApplicantProfile(facts={}),
        now=fixed_now,
        limit=1,
        max_pages=1,
        handoff_callback=lambda run_id, status, page: handed_off.append(status),
        close_browser=False,
        block_linkedin_jobs=True,
    )

    run = connection.execute("SELECT status, reason, final_url FROM application_runs WHERE id = ?", (result["run_ids"][0],)).fetchone()
    assert result["attempted"] == 0
    assert result["blocked"] == 1
    assert browser.created_pages == 0
    assert handed_off == []
    assert run["status"] == "blocked"
    assert "LinkedIn job URL blocked" in run["reason"]
    assert run["final_url"] == "https://www.linkedin.com/jobs/view/123"


def test_runner_skips_linkedin_jobs_until_non_linkedin_limit() -> None:
    connection = memory_db()
    upsert_job(connection, {"id": "ts-linkedin", "job_title": "LinkedIn Job", "url": "https://www.linkedin.com/jobs/view/123"})
    upsert_job(connection, {"id": "ts-greenhouse", "job_title": "Greenhouse Job", "url": "https://example.com/apply"})
    browser = FakeBrowser(["<button id=\"submit\">Submit application</button>"])

    result = run_backlog_applications(
        connection,
        browser=browser,
        action_target_factory=AdvancingTarget,
        profile=ApplicantProfile(facts={}),
        now=fixed_now,
        limit=1,
        max_pages=1,
        close_browser=False,
        block_linkedin_jobs=True,
    )

    assert result["attempted"] == 1
    assert result["blocked"] == 1
    assert result["dry_run_ready"] == 1
    assert browser.created_pages == 1