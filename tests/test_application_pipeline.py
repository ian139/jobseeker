"""Tests for application.py: observe_html, resolve_with_llm, load_resume_context,
record_application_run, and no-click safety gate.

No Puppeteer/browser required — all tests exercise source-level behavior
through the public API with monkeypatch, tmp_path, and in-memory SQLite.
"""

import asyncio
import json
from pathlib import Path

import pytest
import jobs_assistant.application as application_mod

from jobs_assistant.application import (
    BLOCKED_STATUS,
    COMPLETED_STATUS,
    FINAL_RE,
    MANUAL_STATUS,
    UNSAFE_RE,
    DEFAULT_LLM_MODEL,
    DEFAULT_LLM_THINK,
    AutofillPlan,
    FieldAnswer,
    ObservedButton,
    ObservedField,
    PageObservation,
    GREENHOUSE_ITERATION_PATH,
    _field_is_sensitive,
    _safe_observed_button_selector,
    load_resume_context,
    load_resume_metadata,
    job_description_from_row,
    observe_html,
    record_application_run,
    resolve_with_llm,
    plan_action_evidence,
    unresolved_required_fields,
    write_application_artifacts,
    run_browser_autofill,
)
from jobs_assistant.ats import ApplicationContext, GreenhouseAdapter, classify_application_site, merge_plans
from jobs_assistant.db import connect, init_db


# ---------------------------------------------------------------------------
# observe_html
# ---------------------------------------------------------------------------

SIMPLE_FORM = """<html>
<body>
<form>
  <input type="text" name="full_name" id="name" aria-label="Full Name" required>
  <input type="email" name="email" placeholder="you@example.com">
  <input type="hidden" name="csrf" value="abc">
  <input type="submit" value="Submit Application">
  <textarea name="cover_letter" id="cover" placeholder="Tell us about yourself"></textarea>
  <select name="source" id="source-select" required>
    <option value="linkedin">LinkedIn</option>
    <option value="referral">Referral</option>
  </select>
  <button aria-label="Next Page">Next</button>
  <button aria-label="Submit Final">Submit Final</button>
  <span class="error">This field is required</span>
  <div>Invalid email address</div>
</form>
</body>
</html>"""


def test_observe_html_extracts_text_and_email_fields():
    """Contract: observe_html parses <input type=text|email> into ObservedField
    with correct kind, name, label, selector, and required flag."""
    obs = observe_html(SIMPLE_FORM, url="https://a.test/apply", title="Apply")

    names = {f.name: f for f in obs.fields}
    assert "full_name" in names
    assert names["full_name"].kind == "text"
    assert names["full_name"].label == "Full Name"
    assert names["full_name"].selector == "#name"
    assert names["full_name"].required is True

    assert "email" in names
    assert names["email"].kind == "email"
    assert names["email"].label == "you@example.com"
    assert names["email"].selector == "input[name='email']"
    assert names["email"].required is False


def test_observe_html_skips_hidden_and_submit_inputs():
    """Contract: hidden/submit/button/reset/image inputs are excluded from fields;
    submit/button types become ObservedButton entries."""
    obs = observe_html(SIMPLE_FORM)

    field_names = {f.name for f in obs.fields}
    assert "csrf" not in field_names  # hidden — excluded

    button_texts = {b.text for b in obs.buttons}
    assert "Submit Application" in button_texts


def test_observe_html_extracts_textarea_and_select():
    """Contract: <textarea> and <select> produce ObservedField entries;
    <select> captures its <option> values."""
    obs = observe_html(SIMPLE_FORM)

    by_kind = {f.kind: f for f in obs.fields}
    assert "textarea" in by_kind
    assert by_kind["textarea"].name == "cover_letter"
    assert by_kind["textarea"].label == "Tell us about yourself"

    assert "select" in by_kind
    assert by_kind["select"].name == "source"
    assert by_kind["select"].options == ("linkedin", "referral")
    assert by_kind["select"].required is True


def test_observe_html_buttons_safe_flag():
    """Contract: buttons matching FINAL_RE (submit, final, complete application, etc.)
    are marked safe=False; generic tag-only selectors are also unsafe."""
    obs = observe_html(SIMPLE_FORM)

    by_text = {b.text: b for b in obs.buttons}
    # "Next Page" has no id/name → selector="button" (generic) → safe=False
    assert by_text["Next Page"].safe is False
    assert by_text["Submit Final"].safe is False


def test_observe_html_extracts_error_text():
    """Contract: observe_html scans HTML for error/required/invalid patterns
    and returns them in the errors tuple."""
    obs = observe_html(SIMPLE_FORM)

    assert any("required" in e.lower() for e in obs.errors)
    assert any("invalid" in e.lower() for e in obs.errors)


def test_observe_html_empty_form():
    """Contract: a page with no form elements returns empty fields/buttons/errors."""
    obs = observe_html("<html><body><p>Hello</p></body></html>")

    assert obs.fields == ()
    assert obs.buttons == ()
    assert obs.errors == ()


def test_observe_html_preserves_url_and_title():
    obs = observe_html("<html><head><title>Acme Careers</title></head><body></body></html>",
                       url="https://jobs.acme.test/apply", title="Acme Careers")
    assert obs.url == "https://jobs.acme.test/apply"
    assert obs.title == "Acme Careers"


# ---------------------------------------------------------------------------
# _field_is_sensitive / UNSAFE_RE
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("label,expected", [
    ("Full Name", False),
    ("Email Address", False),
    ("Phone Number", False),
    ("Social Security Number", True),
    ("SSN", True),
    ("Work Authorization Status", True),
    ("Visa Sponsorship Required", True),
    ("Gender", True),
    ("Race / Ethnicity", True),
    ("Veteran Status", True),
    ("Disability", True),
    ("Credit Card Number", True),
    ("Payment Method", True),
    ("Password", True),
    ("Sign In", True),
    ("Login", True),
    ("Log In", True),
    ("Captcha", True),
    ("Assessment", True),
    ("EEO", True),
    ("Signature", True),
    ("Submit", True),
    ("Final", True),
])
def test_field_is_sensitive_matches_unsafe_re(label, expected):
    """Contract: _field_is_sensitive returns True for any field whose
    name/label/kind contains an UNSAFE_RE keyword."""
    field = ObservedField(kind="text", name=None, label=label, selector="#f")
    assert _field_is_sensitive(field) == expected


def test_field_is_sensitive_searches_name_and_kind_too():
    """Contract: sensitivity check spans name and kind, not just label."""
    assert _field_is_sensitive(ObservedField(kind="password", name=None, label="", selector="#f")) is True
    assert _field_is_sensitive(ObservedField(kind="text", name="ssn", label="", selector="#f")) is True


# ---------------------------------------------------------------------------
# resolve_with_llm
# ---------------------------------------------------------------------------

SAFE_FIELDS = (
    ObservedField(kind="text", name="full_name", label="Full Name", selector="#name"),
    ObservedField(kind="email", name="email", label="Email", selector="#email"),
)

SENSITIVE_FIELDS = (
    ObservedField(kind="text", name="ssn", label="SSN", selector="#ssn"),
    ObservedField(kind="password", name="pw", label="Password", selector="#pw"),
)

MIXED_FIELDS = SAFE_FIELDS + SENSITIVE_FIELDS

SAFE_BUTTONS = (
    ObservedButton(text="Next", selector="button[name='next']", safe=True),
    ObservedButton(text="Submit Final", selector="#submit", safe=False),
)

JOB = {"title": "Software Engineer", "company": "Acme", "canonical_url": "https://a.test/apply"}


