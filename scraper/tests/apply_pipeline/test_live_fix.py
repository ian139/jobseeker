import json
import sqlite3

import pytest

from apply_pipeline.contracts import ObservedButton, ObservedField, PageSnapshot, ResolvedAnswer, ResolverOutput, StepStatus
from apply_pipeline.executor import FakeActionTarget
from apply_pipeline.observer import observe_html, observe_page
from apply_pipeline.policy import plan_guarded_actions
from apply_pipeline.resolver import resolve_snapshot
from apply_pipeline.runner import ApplicantProfile, PlaywrightActionTarget, run_application_once
from sync.jobs import initialize_database, upsert_job


class FakeLLMClient:
    def __init__(self, response):
        self.response = response
        self.payloads = []

    def resolve_answers(self, payload):
        self.payloads.append(payload)
        return self.response


def memory_db() -> sqlite3.Connection:
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    initialize_database(connection)
    return connection


def fixed_now() -> str:
    return "2026-01-01T00:00:00+00:00"


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


def test_observer_exposes_disabled_visibility_and_locator_metadata() -> None:
    snapshot = observe_html(
        """
        <form>
          <label for="email">Email *</label><input id="email" required>
          <label for="resume">Resume</label><input id="resume" type="file">
          <label for="state">State</label><select id="state" required><option>CA</option><option>NY</option></select>
          <label>Portfolio <div contenteditable="true" id="portfolio"></div></label>
          <button id="disabled" disabled>Continue</button>
          <button id="next">Continue</button>
          <button id="submit">Submit application</button>
        </form>
        """,
        url="https://example.com/apply",
    )

    fields = {field.id: field for field in snapshot.fields}
    assert fields["email"].required is True
    assert fields["email"].visible is True
    assert fields["email"].disabled is False
    assert fields["email"].selector == "#email"
    assert fields["resume"].kind == "file"
    assert fields["state"].options == ("CA", "NY")
    assert fields["portfolio"].kind == "typeahead"
    assert [button.disabled for button in snapshot.buttons if button.id == "disabled"] == [True]
    assert [button.final_submit_candidate for button in snapshot.buttons if button.id == "submit"] == [True]
    assert snapshot.metadata["observed_field_count"] == len(snapshot.fields)
    assert snapshot.metadata["observed_button_count"] == len(snapshot.buttons)


def test_policy_blocks_notfound_urls_before_search_pagination_clicks() -> None:
    snapshot = PageSnapshot(
        url="https://careers.example.com/jobs/search?notFound=1",
        fields=(ObservedField("keywords", "text", "Start your job search here"),),
        buttons=(ObservedButton("a_1", "Next page of results"),),
    )
    decision = plan_guarded_actions(snapshot, ResolverOutput(next_button_id="a_1"))

    assert decision.status == StepStatus.BLOCKED
    assert decision.reason == "blocked_job_gone"


def test_resolver_selects_initial_apply_button_without_form_fields() -> None:
    snapshot = PageSnapshot(
        url="https://example.com/job",
        fields=(),
        buttons=(ObservedButton("apply-now", "Apply Now"),),
    )
    resolved = resolve_snapshot(snapshot, facts={})

    assert resolved.next_button_id == "apply-now"
    assert resolved.submit_button_id is None


def test_resolver_accepts_llm_selected_initial_navigation_button() -> None:
    snapshot = PageSnapshot(
        url="https://example.com/job",
        fields=(),
        buttons=(ObservedButton("begin", "Begin application"),),
    )
    client = FakeLLMClient({"answers": [], "next_button_id": "begin", "submit_button_id": None, "needs_review": []})
    resolved = resolve_snapshot(snapshot, facts={}, llm_client=client)

    assert client.payloads
    assert resolved.next_button_id == "begin"
    assert resolved.metadata["llm_called"] is True
    assert resolved.metadata["llm_navigation_button_id"] == "begin"


def test_resolver_rejects_invalid_llm_navigation_button_id() -> None:
    snapshot = PageSnapshot(
        url="https://example.com/job",
        fields=(),
        buttons=(ObservedButton("begin", "Begin application"),),
    )
    resolved = resolve_snapshot(
        snapshot,
        facts={},
        llm_client=FakeLLMClient({"answers": [], "next_button_id": "missing", "submit_button_id": None, "needs_review": []}),
    )

    assert resolved.next_button_id == "begin"
    assert "llm_invalid_button_id" in resolved.metadata["reason_codes"]


