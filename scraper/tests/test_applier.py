from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

import pytest
from playwright.sync_api import Error as PlaywrightError, sync_playwright

from job_scraper.applier import ApplyResult, BrowserApplyOutcome, PlaywrightBrowserApplier, apply_to_job
from job_scraper.cli import main
from job_scraper.resume import ResumeProfile, load_resume_profile
from job_scraper.storage import JobStorage


def test_record_application_attempt_persists_fields_and_upload_flag(tmp_path: Path) -> None:
    storage = JobStorage(tmp_path / "jobs.sqlite3")
    storage.upsert_job(_job("job-1"))

    attempt = storage.record_application_attempt(
        "job-1",
        target_url="https://acme.example/jobs/123",
        status="prepared",
        submitted=False,
        message="Filled application form without submitting",
        fields_filled=("email",),
        resume_uploaded=True,
    )

    assert attempt.theirstack_id == "job-1"
    assert attempt.target_url == "https://acme.example/jobs/123"
    assert attempt.status == "prepared"
    assert attempt.submitted == 0
    assert attempt.message == "Filled application form without submitting"
    assert attempt.fields_filled_json == '["email"]'
    assert attempt.resume_uploaded == 1

    with sqlite3.connect(storage.db_path) as connection:
        row = connection.execute(
            "SELECT fields_filled_json, resume_uploaded FROM application_attempts WHERE id = ?",
            (attempt.id,),
        ).fetchone()
    assert row == ('["email"]', 1)


def test_apply_to_job_submitted_path_records_attempt_and_marks_applied(tmp_path: Path) -> None:
    storage = JobStorage(tmp_path / "jobs.sqlite3")
    storage.upsert_job(_job("job-1"))
    profile_path = _write_profile(tmp_path)
    resume_path = tmp_path / "resume.pdf"
    resume_path.write_text("resume", encoding="utf-8")
    storage.update_application("job-1", status="tailored", resume_path=str(resume_path))
    browser = FakeBrowser(
        BrowserApplyOutcome(
            target_url="https://acme.example/jobs/123",
            status="submitted",
            submitted=True,
            message="Application submitted",
            fields_filled=("email", "resume"),
            resume_uploaded=True,
        )
    )

    result = apply_to_job(
        storage,
        job_id="job-1",
        profile_path=profile_path,
        submit=True,
        browser=browser,
    )

    assert result.application.status == "applied"
    assert result.application.applied_at == date.today().isoformat()
    assert result.application.resume_path == str(resume_path)
    attempts = storage.list_application_attempts("job-1")
    assert len(attempts) == 1
    assert attempts[0].status == "submitted"
    assert attempts[0].submitted == 1
    assert browser.seen_target_url == "https://acme.example/jobs/123"
    assert browser.seen_profile is not None
    assert browser.seen_profile.contact.email == "candidate@example.com"
    assert browser.seen_resume_path == resume_path
    assert browser.seen_submit is True


def test_apply_to_job_rejects_missing_job(tmp_path: Path) -> None:
    storage = JobStorage(tmp_path / "jobs.sqlite3")
    profile_path = _write_profile(tmp_path)

    with pytest.raises(ValueError, match="Unknown job id: missing"):
        apply_to_job(storage, job_id="missing", profile_path=profile_path, resume_path=tmp_path / "resume.pdf")


def test_apply_to_job_rejects_missing_url(tmp_path: Path) -> None:
    storage = JobStorage(tmp_path / "jobs.sqlite3")
    storage.upsert_job(_job("job-1", url=None, final_url=None))
    profile_path = _write_profile(tmp_path)
    resume_path = tmp_path / "resume.pdf"
    resume_path.write_text("resume", encoding="utf-8")

    with pytest.raises(ValueError, match="Job has no application URL: job-1"):
        apply_to_job(storage, job_id="job-1", profile_path=profile_path, resume_path=resume_path)


def test_apply_to_job_requires_resume_path(tmp_path: Path) -> None:
    storage = JobStorage(tmp_path / "jobs.sqlite3")
    storage.upsert_job(_job("job-1"))
    profile_path = _write_profile(tmp_path)

    with pytest.raises(ValueError, match="Application has no resume_path; run prepare-application or pass --resume-path"):
        apply_to_job(storage, job_id="job-1", profile_path=profile_path)


def test_apply_to_job_requires_profile_email(tmp_path: Path) -> None:
    storage = JobStorage(tmp_path / "jobs.sqlite3")
    storage.upsert_job(_job("job-1"))
    profile_path = _write_profile(tmp_path, email=None)
    resume_path = tmp_path / "resume.pdf"
    resume_path.write_text("resume", encoding="utf-8")

    with pytest.raises(ValueError, match="Application profile requires contact.email"):
        apply_to_job(storage, job_id="job-1", profile_path=profile_path, resume_path=resume_path)