def test_resolve_missing_api_key_returns_manual(monkeypatch):
    """Contract: when no API key is available (env or arg), resolve_with_llm
    returns MANUAL_STATUS with 'missing LLM API key' reason."""
    monkeypatch.delenv("OLLAMA_CLOUD_API_KEY", raising=False)
    monkeypatch.delenv("OLLAMA_API_KEY", raising=False)
    plan = resolve_with_llm(
        PageObservation(url="https://a.test", title="Apply", fields=SAFE_FIELDS, buttons=SAFE_BUTTONS),
        job=JOB,
        resume_context="resume text",
        api_key=None,
    )
    assert plan.status == MANUAL_STATUS
    assert "missing llm api key" in plan.reason.lower()


def test_resolve_all_fields_sensitive_returns_manual():
    """Contract: when every field is sensitive, resolve_with_llm returns
    MANUAL_STATUS with 'no safe answerable fields' — no LLM call needed."""
    plan = resolve_with_llm(
        PageObservation(url="https://a.test", title="Apply", fields=SENSITIVE_FIELDS, buttons=SAFE_BUTTONS),
        job=JOB,
        resume_context="resume text",
        api_key="fake-key",
    )
    assert plan.status == MANUAL_STATUS
    assert "no safe answerable fields" in plan.reason.lower()
    assert len(plan.skipped_fields) == len(SENSITIVE_FIELDS)


def test_resolve_sensitive_fields_are_skipped(monkeypatch):
    """Contract: sensitive fields appear in skipped_fields; only safe fields
    are sent to the LLM."""
    monkeypatch.delenv("OLLAMA_CLOUD_API_KEY", raising=False)
    monkeypatch.delenv("OLLAMA_API_KEY", raising=False)
    plan = resolve_with_llm(
        PageObservation(url="https://a.test", title="Apply", fields=MIXED_FIELDS, buttons=SAFE_BUTTONS),
        job=JOB,
        resume_context="resume text",
        api_key=None,  # no key → manual, but skipped_fields still populated
    )
    assert len(plan.skipped_fields) == len(SENSITIVE_FIELDS)
    assert plan.status == MANUAL_STATUS



def test_resolve_required_sensitive_field_blocks_autofill_before_llm(monkeypatch):
    """Contract: required sensitive/manual fields block the run even when
    safe fields are present, so a draft is not marked complete prematurely."""
    monkeypatch.delenv("OLLAMA_CLOUD_API_KEY", raising=False)
    monkeypatch.delenv("OLLAMA_API_KEY", raising=False)
    plan = resolve_with_llm(
        PageObservation(
            url="https://a.test",
            title="Apply",
            fields=SAFE_FIELDS + (ObservedField(kind="text", name="sponsorship", label="Visa Sponsorship", selector="#visa", required=True),),
            buttons=SAFE_BUTTONS,
        ),
        job=JOB,
        resume_context="resume text",
        api_key="fake-key",
    )

    assert plan.status == MANUAL_STATUS
    assert plan.answers == ()
    assert "required sensitive" in plan.reason
    assert plan.raw["blocking_sensitive_fields"] == ["Visa Sponsorship"]


def test_resolve_llm_explicit_manual_status_overrides_answers(monkeypatch):
    """Contract: when the LLM returns status:'manual' (not 'ready'), the
    resolver returns MANUAL_STATUS with empty answers and preserves the
    reason, even when the LLM also returns high-confidence answers."""
    monkeypatch.delenv("OLLAMA_CLOUD_API_KEY", raising=False)
    monkeypatch.delenv("OLLAMA_API_KEY", raising=False)
    monkeypatch.delenv("OLLAMA_CLOUD_BASE_URL", raising=False)
    monkeypatch.delenv("OLLAMA_CLOUD_MODEL", raising=False)
    monkeypatch.delenv("DEEPSEEK_MODEL", raising=False)

    fake_response = {
        "message": {
            "content": json.dumps({
                "answers": [
                    {"selector": "#name", "value": "John", "confidence": 0.95, "reason": "resume match"},
                    {"selector": "#email", "value": "john@test.com", "confidence": 0.90, "reason": "resume match"},
                ],
                "safe_button_selector": "button[name='next']",
                "status": "manual",
                "reason": "page requires human judgment for EEO section",
            })
        }
    }

    class FakeResponse:
        def raise_for_status(self): pass
        def json(self): return fake_response

    monkeypatch.setattr("jobs_assistant.application.httpx.post", lambda *a, **kw: FakeResponse())

    plan = resolve_with_llm(
        PageObservation(url="https://a.test", title="Apply", fields=SAFE_FIELDS, buttons=SAFE_BUTTONS),
        job=JOB,
        resume_context="resume text",
        api_key="fake-key",
    )
    assert plan.status == MANUAL_STATUS
    assert plan.answers == ()
    assert plan.reason == "page requires human judgment for EEO section"


def test_resolve_llm_defaults_to_deepseek_flash_low_thinking(monkeypatch):
    """Contract: default app-runtime LLM inference uses DeepSeek V4 Flash
    through Ollama native chat with low thinking unless env/caller overrides."""
    monkeypatch.delenv("OLLAMA_CLOUD_MODEL", raising=False)
    monkeypatch.delenv("DEEPSEEK_MODEL", raising=False)
    monkeypatch.delenv("OLLAMA_CLOUD_THINK", raising=False)
    monkeypatch.delenv("OLLAMA_CLOUD_REASONING", raising=False)
    captured: dict[str, object] = {}

    class FakeResponse:
        def raise_for_status(self): pass
        def json(self):
            return {"message": {"content": json.dumps({
                "answers": [{"selector": "#name", "value": "Jane", "confidence": 0.95}],
                "status": "ready",
                "reason": "ok",
            })}}

    def fake_post(*args, **kwargs):
        captured.update(kwargs["json"])
        return FakeResponse()

    monkeypatch.setattr("jobs_assistant.application.httpx.post", fake_post)
    plan = resolve_with_llm(
        PageObservation(url="https://a.test", title="Apply", fields=SAFE_FIELDS, buttons=SAFE_BUTTONS),
        job=JOB,
        resume_context="resume text",
        api_key="fake-key",
    )
    assert plan.status == "ready"
    assert captured["model"] == DEFAULT_LLM_MODEL == "deepseek-v4-flash"
    assert captured["think"] == DEFAULT_LLM_THINK == "low"


def test_resolve_prompt_includes_resume_metadata_and_job_description(monkeypatch):
    captured: dict[str, object] = {}

    class FakeResponse:
        def raise_for_status(self): pass
        def json(self):
            return {"message": {"content": json.dumps({
                "answers": [{"selector": "#name", "value": "Jane", "confidence": 0.95}],
                "status": "ready",
                "reason": "ok",
            })}}

    def fake_post(*args, **kwargs):
        captured.update(kwargs["json"])
        return FakeResponse()

    monkeypatch.setattr("jobs_assistant.application.httpx.post", fake_post)

    plan = resolve_with_llm(
        PageObservation(url="https://a.test", title="Apply", fields=SAFE_FIELDS, buttons=SAFE_BUTTONS),
        job=JOB,
        resume_context="resume text",
        resume_metadata={"skills": ["python", "sqlite"]},
        job_description="Build ingestion pipelines and SQLite tooling.",
        api_key="fake-key",
    )

    prompt = json.loads(captured["messages"][0]["content"])
    assert plan.status == "ready"
    assert prompt["resume_context"] == "resume text"
    assert prompt["resume_metadata"] == {"skills": ["python", "sqlite"]}
    assert prompt["job_description"] == "Build ingestion pipelines and SQLite tooling."

# ---------------------------------------------------------------------------
# load_resume_context
# ---------------------------------------------------------------------------