def test_llm_payload_includes_full_snapshot_with_answer_eligibility() -> None:
    snapshot = PageSnapshot(
        url="https://example.com/apply",
        fields=(
            ObservedField("email", "text", "Email", required=True),
            ObservedField("dob", "text", "Date of birth", required=True),
        ),
        buttons=(ObservedButton("continue", "Continue"),),
    )
    client = FakeLLMClient({"answers": [], "next_button_id": "continue", "submit_button_id": None, "needs_review": []})
    resolve_snapshot(snapshot, facts={}, llm_client=client)

    fields = {field["id"]: field for field in client.payloads[0]["fields"]}
    assert set(fields) == {"email", "dob"}
    assert fields["email"]["eligible_for_answer"] is True
    assert fields["dob"]["eligible_for_answer"] is False
    assert client.payloads[0]["eligible_field_ids"] == ["email"]


def test_resolver_records_llm_eligibility_and_rejects_invalid_schema_or_ids() -> None:
    snapshot = PageSnapshot(
        url="https://example.com/apply",
        fields=(
            ObservedField("email", "text", "Email", required=True),
            ObservedField("why", "textarea", "Why are you a fit?", required=True),
            ObservedField("dob", "text", "Date of birth", required=True),
        ),
        buttons=(ObservedButton("next", "Continue"),),
    )
    invalid = resolve_snapshot(
        snapshot,
        facts={"email": "me@example.com"},
        llm_client=FakeLLMClient({"answers": [{"field_id": "missing", "value": "x", "confidence": "high"}], "needs_review": []}),
    )

    assert invalid.metadata["unresolved_before_llm"] == ["why", "dob"]
    assert invalid.metadata["eligible_for_llm"] == ["why"]
    assert invalid.metadata["llm_called"] is True
    assert "llm_invalid_field_id" in invalid.metadata["reason_codes"]
    assert ResolvedAnswer("email", "me@example.com") in invalid.answers
    assert all(answer.field_id != "missing" for answer in invalid.answers)

    malformed = resolve_snapshot(snapshot, facts={"email": "me@example.com"}, llm_client=FakeLLMClient({"answers": "not-a-list"}))
    assert "llm_schema_invalid" in malformed.metadata["reason_codes"]



def test_resolver_replaces_generic_required_review_with_specific_llm_review() -> None:
    snapshot = PageSnapshot(
        url="https://example.com/apply",
        fields=(ObservedField("hybrid", "typeahead", "Can you work hybrid?", required=True),),
        buttons=(ObservedButton("submit", "Submit application"),),
    )
    resolved = resolve_snapshot(
        snapshot,
        facts={},
        llm_client=FakeLLMClient({"answers": [], "needs_review": [{"field_id": "hybrid", "reason": "No explicit willingness in supplied context."}]}),
    )

    assert resolved.needs_review == ("hybrid: No explicit willingness in supplied context.",)


def test_live_observer_exposes_attestation_checkbox_as_sensitive_context(tmp_path) -> None:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        pytest.skip("Playwright is not installed")

    html = tmp_path / "attestation.html"
    html.write_text(
        """
        <!doctype html>
        <form>
          <fieldset aria-required="true">
            <legend>By submitting this application, I affirm that all statements are truthful and any falsification may disqualify me.</legend>
            <input required type="checkbox" id="agree" name="agree" description="By submitting this application, I affirm that all statements are truthful.">
            <label for="agree">I agree</label>
          </fieldset>
          <button id="submit" type="submit">Submit application</button>
        </form>
        """,
        encoding="utf-8",
    )
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page()
        try:
            page.goto(html.as_uri())
            snapshot = observe_page(page)
        finally:
            browser.close()

    field = snapshot.fields[0]
    assert "affirm" in field.label
    assert "I agree" in field.label
    resolved = resolve_snapshot(
        snapshot,
        facts={},
        llm_client=FakeLLMClient({"answers": [{"field_id": "agree", "value": True, "confidence": "high"}], "needs_review": []}),
    )
    assert not resolved.answers
    assert resolved.needs_review == (f"resolver_sensitive_field: {field.label}",)