def test_cli_apply_prints_attempt_result(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    db_path = tmp_path / "jobs.sqlite3"
    monkeypatch.setenv("JOB_SCRAPER_DB_PATH", str(db_path))
    storage = JobStorage(db_path)
    storage.upsert_job(_job("job-1"))
    profile_path = _write_profile(tmp_path)
    resume_path = tmp_path / "resume.pdf"
    resume_path.write_text("resume", encoding="utf-8")

    def fake_apply_to_job(storage_arg: JobStorage, **kwargs: Any) -> ApplyResult:
        assert storage_arg.db_path == db_path
        assert kwargs["job_id"] == "job-1"
        assert kwargs["profile_path"] == profile_path
        assert kwargs["resume_path"] == resume_path
        assert kwargs["submit"] is True
        assert kwargs["headless"] is True
        assert kwargs["timeout_ms"] == 1000
        application = storage_arg.update_application("job-1", status="applied", resume_path=str(resume_path))
        attempt = storage_arg.record_application_attempt(
            "job-1",
            target_url="https://acme.example/jobs/123",
            status="submitted",
            submitted=True,
            message="Application submitted",
            fields_filled=("email",),
            resume_uploaded=True,
        )
        return ApplyResult(application=application, attempt=attempt)

    monkeypatch.setattr("job_scraper.cli.apply_to_job", fake_apply_to_job)

    exit_code = main(
        [
            "apply",
            "--job-id",
            "job-1",
            "--profile",
            str(profile_path),
            "--resume-path",
            str(resume_path),
            "--submit",
            "--headless",
            "--timeout-ms",
            "1000",
        ]
    )

    assert exit_code == 0
    assert capsys.readouterr().out.strip() == "submitted\tapplied\thttps://acme.example/jobs/123\tApplication submitted"


def test_playwright_browser_applier_fills_local_contact_form(tmp_path: Path) -> None:
    _skip_if_chromium_unavailable()
    profile = load_resume_profile(_write_profile(tmp_path))
    resume_path = tmp_path / "resume.pdf"
    resume_path.write_text("resume", encoding="utf-8")
    html_path = tmp_path / "application.html"
    html_path.write_text(
        """
<!doctype html>
<html>
  <body>
    <form onsubmit="document.body.insertAdjacentHTML('beforeend', '<p>Application submitted</p>'); return false;">
      <label>First Name <input name="first_name" required></label>
      <label>Last Name <input name="last_name" required></label>
      <label>Email <input type="email" name="email" required></label>
      <label>Phone <input type="tel" name="phone"></label>
      <label>LinkedIn <input name="linkedin"></label>
      <label>Resume <input type="file" name="resume" required></label>
      <button type="submit">Submit Application</button>
    </form>
  </body>
</html>
""".strip(),
        encoding="utf-8",
    )

    outcome = PlaywrightBrowserApplier(headless=True, timeout_ms=5000).apply(
        target_url=html_path.as_uri(),
        profile=profile,
        resume_path=resume_path,
        submit=True,
    )

    assert outcome.status == "submitted"
    assert outcome.submitted is True
    assert outcome.resume_uploaded is True
    assert "email" in outcome.fields_filled


@dataclass
class FakeBrowser:
    outcome: BrowserApplyOutcome
    seen_target_url: str | None = None
    seen_profile: ResumeProfile | None = None
    seen_resume_path: Path | None = None
    seen_submit: bool | None = None

    def apply(
        self,
        *,
        target_url: str,
        profile: ResumeProfile,
        resume_path: Path,
        submit: bool,
    ) -> BrowserApplyOutcome:
        self.seen_target_url = target_url
        self.seen_profile = profile
        self.seen_resume_path = resume_path
        self.seen_submit = submit
        return self.outcome


def _skip_if_chromium_unavailable() -> None:
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            browser.close()
    except PlaywrightError as exc:
        pytest.skip(f"Playwright Chromium is unavailable: {exc}")


def _write_profile(tmp_path: Path, *, email: str | None = "candidate@example.com") -> Path:
    email_line = f"  email: {email}\n" if email is not None else ""
    profile_path = tmp_path / "profile.yaml"
    profile_path.write_text(
        f"""
name: Ada Lovelace
headline: Product engineer
contact:
{email_line}  phone: '+15551234567'
  location: San Francisco, CA
  links:
    - https://www.linkedin.com/in/ada-lovelace
    - https://ada.example
skills:
  Languages:
    - Python
sections:
  - heading: Experience
    items:
      - title: Engineer
        organization: Example Co
        dates: 2021 — Present
        bullets:
          - text: Built Python services.
            skills: [Python]
""".strip(),
        encoding="utf-8",
    )
    return profile_path


def _job(
    job_id: str,
    *,
    url: str | None = "https://www.linkedin.com/jobs/view/123",
    source_url: str | None = "https://www.linkedin.com/jobs/view/123",
    final_url: str | None = "https://acme.example/jobs/123",
    job_title: str = "Fall Software Co-op",
    company_name: str = "Acme",
    job_description: str = "Build software",
) -> dict[str, object]:
    return {
        "id": job_id,
        "job_title": job_title,
        "company_name": company_name,
        "company_domain": "acme.example",
        "job_country_code": "US",
        "remote": False,
        "date_posted": "2026-06-23",
        "discovered_at": "2026-06-23T12:00:00+00:00",
        "url": url,
        "source_url": source_url,
        "final_url": final_url,
        "job_description": job_description,
    }