RESUME_MD = "# John Doe\nSoftware Engineer with 5 years experience."
RESUME_TXT = "John Doe — Software Engineer"
PROFILE_JSON = '{"name": "John Doe", "title": "Software Engineer"}'
OTHER_MD = "# Cover Letter\nI am excited to apply."


def test_load_resume_context_case_insensitive_main_resume_md(tmp_path: Path):
    """Contract: load_resume_context matches main_resume.md case-insensitively."""
    (tmp_path / "Main_Resume.md").write_text(RESUME_MD)
    result = load_resume_context(tmp_path)
    assert result == RESUME_MD


def test_load_resume_context_case_insensitive_main_resume_txt(tmp_path: Path):
    """Contract: load_resume_context matches main_resume.txt case-insensitively."""
    (tmp_path / "MAIN_RESUME.TXT").write_text(RESUME_TXT)
    result = load_resume_context(tmp_path)
    assert result == RESUME_TXT


def test_load_resume_context_case_insensitive_profile_json(tmp_path: Path):
    """Contract: load_resume_context matches profile.json case-insensitively."""
    (tmp_path / "Profile.json").write_text(PROFILE_JSON)
    result = load_resume_context(tmp_path)
    assert result == PROFILE_JSON


def test_load_resume_context_skips_non_main_resume_md(tmp_path: Path):
    """Contract: load_resume_context only matches main_resume.* and profile.json;
    other .md files are ignored and FileNotFoundError is raised."""
    (tmp_path / "cover_letter.md").write_text(OTHER_MD)
    with pytest.raises(FileNotFoundError, match="no main_resume"):
        load_resume_context(tmp_path)

def test_load_resume_context_reads_pdf_via_pypdf(tmp_path: Path, monkeypatch):
    """Contract: when main_resume.pdf exists and pypdf is available,
    load_resume_context extracts text from the PDF."""
    import sys
    from types import SimpleNamespace

    pdf = tmp_path / "main_resume.pdf"
    pdf.write_text("fake pdf bytes")

    class FakePage:
        def extract_text(self):
            return "Extracted PDF resume text"

    class FakeReader:
        def __init__(self, path):
            self.pages = [FakePage()]

    fake_pypdf = SimpleNamespace(PdfReader=FakeReader)
    monkeypatch.setitem(sys.modules, "pypdf", fake_pypdf)
    result = load_resume_context(tmp_path)
    assert result == "Extracted PDF resume text"


def test_load_resume_context_raises_when_nothing_found(tmp_path: Path):
    """Contract: load_resume_context raises FileNotFoundError when the
    directory has no matching resume files."""
    (tmp_path / "random.txt").write_text("not a resume")
    with pytest.raises(FileNotFoundError, match="no main_resume"):
        load_resume_context(tmp_path)


def test_load_resume_context_raises_when_dir_missing(tmp_path: Path):
    """Contract: load_resume_context raises FileNotFoundError when the
    resume directory does not exist."""
    with pytest.raises(FileNotFoundError, match="resume directory not found"):
        load_resume_context(tmp_path / "nonexistent")


def test_load_resume_context_finds_main_resume_md(tmp_path: Path):
    """Contract: main_resume.md is found when present in the directory."""
    (tmp_path / "main_resume.md").write_text(RESUME_MD)
    result = load_resume_context(tmp_path)
    assert result == RESUME_MD


def test_load_resume_metadata_absent_returns_empty(tmp_path: Path):
    assert load_resume_metadata(tmp_path) == {}


def test_load_resume_metadata_filters_allowed_sections(tmp_path: Path):
    payload = {
        "skills": ["python", "sqlite"],
        "jobs": [{"company": "Acme", "title": "Engineer"}],
        "research": {"systems": ["ATS"]},
        "leadership": ["mentor"],
        "education": {"degree": "BS"},
        "summary": "not allowed",
        "email": "not-prompt-context@example.test",
    }
    (tmp_path / "resume.json").write_text(json.dumps(payload))

    metadata = load_resume_metadata(tmp_path)

    assert metadata == {
        "skills": payload["skills"],
        "jobs": payload["jobs"],
        "research": payload["research"],
        "leadership": payload["leadership"],
        "education": payload["education"],
    }


def test_load_resume_metadata_rejects_non_object_json(tmp_path: Path):
    (tmp_path / "resume.json").write_text(json.dumps(["python", "sqlite"]))

    with pytest.raises(ValueError, match="resume.json must contain a JSON object"):
        load_resume_metadata(tmp_path)


def test_job_description_from_row_prefers_description_column():
    conn = _memory_db()
    conn.execute("UPDATE jobs SET description = ?, raw_json = ? WHERE id = 1", ("Column description", json.dumps({"description": "Raw description"})))
    row = conn.execute("SELECT * FROM jobs WHERE id = 1").fetchone()

    assert job_description_from_row(row) == "Column description"


def test_job_description_from_row_falls_back_to_raw_json_description():
    conn = _memory_db()
    conn.execute("UPDATE jobs SET description = NULL, raw_json = ? WHERE id = 1", (json.dumps({"job_description": "Raw job description"}),))
    row = conn.execute("SELECT * FROM jobs WHERE id = 1").fetchone()

    assert job_description_from_row(row) == "Raw job description"


def test_job_description_from_row_returns_empty_for_malformed_raw_json():
    conn = _memory_db()
    conn.execute("UPDATE jobs SET description = NULL, raw_json = ? WHERE id = 1", ("{not-json",))
    row = conn.execute("SELECT * FROM jobs WHERE id = 1").fetchone()

    assert job_description_from_row(row) == ""


# ---------------------------------------------------------------------------
# record_application_run
# ---------------------------------------------------------------------------

def _memory_db():
    conn = connect(":memory:")
    init_db(conn)
    conn.execute(
        "INSERT INTO jobs (source, source_job_id, canonical_url, title, company, discovered_at, first_seen_at, last_seen_at) "
        "VALUES ('test', 'j1', 'https://a.test/apply', 'Test Job', 'Acme', '2026-01-01T00:00:00', '2026-01-01T00:00:00', '2026-01-01T00:00:00')"
    )
    conn.commit()
    return conn


def test_record_application_run_inserts_and_returns_id():
    """Contract: record_application_run inserts a row into application_runs
    and returns the new row ID."""
    conn = _memory_db()
    run_id = record_application_run(conn, job_id=1, url="https://a.test/apply",
                                    status=MANUAL_STATUS, reason="test")
    assert isinstance(run_id, int)
    assert run_id > 0

    row = conn.execute("SELECT * FROM application_runs WHERE id = ?", (run_id,)).fetchone()
    assert row is not None
    assert row["job_id"] == 1
    assert row["apply_url"] == "https://a.test/apply"
    assert row["status"] == MANUAL_STATUS
    assert row["reason"] == "test"


def test_record_application_run_serializes_observation():
    """Contract: when an observation is provided, it is serialized to
    observation_json with fields, buttons, and errors."""
    conn = _memory_db()
    obs = PageObservation(
        url="https://a.test/apply",
        title="Apply",
        fields=(ObservedField(kind="text", name="full_name", label="Full Name", selector="#name"),),
        buttons=(ObservedButton(text="Next", selector="button[name='next']", safe=True),),
        errors=("Field is required",),
    )
    run_id = record_application_run(conn, job_id=1, url="https://a.test/apply",
                                    status=MANUAL_STATUS, reason="test",
                                    observation=obs)

    row = conn.execute("SELECT observation_json FROM application_runs WHERE id = ?", (run_id,)).fetchone()
    stored = json.loads(row["observation_json"])
    assert stored["url"] == "https://a.test/apply"
    assert stored["title"] == "Apply"
    assert len(stored["fields"]) == 1
    assert stored["fields"][0]["name"] == "full_name"
    assert len(stored["buttons"]) == 1
    assert stored["buttons"][0]["text"] == "Next"
    assert stored["errors"] == ["Field is required"]