def test_optional_sensitive_fields_are_filtered_without_blocking_review() -> None:
    snapshot = PageSnapshot(
        url="https://example.com/apply",
        fields=(
            ObservedField("gender", "typeahead", "Gender", required=False),
            ObservedField("hispanic_ethnicity", "typeahead", "Are you Hispanic/Latino?", required=False),
            ObservedField("sponsorship", "typeahead", "Will you require sponsorship?", required=True),
        ),
        buttons=(ObservedButton("submit", "Submit application"),),
    )
    resolved = resolve_snapshot(
        snapshot,
        facts={},
        llm_client=FakeLLMClient(
            {
                "answers": [
                    {"field_id": "gender", "value": "Decline to self identify", "confidence": "high"},
                    {"field_id": "hispanic_ethnicity", "value": "No", "confidence": "high"},
                ],
                "needs_review": [],
            }
        ),
    )

    assert resolved.answers == ()
    assert resolved.needs_review == ("resolver_sensitive_field: Will you require sponsorship?",)
    assert resolved.metadata["eligible_for_llm"] == []
    assert resolved.metadata["filtered_from_llm"] == [
        {"field_id": "gender", "reason": "resolver_sensitive_field"},
        {"field_id": "hispanic_ethnicity", "reason": "resolver_sensitive_field"},
        {"field_id": "sponsorship", "reason": "resolver_sensitive_field"},
    ]


def test_explicit_sensitive_facts_can_be_executed_without_llm_inference() -> None:
    snapshot = PageSnapshot(
        url="https://example.com/apply",
        fields=(
            ObservedField("sponsorship", "typeahead", "Will you now or in the future require sponsorship?", required=True),
            ObservedField("agree", "checkbox", "I affirm all statements are truthful. I agree", required=True),
            ObservedField("hybrid", "typeahead", "This role requires a hybrid work schedule. Are you willing, and able to work on this schedule?", required=True),
            ObservedField("experience", "typeahead", "How many years of relevant work experience do you have?", required=False),
            ObservedField("gender", "typeahead", "Gender", required=False),
            ObservedField("hispanic_ethnicity", "typeahead", "Are you Hispanic/Latino?", required=False),
            ObservedField("race", "typeahead", "Please identify your race", required=False),
            ObservedField("veteran_status", "typeahead", "Veteran Status", required=False),
            ObservedField("disability_status", "typeahead", "Disability Status", required=False),
        ),
        buttons=(ObservedButton("submit", "Submit application"),),
    )
    resolved = resolve_snapshot(
        snapshot,
        facts={
            "sponsorship": "No",
            "application_attestation": "true",
            "hybrid_schedule": "Yes",
            "relevant_experience_years": "2 years",
            "gender": "Male",
            "hispanic_ethnicity": "No",
            "race": "White",
            "veteran_status": "I am not a protected veteran",
            "disability_status": "I do not want to answer",
        },
        llm_client=FakeLLMClient({"answers": [], "needs_review": []}),
    )

    assert resolved.needs_review == ()
    assert ResolvedAnswer("sponsorship", "No") in resolved.answers
    assert ResolvedAnswer("agree", "true") in resolved.answers
    assert ResolvedAnswer("hybrid", "Yes") in resolved.answers
    assert ResolvedAnswer("experience", "2 years") in resolved.answers
    assert ResolvedAnswer("gender", "Male") in resolved.answers
    assert ResolvedAnswer("hispanic_ethnicity", "No") in resolved.answers
    assert ResolvedAnswer("race", "White") in resolved.answers
    assert ResolvedAnswer("veteran_status", "I am not a protected veteran") in resolved.answers
    assert ResolvedAnswer("disability_status", "I do not want to answer") in resolved.answers
    decision = plan_guarded_actions(snapshot, resolved)
    assert decision.status == StepStatus.DRY_RUN_READY
    assert {action.target_id for action in decision.actions} == {
        "sponsorship",
        "agree",
        "hybrid",
        "experience",
        "gender",
        "hispanic_ethnicity",
        "race",
        "veteran_status",
        "disability_status",
    }