def test_record_application_run_serializes_plan():
    """Contract: when a plan is provided, it is serialized to plan_json
    with answers, safe_button_selector, status, reason, and skipped_fields."""
    conn = _memory_db()
    plan = AutofillPlan(
        answers=(FieldAnswer(selector="#name", value="John", confidence=0.95, reason="resume match"),),
        safe_button_selector="button[name='next']",
        status="ready",
        reason="autofill complete",
        skipped_fields=("SSN", "Password"),
    )
    run_id = record_application_run(conn, job_id=1, url="https://a.test/apply",
                                    status=COMPLETED_STATUS, reason="done",
                                    plan=plan)

    row = conn.execute("SELECT plan_json FROM application_runs WHERE id = ?", (run_id,)).fetchone()
    stored = json.loads(row["plan_json"])
    assert stored["status"] == "ready"
    assert stored["safe_button_selector"] == "button[name='next']"
    assert len(stored["answers"]) == 1
    assert stored["answers"][0]["value"] == "John"
    assert stored["skipped_fields"] == ["SSN", "Password"]


def test_record_application_run_handles_none_observation_and_plan():
    """Contract: record_application_run stores empty JSON when observation
    and plan are None."""
    conn = _memory_db()
    run_id = record_application_run(conn, job_id=1, url="https://a.test/apply",
                                    status=BLOCKED_STATUS, reason="timeout")
    row = conn.execute("SELECT observation_json, plan_json FROM application_runs WHERE id = ?", (run_id,)).fetchone()
    assert json.loads(row["observation_json"]) == {}
    assert json.loads(row["plan_json"]) == {}


# ---------------------------------------------------------------------------
# _safe_observed_button_selector — guarded safe-click gate
# ---------------------------------------------------------------------------

SAFE_BUTTON_OBS = PageObservation(
    url="https://a.test/form",
    title="Apply",
    fields=(),
    buttons=(
        ObservedButton(text="Next", selector="button[name='next']", safe=True),
        ObservedButton(text="Submit Final", selector="#submit", safe=False),
    ),
)


def test_safe_observed_button_selector_approves_exact_observed_safe_selector():
    """Contract: passing the exact selector of an observed safe button
    returns that selector — the gate approves it for click."""
    result = _safe_observed_button_selector(SAFE_BUTTON_OBS, "button[name='next']")
    assert result == "button[name='next']"


@pytest.mark.parametrize(
    "selector,description",
    [
        ("button", "generic tag-only selector"),
        ("#submit", "unsafe button selector"),
        ("#nonexistent", "unobserved selector"),
        (None, "None selector"),
    ],
)
def test_safe_observed_button_selector_rejects_unsafe_or_unobserved(selector, description):
    """Contract: _safe_observed_button_selector returns None for generic
    'button' selectors, unsafe buttons, unobserved selectors, and None."""
    result = _safe_observed_button_selector(SAFE_BUTTON_OBS, selector)
    assert result is None, f"expected None for {description}, got {result!r}"


# ---------------------------------------------------------------------------
# UNSAFE_RE / FINAL_RE regression boundaries
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("text,should_match", [
    ("submit", True),
    ("Submit Application", True),
    ("final", True),
    ("Final Review", True),
    ("send application", True),
    ("finish", True),
    ("complete application", True),
    # Safe navigation buttons
    ("Next", False),
    ("Continue", False),
    ("Save Draft", False),
    ("Previous", False),
    ("Upload Resume", False),
    ("Add Experience", False),
])
def test_final_re_matches_submit_variants(text, should_match):
    """Contract: FINAL_RE matches submit/final/complete variants but not
    safe navigation buttons like Next/Continue/Save/Previous."""
    assert bool(FINAL_RE.search(text)) == should_match


@pytest.mark.parametrize("text,should_match", [
    # Unsafe — should match
    ("submit", True),
    ("final", True),
    ("captcha", True),
    ("assessment", True),
    ("payment", True),
    ("credit card", True),
    ("ssn", True),
    ("social security", True),
    ("work authorization", True),
    ("visa", True),
    ("sponsorship", True),
    ("eeo", True),
    ("gender", True),
    ("race", True),
    ("ethnicity", True),
    ("veteran", True),
    ("disability", True),
    ("signature", True),
    ("sign in", True),
    ("login", True),
    ("log in", True),
    ("password", True),
    # Safe — should not match
    ("full name", False),
    ("email address", False),
    ("phone number", False),
    ("linkedin url", False),
    ("github", False),
    ("portfolio", False),
    ("cover letter", False),
    ("resume upload", False),
    ("years of experience", False),
    ("education", False),
    ("skills", False),
])
def test_unsafe_re_boundaries(text, should_match):
    """Contract: UNSAFE_RE matches all sensitive/legal/identity/payment
    keywords and does not match safe profile fields."""
    assert bool(UNSAFE_RE.search(text)) == should_match

# ---------------------------------------------------------------------------
# Greenhouse ATS fixture — mirrors real Greenhouse public application forms
# ---------------------------------------------------------------------------

# Greenhouse uses dynamic IDs that change per session, so the active parser's
# _selector() falls through to name-based selectors when id is absent.
# We deliberately omit id on job_application[...] inputs so the observer
# produces robust input[name='job_application[first_name]'] selectors.
# The parser does NOT associate <label for> text; labels come from
# aria-label > placeholder > name > id.
# <option> text is NOT captured — only value attributes.

GREENHOUSE_FORM = """<html>
<head><title>Acme Corp — Software Engineer</title></head>
<body>
<form id="application_form" action="/apply" method="post">
  <input type="hidden" name="authenticity_token" value="abc123">

  <div class="field">
    <label for="first_name_field">First Name *</label>
    <input type="text" name="job_application[first_name]" aria-label="First Name" placeholder="First Name" required>
  </div>

  <div class="field">
    <label for="last_name_field">Last Name *</label>
    <input type="text" name="job_application[last_name]" aria-label="Last Name" placeholder="Last Name" required>
  </div>

  <div class="field">
    <label for="email_field">Email *</label>
    <input type="email" name="job_application[email]" placeholder="you@example.com" required>
  </div>

  <div class="field">
    <label for="phone_field">Phone *</label>
    <input type="tel" name="job_application[phone]" aria-label="Phone" required>
  </div>

  <div class="field">
    <label for="resume_field">Resume/CV *</label>
    <input type="file" name="job_application[resume]" aria-label="Resume/CV" accept=".pdf,.doc,.docx" required>
  </div>

  <div class="field">
    <label for="source_field">How did you hear about us? *</label>
    <select name="job_application[source]" aria-label="How did you hear about us?" required>
      <option value="">Select...</option>
      <option value="linkedin">LinkedIn</option>
      <option value="referral">Employee Referral</option>
      <option value="job_board">Job Board</option>
      <option value="other">Other</option>
    </select>
  </div>

  <div class="field">
    <label for="cover_letter_field">Cover Letter</label>
    <textarea name="job_application[cover_letter]" aria-label="Cover Letter" placeholder="Tell us why you're a great fit..."></textarea>
  </div>

  <div class="field">
    <label for="linkedin_field">LinkedIn Profile</label>
    <input type="url" name="job_application[linkedin]" aria-label="LinkedIn Profile" placeholder="https://linkedin.com/in/...">
  </div>

  <div class="field">
    <label for="website_field">Website / Portfolio</label>
    <input type="url" name="job_application[website]" aria-label="Website / Portfolio" placeholder="https://...">
  </div>

  <div class="actions">
    <button type="button" name="continue" aria-label="Continue">Continue</button>
    <button type="submit" name="submit_app" aria-label="Submit Application">Submit Application</button>
  </div>

  <div class="errors">
    <span class="error">This field is required</span>
    <div class="field-error">Invalid email address</div>
  </div>
</form>
</body>
</html>"""


def test_greenhouse_fixture_extracts_name_email_phone():
    """Contract: Greenhouse form fields with job_application[...] names
    are parsed into ObservedField entries with correct kinds, labels,
    and name-based selectors (no id fallback)."""
    obs = observe_html(GREENHOUSE_FORM, url="https://boards.greenhouse.io/acme/jobs/123")
    by_name = {f.name: f for f in obs.fields}

    assert by_name["job_application[first_name]"].kind == "text"
    assert by_name["job_application[first_name]"].label == "First Name"
    assert by_name["job_application[first_name]"].selector == "input[name='job_application[first_name]']"
    assert by_name["job_application[first_name]"].required is True

    assert by_name["job_application[last_name]"].kind == "text"
    assert by_name["job_application[last_name]"].label == "Last Name"
    assert by_name["job_application[last_name]"].selector == "input[name='job_application[last_name]']"
    assert by_name["job_application[last_name]"].required is True

    assert by_name["job_application[email]"].kind == "email"
    assert by_name["job_application[email]"].label == "you@example.com"  # placeholder wins over name
    assert by_name["job_application[email]"].selector == "input[name='job_application[email]']"
    assert by_name["job_application[email]"].required is True

    assert by_name["job_application[phone]"].kind == "tel"
    assert by_name["job_application[phone]"].label == "Phone"
    assert by_name["job_application[phone]"].selector == "input[name='job_application[phone]']"
    assert by_name["job_application[phone]"].required is True


def test_greenhouse_fixture_resume_file_field():
    """Contract: Greenhouse resume upload is captured as kind='file'
    with the correct name-based selector."""
    obs = observe_html(GREENHOUSE_FORM)
    by_name = {f.name: f for f in obs.fields}

    resume = by_name["job_application[resume]"]
    assert resume.kind == "file"
    assert resume.label == "Resume/CV"
    assert resume.selector == "input[name='job_application[resume]']"
    assert resume.required is True


def test_greenhouse_fixture_select_with_options():
    """Contract: Greenhouse <select> captures option values (not text)
    and uses aria-label for the field label."""
    obs = observe_html(GREENHOUSE_FORM)
    by_name = {f.name: f for f in obs.fields}

    source = by_name["job_application[source]"]
    assert source.kind == "select"
    assert source.label == "How did you hear about us?"
    assert source.selector == "select[name='job_application[source]']"
    assert source.required is True
    # Parser only captures option value attributes, not text.
    # Empty value="" is skipped by the parser (line 98: "if value:").
    assert source.options == ("linkedin", "referral", "job_board", "other")


def test_greenhouse_fixture_textarea():
    """Contract: Greenhouse <textarea> is captured with correct kind and label."""
    obs = observe_html(GREENHOUSE_FORM)
    by_name = {f.name: f for f in obs.fields}

    cover = by_name["job_application[cover_letter]"]
    assert cover.kind == "textarea"
    assert cover.label == "Cover Letter"
    assert cover.selector == "textarea[name='job_application[cover_letter]']"
    assert cover.required is False


def test_greenhouse_fixture_url_fields():
    """Contract: Greenhouse LinkedIn and Website URL fields are captured."""
    obs = observe_html(GREENHOUSE_FORM)
    by_name = {f.name: f for f in obs.fields}

    linkedin = by_name["job_application[linkedin]"]
    assert linkedin.kind == "url"
    assert linkedin.label == "LinkedIn Profile"
    assert linkedin.required is False

    website = by_name["job_application[website]"]
    assert website.kind == "url"
    assert website.label == "Website / Portfolio"
    assert website.required is False


def test_greenhouse_fixture_skips_hidden_inputs():
    """Contract: Greenhouse authenticity_token hidden input is excluded from fields."""
    obs = observe_html(GREENHOUSE_FORM)
    names = {f.name for f in obs.fields}
    assert "authenticity_token" not in names


def test_greenhouse_fixture_submit_button_unsafe():
    """Contract: Greenhouse Submit Application button is observed and marked unsafe."""
    obs = observe_html(GREENHOUSE_FORM)
    by_text = {b.text: b for b in obs.buttons}

    assert "Submit Application" in by_text
    assert by_text["Submit Application"].safe is False
    assert by_text["Submit Application"].selector == "button[name='submit_app']"


def test_greenhouse_fixture_continue_button_safe():
    """Contract: Greenhouse Continue navigation button is observed and marked safe."""
    obs = observe_html(GREENHOUSE_FORM)
    by_text = {b.text: b for b in obs.buttons}

    assert "Continue" in by_text
    assert by_text["Continue"].safe is True
    assert by_text["Continue"].selector == "button[name='continue']"


def test_greenhouse_fixture_errors_extracted():
    """Contract: Greenhouse error text is extracted from the page."""
    obs = observe_html(GREENHOUSE_FORM)
    assert any("required" in e.lower() for e in obs.errors)
    assert any("invalid" in e.lower() for e in obs.errors)


def test_greenhouse_fixture_preserves_url_and_title():
    """Contract: Greenhouse observation preserves the application URL and page title."""
    obs = observe_html(GREENHOUSE_FORM,
                       url="https://boards.greenhouse.io/acme/jobs/123/apply",
                       title="Acme Corp — Software Engineer")
    assert obs.url == "https://boards.greenhouse.io/acme/jobs/123/apply"
    assert obs.title == "Acme Corp — Software Engineer"


# ---------------------------------------------------------------------------
# No-final-submit guard — Greenhouse-specific
# ---------------------------------------------------------------------------

def test_greenhouse_no_final_submit_guard():
    """Contract: the Greenhouse Submit Application button is unsafe and
    _safe_observed_button_selector rejects it. The FINAL_RE matches
    'Submit Application' so the guard cannot be bypassed by renaming."""
    obs = observe_html(GREENHOUSE_FORM)
    submit_button = next(b for b in obs.buttons if b.text == "Submit Application")
    assert submit_button.safe is False
    assert FINAL_RE.search(submit_button.text) is not None

    # _safe_observed_button_selector must reject the submit button's selector
    result = _safe_observed_button_selector(obs, submit_button.selector)
    assert result is None

    # The Continue button must pass the gate
    continue_button = next(b for b in obs.buttons if b.text == "Continue")
    assert continue_button.safe is True
    result = _safe_observed_button_selector(obs, continue_button.selector)
    assert result == continue_button.selector


def test_button_with_submit_like_selector_is_rejected_even_with_safe_label():
    """Contract: selector/name safety participates in the click gate; a
    misleading Continue label cannot make submit_app safe."""
    html = """<form>
      <button type="button" name="submit_app" aria-label="Continue">Continue</button>
    </form>"""
    obs = observe_html(html)
    button = obs.buttons[0]

    assert button.text == "Continue"
    assert button.selector == "button[name='submit_app']"
    assert button.safe is False
    assert _safe_observed_button_selector(obs, button.selector) is None



def test_bare_input_button_selector_is_rejected():
    """Contract: generic selector-only controls are unclickable even when the
    visible label is safe."""
    obs = observe_html("""<form><input type="button" value="Continue"></form>""")
    button = obs.buttons[0]

    assert button.text == "Continue"
    assert button.selector == "input"
    assert button.safe is False
    assert _safe_observed_button_selector(obs, button.selector) is None

def test_greenhouse_iteration_path_is_defined_for_overnight_runs():
    assert GREENHOUSE_ITERATION_PATH == (
        "discover_queued_job",
        "observe_page",
        "resolve_profile_resume_and_llm_plan",
        "execute_guarded_non_final_actions",
        "persist_run_evidence",
        "human_review_manual_submit",
    )