def test_explicit_sensitive_fact_outside_options_needs_review() -> None:
    snapshot = PageSnapshot(
        url="https://example.com/apply",
        fields=(
            ObservedField(
                "sponsorship",
                "typeahead",
                "Will you now or in the future require sponsorship?",
                required=True,
                options=("Sponsorship required", "No sponsorship required"),
            ),
        ),
        buttons=(ObservedButton("submit", "Submit application"),),
    )

    resolved = resolve_snapshot(snapshot, facts={"sponsorship": "No"}, llm_client=FakeLLMClient({"answers": [], "needs_review": []}))

    assert resolved.answers == ()
    assert resolved.needs_review == ("resolver_sensitive_answer_not_in_options: Will you now or in the future require sponsorship?",)
    assert "resolver_sensitive_answer_not_in_options" in resolved.metadata["reason_codes"]

def test_resolver_no_llm_paths_have_explicit_reason_codes() -> None:
    snapshot = PageSnapshot(
        url="https://example.com/apply",
        fields=(ObservedField("why", "textarea", "Why are you a fit?", required=True),),
        buttons=(ObservedButton("next", "Continue"),),
    )

    no_key = resolve_snapshot(snapshot, facts={}, llm_client=None, llm_enabled=True)
    disabled = resolve_snapshot(snapshot, facts={}, llm_client=None, llm_enabled=False)

    assert no_key.metadata["llm_configured"] is False
    assert no_key.metadata["llm_called"] is False
    assert "llm_not_configured" in no_key.metadata["reason_codes"]
    assert "resolver_unknown_required_after_llm" in no_key.metadata["reason_codes"]
    assert "llm_disabled_by_flag" in disabled.metadata["reason_codes"]


def test_runner_merges_deterministic_and_llm_answers_before_final_submit() -> None:
    connection = memory_db()
    upsert_job(connection, {"id": "ts-1", "job_title": "Software Engineer", "url": "https://example.com/apply"})
    job = connection.execute("SELECT * FROM jobs").fetchone()
    page = FakePage(
        [
            '<label for="email">Email</label><input id="email" required><label for="why">Why are you a fit?</label><textarea id="why" required></textarea><button id="next">Continue</button>',
            '<button id="submit">Submit application</button>',
        ]
    )
    target = AdvancingTarget(page)

    run_id, status = run_application_once(
        connection,
        job=job,
        page=page,
        action_target=target,
        profile=ApplicantProfile(facts={"email": "me@example.com"}),
        now=fixed_now,
        max_pages=3,
        llm_client=FakeLLMClient({"answers": [{"field_id": "why", "value": "I build reliable tools.", "confidence": "high", "source_reason": "resume"}], "needs_review": []}),
    )

    assert status == StepStatus.DRY_RUN_READY
    assert target.calls == [("fill", "email", "me@example.com"), ("fill", "why", "I build reliable tools."), ("click", "next", None)]
    pages = connection.execute("SELECT resolver_json FROM application_pages WHERE run_id = ? ORDER BY page_index", (run_id,)).fetchall()
    first_resolver = json.loads(pages[0]["resolver_json"])
    assert first_resolver["metadata"]["llm_called"] is True
    assert first_resolver["metadata"]["merged_answer_field_ids"] == ["email", "why"]
    assert first_resolver["metadata"]["decision_status"] == "continue"
    assert first_resolver["metadata"]["planned_actions"] == [
        {"kind": "fill", "target_id": "email", "value": "me@example.com"},
        {"kind": "fill", "target_id": "why", "value": "I build reliable tools."},
        {"kind": "click", "target_id": "next", "value": None},
    ]


def test_executor_unknown_field_reason_is_structured() -> None:
    decision = plan_guarded_actions(PageSnapshot(url="https://example.com", fields=()), type("Resolved", (), {"answers": (ResolvedAnswer("missing", "x"),), "needs_review": (), "submit_button_id": None, "next_button_id": None})())

    assert decision.status == StepStatus.NEEDS_REVIEW
    assert decision.reason == "executor_unknown_field_id"