def test_plan_action_evidence_rejects_final_submit_and_unobserved_selectors():
    obs = observe_html(GREENHOUSE_FORM)
    plan = AutofillPlan(
        answers=(
            FieldAnswer("input[name='job_application[first_name]']", "Ada", 1.0, "profile field"),
            FieldAnswer("#not-observed", "bad", 0.99, "bad selector"),
        ),
        safe_button_selector="button[name='submit_app']",
        status="ready",
        reason="test plan",
    )

    planned, rejected = plan_action_evidence(obs, plan)

    assert planned == [
        {
            "action": "fill",
            "selector": "input[name='job_application[first_name]']",
            "status": "planned",
            "reason": "profile field",
            "kind": "text",
            "confidence": 1.0,
            "value_length": 3,
        }
    ]
    assert {action["selector"] for action in rejected} == {"#not-observed", "button[name='submit_app']"}
    assert all(action["status"] == "rejected" for action in rejected)



def test_greenhouse_adapter_maps_profile_fields_to_selectors(tmp_path: Path):
    """Contract: GreenhouseAdapter fills only explicit profile fields into the
    observed Greenhouse selectors callers will pass to the browser boundary."""
    resume_file = tmp_path / "Main_Resume.pdf"
    resume_file.write_bytes(b"%PDF-1.4\nfake")
    obs = observe_html(GREENHOUSE_FORM, url="https://boards.greenhouse.io/acme/jobs/123/apply")
    context = ApplicationContext(
        resume_text="Fake resume text",
        resume_file=resume_file,
        application_profile={
            "first_name": "Ada",
            "last_name": "Lovelace",
            "email": "ada@example.test",
            "phone": "+1 555 0100",
            "linkedin": "https://www.linkedin.com/in/ada",
            "personal_site": "https://ada.example.test",
        },
    )

    answers = GreenhouseAdapter().deterministic_answers(obs, context)
    by_selector = {answer.selector: answer for answer in answers}

    assert by_selector["input[name='job_application[first_name]']"].value == "Ada"
    assert by_selector["input[name='job_application[last_name]']"].value == "Lovelace"
    assert by_selector["input[name='job_application[email]']"].value == "ada@example.test"
    assert by_selector["input[name='job_application[phone]']"].value == "+1 555 0100"
    assert by_selector["input[name='job_application[linkedin]']"].value == "https://www.linkedin.com/in/ada"
    assert by_selector["input[name='job_application[website]']"].value == "https://ada.example.test"
    assert by_selector["input[name='job_application[resume]']"].value == str(resume_file)
    assert all(answer.confidence == 1.0 for answer in by_selector.values())


def test_greenhouse_adapter_resume_upload_answer_is_configured_file(tmp_path: Path):
    """Contract: resume upload answers come from ApplicationContext.resume_file,
    never from resume text or an LLM-suggested path."""
    configured_resume = tmp_path / "configured_resume.pdf"
    configured_resume.write_bytes(b"%PDF-1.4\nfake")
    obs = PageObservation(
        url="https://boards.greenhouse.io/acme/jobs/123/apply",
        title="Apply",
        fields=(
            ObservedField(
                kind="file",
                name="job_application[resume]",
                label="Resume/CV",
                selector="input[name='job_application[resume]']",
                required=True,
            ),
        ),
        buttons=(),
    )
    context = ApplicationContext(
        resume_text="Resume text mentions /tmp/not-the-upload.pdf",
        resume_file=configured_resume,
        application_profile={},
    )

    answers = GreenhouseAdapter().deterministic_answers(obs, context)

    assert answers == (
        FieldAnswer(
            selector="input[name='job_application[resume]']",
            value=str(configured_resume),
            confidence=1.0,
            reason="configured resume upload",
        ),
    )


def test_greenhouse_adapter_skips_sensitive_fields_even_when_profile_could_answer():
    """Contract: sensitive Greenhouse fields are omitted before profile mapping,
    so explicit identity/contact data cannot leak into SSN or password-like fields."""
    obs = PageObservation(
        url="https://boards.greenhouse.io/acme/jobs/123/apply",
        title="Apply",
        fields=(
            ObservedField(
                kind="email",
                name="job_application[email]",
                label="SSN Email",
                selector="input[name='job_application[email]']",
                required=True,
            ),
            ObservedField(
                kind="text",
                name="job_application[phone]",
                label="Password recovery phone",
                selector="input[name='job_application[phone]']",
                required=False,
            ),
            ObservedField(
                kind="text",
                name="job_application[first_name]",
                label="First Name",
                selector="input[name='job_application[first_name]']",
                required=True,
            ),
        ),
        buttons=(),
    )
    context = ApplicationContext(
        resume_text="Fake resume text",
        resume_file=None,
        application_profile={"first_name": "Ada", "email": "ada@example.test", "phone": "+1 555 0100"},
    )

    answers = GreenhouseAdapter().deterministic_answers(obs, context)

    assert answers == (
        FieldAnswer(
            selector="input[name='job_application[first_name]']",
            value="Ada",
            confidence=1.0,
            reason="profile field",
        ),
    )


def test_greenhouse_deterministic_answers_override_llm_and_submit_is_not_approved(tmp_path: Path):
    """Contract: deterministic Greenhouse answers win over LLM answers for the
    same selector, and an LLM-proposed final submit selector still fails the
    observed-button safety gate."""
    resume_file = tmp_path / "Main_Resume.pdf"
    resume_file.write_bytes(b"%PDF-1.4\nfake")
    obs = observe_html(GREENHOUSE_FORM, url="https://boards.greenhouse.io/acme/jobs/123/apply")
    context = ApplicationContext(
        resume_text="Fake resume text",
        resume_file=resume_file,
        application_profile={"first_name": "Ada", "email": "ada@example.test"},
    )
    deterministic = GreenhouseAdapter().deterministic_answers(obs, context)
    llm_plan = AutofillPlan(
        answers=(
            FieldAnswer("input[name='job_application[first_name]']", "Grace", 0.99, "llm guess"),
            FieldAnswer("textarea[name='job_application[cover_letter]']", "I am excited to apply.", 0.87, "draft"),
        ),
        safe_button_selector="button[name='submit_app']",
        status="ready",
        reason="llm ready",
    )

    merged = merge_plans(deterministic, llm_plan)
    by_selector = {answer.selector: answer for answer in merged.answers}

    assert by_selector["input[name='job_application[first_name]']"].value == "Ada"
    assert by_selector["input[name='job_application[first_name]']"].reason == "profile field"
    assert by_selector["input[name='job_application[email]']"].value == "ada@example.test"
    assert by_selector["input[name='job_application[resume]']"].value == str(resume_file)
    assert by_selector["textarea[name='job_application[cover_letter]']"].value == "I am excited to apply."
    assert merged.raw["deterministic_answer_count"] == len(deterministic)
    assert _safe_observed_button_selector(obs, merged.safe_button_selector) is None


def test_merge_plans_preserves_required_sensitive_manual_status():
    deterministic = (FieldAnswer("#email", "profile@example.com", 1.0, "profile field"),)
    llm_plan = AutofillPlan(
        status=MANUAL_STATUS,
        reason="required sensitive/manual fields need review",
        skipped_fields=("Visa Sponsorship",),
        raw={"blocking_sensitive_fields": ["Visa Sponsorship"]},
    )

    merged = merge_plans(deterministic, llm_plan)

    assert merged.status == MANUAL_STATUS
    assert merged.answers == ()
    assert merged.safe_button_selector is None
    assert merged.raw["blocking_sensitive_fields"] == ["Visa Sponsorship"]
    assert merged.raw["deterministic_answer_count"] == 1


def test_merge_plans_preserves_deterministic_profile_precedence():
    """Contract: deterministic profile/resume answers override LLM answers for
    the same selector, while LLM-only safe answers remain available."""
    deterministic = (
        FieldAnswer("#email", "profile@example.com", 1.0, "profile field"),
        FieldAnswer("#resume", "/resume/Main_Resume.pdf", 1.0, "configured resume upload"),
    )
    llm_plan = AutofillPlan(
        answers=(
            FieldAnswer("#email", "llm@example.com", 0.99, "guessed email"),
            FieldAnswer("#cover_letter", "I am interested.", 0.85, "draft text"),
        ),
        safe_button_selector="#next",
        status="ready",
        reason="llm answers ready",
        raw={"source": "test"},
    )

    merged = merge_plans(deterministic, llm_plan)
    by_selector = {answer.selector: answer for answer in merged.answers}

    assert by_selector["#email"].value == "profile@example.com"
    assert by_selector["#email"].reason == "profile field"
    assert by_selector["#resume"].value == "/resume/Main_Resume.pdf"
    assert by_selector["#cover_letter"].value == "I am interested."
    assert merged.safe_button_selector == "#next"
    assert merged.status == "ready"
    assert merged.raw["deterministic_answer_count"] == 2


def test_classify_application_site_detects_greenhouse_adapter():
    adapter = GreenhouseAdapter()

    assert classify_application_site(adapter=adapter, url="https://boards.greenhouse.io/acme/jobs/123", html="<html></html>") == "greenhouse"


def test_classify_application_site_marks_workday_unknown_ats():
    assert classify_application_site(adapter=None, url="https://acme.myworkdayjobs.com/job/123", html="<html></html>") == "unknown_ats"


def test_classify_application_site_marks_plain_company_site_in_house():
    assert classify_application_site(adapter=None, url="https://jobs.example.test/apply", html="<html></html>") == "in_house"


def test_unresolved_required_fields_lists_required_unanswered_fields():
    observation = PageObservation(
        url="https://a.test",
        title="Apply",
        fields=(
            ObservedField(kind="text", name="first_name", label="First Name", selector="#first", required=True),
            ObservedField(kind="email", name="email", label="Email", selector="#email", required=True),
            ObservedField(kind="text", name="visa", label="Visa Sponsorship", selector="#visa", required=True),
            ObservedField(kind="text", name="portfolio", label="Portfolio", selector="#portfolio", required=False),
        ),
        buttons=(),
    )

    plan = AutofillPlan(answers=(FieldAnswer(selector="#first", value="Ada", confidence=1.0, reason="profile field"),))

    assert unresolved_required_fields(observation, plan) == ("Email", "Visa Sponsorship")


def test_merge_plans_marks_unresolved_required_safe_fields_manual():
    observation = PageObservation(
        url="https://a.test",
        title="Apply",
        fields=(
            ObservedField(kind="text", name="first_name", label="First Name", selector="#first", required=True),
            ObservedField(kind="email", name="email", label="Email", selector="#email", required=True),
        ),
        buttons=(),
    )
    llm_plan = AutofillPlan(
        answers=(FieldAnswer("#first", "Ada", 1.0, "profile field"),),
        status="ready",
        reason="llm ready",
    )

    merged = merge_plans((), llm_plan, observation=observation)

    assert merged.status == MANUAL_STATUS
    assert merged.reason == "required safe fields unresolved"
    assert merged.raw["unresolved_required_fields"] == ["Email"]

# ---------------------------------------------------------------------------
# Artifact persistence — Greenhouse observation/plan JSON round-trips
# ---------------------------------------------------------------------------

def test_greenhouse_observation_artifact_roundtrip():
    """Contract: a Greenhouse PageObservation serializes to observation_json
    and round-trips through record_application_run with all fields, buttons,
    and errors intact."""
    conn = connect(":memory:")
    init_db(conn)
    # Insert a job so FK constraint is satisfied
    conn.execute(
        "INSERT INTO jobs (source, source_job_id, title, company, canonical_url, discovered_at, first_seen_at, last_seen_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        ("test", "gh-1", "Software Engineer", "Acme Corp", "https://boards.greenhouse.io/acme/jobs/123/apply", "2026-01-01T00:00:00Z", "2026-01-01T00:00:00Z", "2026-01-01T00:00:00Z"),
    )
    job_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]

    obs = observe_html(GREENHOUSE_FORM,
                       url="https://boards.greenhouse.io/acme/jobs/123/apply",
                       title="Acme Corp — Software Engineer")

    run_id = record_application_run(conn, job_id=job_id,
                                    url=obs.url, status=MANUAL_STATUS,
                                    reason="greenhouse fixture observation",
                                    observation=obs)

    row = conn.execute("SELECT observation_json FROM application_runs WHERE id = ?", (run_id,)).fetchone()
    stored = json.loads(row["observation_json"])

    assert stored["url"] == "https://boards.greenhouse.io/acme/jobs/123/apply"
    assert stored["title"] == "Acme Corp — Software Engineer"

    # All 9 fields present (first_name, last_name, email, phone, resume,
    # source, cover_letter, linkedin, website)
    assert len(stored["fields"]) == 9

    field_by_name = {f["name"]: f for f in stored["fields"]}
    assert field_by_name["job_application[first_name]"]["kind"] == "text"
    assert field_by_name["job_application[first_name]"]["required"] is True
    assert field_by_name["job_application[resume]"]["kind"] == "file"
    assert field_by_name["job_application[source]"]["kind"] == "select"
    # Empty value="" is skipped by the parser
    assert field_by_name["job_application[source]"]["options"] == ["linkedin", "referral", "job_board", "other"]
    assert field_by_name["job_application[cover_letter]"]["kind"] == "textarea"

    # Both buttons present
    assert len(stored["buttons"]) == 2
    button_by_text = {b["text"]: b for b in stored["buttons"]}
    assert button_by_text["Submit Application"]["safe"] is False
    assert button_by_text["Continue"]["safe"] is True

    # Errors present
    assert len(stored["errors"]) >= 2
    assert any("required" in e.lower() for e in stored["errors"])
    assert any("invalid" in e.lower() for e in stored["errors"])

    conn.close()


def test_greenhouse_plan_artifact_roundtrip():
    """Contract: a Greenhouse AutofillPlan serializes to plan_json and
    round-trips through record_application_run with answers, skipped_fields,
    safe_button_selector, status, and reason intact.

    plan.status='ready' is stored inside plan_json; the run status uses
    a valid DB value (MANUAL_STATUS)."""
    conn = connect(":memory:")
    init_db(conn)
    conn.execute(
        "INSERT INTO jobs (source, source_job_id, title, company, canonical_url, discovered_at, first_seen_at, last_seen_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        ("test", "gh-2", "Software Engineer", "Acme Corp", "https://boards.greenhouse.io/acme/jobs/123/apply", "2026-01-01T00:00:00Z", "2026-01-01T00:00:00Z", "2026-01-01T00:00:00Z"),
    )
    job_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]

    plan = AutofillPlan(
        answers=(
            FieldAnswer(selector="input[name='job_application[first_name]']", value="Jane", confidence=0.95, reason="profile"),
            FieldAnswer(selector="input[name='job_application[last_name]']", value="Doe", confidence=0.95, reason="profile"),
            FieldAnswer(selector="input[name='job_application[email]']", value="jane@example.com", confidence=0.95, reason="profile"),
            FieldAnswer(selector="input[name='job_application[phone]']", value="555-0100", confidence=0.90, reason="profile"),
            FieldAnswer(selector="select[name='job_application[source]']", value="linkedin", confidence=0.80, reason="profile"),
        ),
        safe_button_selector="button[name='continue']",
        status="ready",
        reason="greenhouse fields resolved from profile",
        skipped_fields=("job_application[ssn]", "job_application[visa]"),
    )

    run_id = record_application_run(conn, job_id=job_id,
                                    url="https://boards.greenhouse.io/acme/jobs/123/apply",
                                    status=MANUAL_STATUS, reason="greenhouse plan test",
                                    plan=plan)

    row = conn.execute("SELECT plan_json FROM application_runs WHERE id = ?", (run_id,)).fetchone()
    stored = json.loads(row["plan_json"])

    # plan.status='ready' is preserved inside plan_json
    assert stored["status"] == "ready"
    assert stored["reason"] == "greenhouse fields resolved from profile"
    assert stored["safe_button_selector"] == "button[name='continue']"
    assert stored["skipped_fields"] == ["job_application[ssn]", "job_application[visa]"]

    assert len(stored["answers"]) == 5
    answer_by_selector = {a["selector"]: a for a in stored["answers"]}
    assert answer_by_selector["input[name='job_application[first_name]']"]["value"] == "Jane"
    assert answer_by_selector["input[name='job_application[first_name]']"]["confidence"] == 0.95
    assert answer_by_selector["select[name='job_application[source]']"]["value"] == "linkedin"

    conn.close()