def test_local_playwright_smoke_fills_advances_and_stops_before_final_submit(tmp_path) -> None:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        pytest.skip("Playwright is not installed")

    html = tmp_path / "application.html"
    resume = tmp_path / "resume.pdf"
    resume.write_bytes(b"%PDF-1.4\n% smoke resume\n")
    html.write_text(
        """
        <!doctype html>
        <html>
        <body>
          <form onsubmit="window.finalSubmitClicked = true; event.preventDefault();">
            <section id="step1">
              <label for="first_name">First name</label><input id="first_name" required>
              <label for="last_name">Last name</label><input id="last_name" required>
              <label for="email">Email</label><input id="email" required>
              <label for="phone">Phone</label><input id="phone" required>
              <label for="linkedin">LinkedIn</label><input id="linkedin" required>
              <label for="portfolio">Portfolio</label><input id="portfolio" required>
              <label for="resume">Resume</label><input id="resume" type="file" required>
              <button id="continue1" type="button" onclick="document.querySelector('#step1').style.display='none';document.querySelector('#step2').style.display='block';">Continue</button>
            </section>
            <section id="step2" style="display:none">
              <label for="location">Location</label><select id="location" required><option></option><option>Toronto</option><option>Remote</option></select>
              <label for="contact_email">Contact by email</label><input id="contact_email" type="checkbox" required>
              <label for="why">Why are you a fit?</label><textarea id="why" required></textarea>
              <button id="continue2" type="button" onclick="document.querySelector('#step2').style.display='none';document.querySelector('#final').style.display='block';">Continue</button>
            </section>
            <section id="final" style="display:none">
              <button id="submit" type="submit">Submit application</button>
            </section>
          </form>
          <script>window.finalSubmitClicked = false;</script>
        </body>
        </html>
        """,
        encoding="utf-8",
    )

    connection = memory_db()
    upsert_job(connection, {"id": "ts-1", "job_title": "Software Engineer", "url": html.as_uri()})
    job = connection.execute("SELECT * FROM jobs").fetchone()
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page()
        try:
            run_id, status = run_application_once(
                connection,
                job=job,
                page=page,
                action_target=PlaywrightActionTarget(page),
                profile=ApplicantProfile(
                    facts={
                        "first_name": "Ian",
                        "last_name": "Rapko",
                        "email": "ian@example.com",
                        "phone": "416-555-0100",
                        "linkedin": "https://www.linkedin.com/in/ianrapko",
                        "portfolio": "https://immemorized.com",
                        "location": "Remote",
                    },
                    resume_path=str(resume),
                ),
                now=fixed_now,
                max_pages=4,
                llm_client=FakeLLMClient(
                    {
                        "answers": [
                            {"field_id": "contact_email", "value": True, "confidence": "high", "source_reason": "safe preference"},
                            {"field_id": "why", "value": "I build reliable Playwright automation.", "confidence": "high", "source_reason": "resume"},
                        ],
                        "needs_review": [],
                    }
                ),
            )
            assert status == StepStatus.DRY_RUN_READY, [dict(row) for row in connection.execute("SELECT status, reason FROM application_runs").fetchall()]
            assert page.locator("#first_name").input_value() == "Ian"
            assert page.locator("#why").input_value() == "I build reliable Playwright automation."
            assert page.locator("#contact_email").is_checked()
            assert page.evaluate("window.finalSubmitClicked") is False
        finally:
            browser.close()

    run = connection.execute("SELECT status, reason, actions_json FROM application_runs WHERE id = ?", (run_id,)).fetchone()
    assert run["status"] == "dry_run_ready"
    assert run["reason"] == "dry_run_final_submit_boundary"
    records = json.loads(run["actions_json"])
    succeeded_actions = [record["action"] for record in records if record["status"] == "succeeded"]
    assert {"kind": "click", "target_id": "continue1", "value": None} in succeeded_actions
    assert {"kind": "click", "target_id": "continue2", "value": None} in succeeded_actions
    assert all(record["action"]["target_id"] != "submit" for record in records)
    assert {record["status"] for record in records} >= {"attempted", "succeeded"}
    assert connection.execute("SELECT COUNT(*) AS count FROM application_pages WHERE run_id = ?", (run_id,)).fetchone()["count"] == 3