def test_plan_raw_learning_metadata_roundtrip():
    conn = connect(":memory:")
    init_db(conn)
    conn.execute(
        "INSERT INTO jobs (source, source_job_id, title, company, canonical_url, discovered_at, first_seen_at, last_seen_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        ("test", "gh-raw", "Software Engineer", "Acme Corp", "https://boards.greenhouse.io/acme/jobs/123/apply", "2026-01-01T00:00:00Z", "2026-01-01T00:00:00Z", "2026-01-01T00:00:00Z"),
    )
    job_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    plan = AutofillPlan(
        status=MANUAL_STATUS,
        reason="required safe fields unresolved",
        raw={
            "site_classification": "greenhouse",
            "ats": "greenhouse",
            "missing_required_fields": ["Email"],
            "learning_policy": "add safe repeatable answers to application_profile_json manually; never infer sensitive/legal fields",
        },
    )

    run_id = record_application_run(
        conn,
        job_id=job_id,
        url="https://boards.greenhouse.io/acme/jobs/123/apply",
        status=MANUAL_STATUS,
        reason=plan.reason,
        plan=plan,
    )
    stored = json.loads(conn.execute("SELECT plan_json FROM application_runs WHERE id = ?", (run_id,)).fetchone()["plan_json"])

    assert stored["raw"]["site_classification"] == "greenhouse"
    assert stored["raw"]["ats"] == "greenhouse"
    assert stored["raw"]["missing_required_fields"] == ["Email"]
    assert "never infer sensitive/legal fields" in stored["raw"]["learning_policy"]


def test_run_browser_autofill_uses_puppeteer_session_boundary(tmp_path: Path, monkeypatch):
    conn = _memory_db()
    resume_dir = tmp_path / "resume"
    resume_dir.mkdir()
    (resume_dir / "main_resume.txt").write_text("Resume text")
    events: list[tuple[str, object]] = []

    class FakePuppeteerSession:
        @classmethod
        async def start(cls, *, headless: bool):
            events.append(("start", headless))
            return cls()

        async def goto(self, url: str):
            events.append(("goto", url))
            return {
                "url": url,
                "title": "Apply",
                "html": "<html><body><input type='email' name='email' aria-label='Email' required></body></html>",
            }

        async def fill(self, selector: str, value: str):
            events.append(("fill", selector, value))

        async def select(self, selector: str, value: str):
            events.append(("select", selector, value))

        async def upload(self, selector: str, value: str):
            events.append(("upload", selector, value))

        async def click(self, selector: str):
            events.append(("click", selector))

        async def screenshot(self):
            events.append(("screenshot", b"png"))
            return b"png"

        async def close(self):
            events.append(("close", None))

    def fake_resolve(*args, **kwargs):
        return AutofillPlan(
            answers=(FieldAnswer("input[name='email']", "candidate@example.test", 0.95, "explicit profile"),),
            status="ready",
            reason="ready",
        )

    monkeypatch.setattr(application_mod, "_PuppeteerSession", FakePuppeteerSession)
    monkeypatch.setattr(application_mod, "resolve_with_llm", fake_resolve)

    results = asyncio.run(run_browser_autofill(conn, limit=1, resume_dir=resume_dir, artifact_dir=tmp_path / "artifacts"))

    assert results[0]["status"] == COMPLETED_STATUS
    assert ("start", True) in events
    assert ("goto", "https://a.test/apply") in events
    assert ("fill", "input[name='email']", "candidate@example.test") in events
    assert any(event[0] == "screenshot" for event in events)
    assert events[-1] == ("close", None)


def test_write_application_artifacts_persists_unique_validation_files(tmp_path: Path):
    """Contract: artifact persistence writes observation, plan, filled-state,
    and screenshot evidence into unique per-run directories."""
    obs = observe_html(
        GREENHOUSE_FORM,
        url="https://boards.greenhouse.io/acme/jobs/123/apply",
        title="Acme Corp — Software Engineer",
    )
    plan = AutofillPlan(
        answers=(FieldAnswer(selector="input[name='job_application[first_name]']", value="Jane", confidence=1.0, reason="profile"),),
        safe_button_selector="button[name='continue']",
        status="ready",
        reason="greenhouse profile fields ready",
    )
    filled_state = [{"selector": "input[name='job_application[first_name]']", "kind": "text", "action": "fill", "value_length": 4}]

    first = write_application_artifacts(tmp_path, job_id=42, observation=obs, plan=plan, filled_state=filled_state, screenshot_bytes=b"png-one")
    second = write_application_artifacts(tmp_path, job_id=42, observation=obs, plan=plan, filled_state=filled_state, screenshot_bytes=b"png-two")
    assert first.keys() == {"observation_json", "plan_json", "actions_json", "filled_state_json", "screenshot_png"}
    assert second.keys() == first.keys()
    assert Path(first["observation_json"]).parent != Path(second["observation_json"]).parent

    observation = json.loads(Path(first["observation_json"]).read_text())
    stored_actions = json.loads(Path(first["actions_json"]).read_text())
    stored_plan = json.loads(Path(first["plan_json"]).read_text())
    stored_filled_state = json.loads(Path(first["filled_state_json"]).read_text())

    assert observation["url"] == "https://boards.greenhouse.io/acme/jobs/123/apply"
    assert stored_actions["executed"] == filled_state
    assert stored_plan["reason"] == "greenhouse profile fields ready"
    assert stored_filled_state == filled_state
    assert Path(first["screenshot_png"]).read_bytes() == b"png-one"
    assert Path(second["screenshot_png"]).read_bytes() == b"png-two"


def test_write_application_artifacts_persists_job_description_text(tmp_path: Path):
    obs = PageObservation(url="https://a.test", title="Apply", fields=(), buttons=())
    plan = AutofillPlan(status=MANUAL_STATUS, reason="manual")

    artifacts = write_application_artifacts(
        tmp_path,
        job_id=7,
        observation=obs,
        plan=plan,
        job_description="Build local ingestion tooling.",
    )

    assert Path(artifacts["job_description_txt"]).read_text() == "Build local ingestion tooling."


def test_write_application_artifacts_disabled_without_directory():
    obs = PageObservation(url="https://a.test", title="Apply", fields=(), buttons=())
    plan = AutofillPlan(status=MANUAL_STATUS, reason="manual")

    assert write_application_artifacts(None, job_id=1, observation=obs, plan=plan) == {}