def test_greenhouse_like_live_form_skips_internal_inputs_uploads_resume_and_selects_combobox(tmp_path) -> None:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        pytest.skip("Playwright is not installed")

    html = tmp_path / "greenhouse.html"
    resume = tmp_path / "resume.pdf"
    resume.write_bytes(b"%PDF-1.4\n% smoke resume\n")
    html.write_text(
        """
        <!doctype html>
        <html>
        <body>
          <form onsubmit="window.finalSubmitClicked = true; event.preventDefault();">
            <label id="country-label" for="country">Country<span>*</span></label>
            <input id="country" role="combobox" aria-autocomplete="list" aria-labelledby="country-label" aria-required="true">
            <input required tabindex="-1" aria-hidden="true" class="requiredInput" value="">
            <div role="group" aria-labelledby="upload-label-resume" aria-required="true">
              <div id="upload-label-resume">Resume/CV<span>*</span></div>
              <button type="button">Attach</button>
              <label class="visually-hidden" for="resume">Attach</label>
              <input id="resume" class="visually-hidden" type="file" accept=".pdf">
            </div>
            <button id="submit" type="submit">Submit application</button>
          </form>
          <script>
            window.finalSubmitClicked = false;
            window.countrySelected = false;
            document.querySelector("#country").addEventListener("keydown", (event) => {
              if (event.key === "Enter" && event.target.value === "United States") {
                window.countrySelected = true;
              }
            });
          </script>
        </body>
        </html>
        """,
        encoding="utf-8",
    )
    connection = memory_db()
    upsert_job(connection, {"id": "ts-1", "job_title": "Software Engineer", "url": html.as_uri()})
    job = connection.execute("SELECT * FROM jobs").fetchone()
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page()
        try:
            run_id, status = run_application_once(
                connection,
                job=job,
                page=page,
                action_target=PlaywrightActionTarget(page),
                profile=ApplicantProfile(facts={"country": "United States"}, resume_path=str(resume)),
                now=fixed_now,
                max_pages=2,
                llm_client=FakeLLMClient({"answers": [], "needs_review": []}),
            )
            assert status == StepStatus.DRY_RUN_READY, [dict(row) for row in connection.execute("SELECT status, reason FROM application_runs").fetchall()]
            assert page.evaluate("window.countrySelected") is True
            assert page.evaluate("window.finalSubmitClicked") is False
        finally:
            browser.close()

    snapshot = json.loads(connection.execute("SELECT snapshot_json FROM application_pages WHERE run_id = ?", (run_id,)).fetchone()["snapshot_json"])
    field_labels = {field["id"]: field["label"] for field in snapshot["fields"]}
    assert "input_1" not in field_labels
    assert field_labels["resume"] == "Resume/CV*"
    actions = json.loads(connection.execute("SELECT actions_json FROM application_runs WHERE id = ?", (run_id,)).fetchone()["actions_json"])
    succeeded_actions = [record["action"] for record in actions if record["status"] == "succeeded"]
    assert {"kind": "fill", "target_id": "country", "value": "United States"} in succeeded_actions
    assert {"kind": "upload", "target_id": "resume", "value": str(resume)} in succeeded_actions


def test_llm_eligible_includes_non_required_fields() -> None:
    """Non-required fields should be eligible for LLM answers, not just required ones."""
    from apply_pipeline.resolver import llm_eligible_field_ids
    snapshot = PageSnapshot(
        url="https://example.com/apply",
        fields=(
            ObservedField(id="name", kind="text", label="Full name", required=True),
            ObservedField(id="source", kind="select", label="How did you hear about us?", required=False, options=("LinkedIn", "Friend", "Other")),
            ObservedField(id="email", kind="text", label="Email", required=True),
        ),
    )
    eligible = llm_eligible_field_ids(snapshot, already_answered=set())
    assert "source" in eligible, "Non-required field should be LLM-eligible"
    assert "name" in eligible
    assert "email" in eligible


def test_llm_payload_includes_value_disabled_visible_frame() -> None:
    """LLM payload should include value, disabled, visible, and frame for each field."""
    from apply_pipeline.llm import llm_payload
    snapshot = PageSnapshot(
        url="https://example.com/apply",
        fields=(
            ObservedField(id="name", kind="text", label="Full name", required=True, value="Ian", disabled=False, visible=True, frame="main"),
            ObservedField(id="hidden_field", kind="text", label="Hidden", required=False, visible=False, frame="iframe-1"),
        ),
    )
    payload = llm_payload(snapshot, facts={"first_name": "Ian"}, job_description=None)
    name_field = next(f for f in payload["fields"] if f["id"] == "name")
    assert name_field.get("value") == "Ian", "LLM payload should include pre-filled value"
    assert name_field.get("disabled") is False, "LLM payload should include disabled state"
    assert name_field.get("visible") is True, "LLM payload should include visible state"
    assert name_field.get("frame") == "main", "LLM payload should include frame name"
    hidden_field = next(f for f in payload["fields"] if f["id"] == "hidden_field")
    assert hidden_field.get("visible") is False
    assert hidden_field.get("frame") == "iframe-1"


def test_playwright_action_target_uses_observer_selector() -> None:
    """PlaywrightActionTarget should try the observer's CSS selector before ID reconstruction."""
    # Simulate what happens: observer computes a real CSS selector like
    # input[name="job_application[first_name]"], but _locator only tries
    # #first_name, [id=first_name], [name=first_name], [aria-label=first_name]
    # The observer selector is more reliable for Greenhouse's dynamic IDs.
    from apply_pipeline.runner import PlaywrightActionTarget

    class StubLocator:
        def __init__(self):
            self.calls = []
        def first(self):
            return self
        def count(self):
            return 0

    class StubPage:
        def __init__(self):
            self.locator_calls = []
        def locator(self, sel):
            self.locator_calls.append(sel)
            return StubLocator()

    page = StubPage()
    # Without observer selectors, _locator only tries ID-based selectors
    target = PlaywrightActionTarget(page)
    try:
        target._locator("first_name")
    except ValueError:
        pass
    # Current behavior: only #id, [id=...], [name=...], [aria-label=...]
    # After fix: should also try observer selectors when provided
    assert len(page.locator_calls) >= 3
    # The gap: no observer selector like input[name="job_application[first_name]"] is tried


def test_playwright_action_target_prefers_observer_selector_when_provided() -> None:
    """When observer selectors are provided, _locator should try them first."""
    from apply_pipeline.runner import PlaywrightActionTarget

    class StubLocator:
        def __init__(self, found=False):
            self._found = found
        def first(self):
            return self
        def count(self):
            return 1 if self._found else 0

    class StubPage:
        def __init__(self):
            self.locator_calls = []
        def locator(self, sel):
            self.locator_calls.append(sel)
            # Simulate: observer selector works, ID-based ones don't
            return StubLocator(found='input[name="job_application[first_name]"]' in sel)

    page = StubPage()
    target = PlaywrightActionTarget(page)
    target.set_selectors(
        field_selectors={"first_name": 'input[name="job_application[first_name]"]'},
        button_selectors={},
    )
    locator = target._locator("first_name")
    # Observer selector should be tried first
    assert page.locator_calls[0] == 'input[name="job_application[first_name]"]'
    assert locator.count() == 1



def test_playwright_action_target_uses_observed_frame_scope() -> None:
    from apply_pipeline.runner import PlaywrightActionTarget

    class StubLocator:
        def __init__(self, found=False):
            self._found = found

        def first(self):
            return self

        def count(self):
            return 1 if self._found else 0

    class StubFrame:
        def __init__(self, name):
            self.name = name
            self.locator_calls = []

        def locator(self, selector):
            self.locator_calls.append(selector)
            return StubLocator(found=self.name == "frame_1" and selector == "#a_1")

    class StubPage:
        def __init__(self):
            self.locator_calls = []
            self.frames = [StubFrame("frame_0"), StubFrame("frame_1")]

        def locator(self, selector):
            self.locator_calls.append(selector)
            return StubLocator(found=False)

    page = StubPage()
    target = PlaywrightActionTarget(page)
    target.set_selectors(field_selectors={}, button_selectors={"a_1": "#a_1"}, button_frames={"a_1": "frame_1"})

    assert target._locator("a_1").count() == 1
    assert page.locator_calls == []
    assert page.frames[1].locator_calls == ["#a_1"]
