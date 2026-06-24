from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from job_scraper.config import AppSettings
from job_scraper.resume_uploads import (
    ResumeUploadError,
    UploadedResumeAnalysis,
    analyze_resume_upload,
    build_tailored_resume_prompt,
)
from job_scraper.storage import JobRecord, JobStorage
from job_scraper.web import create_app


def test_storage_lists_jobs_newest_first(tmp_path: Path) -> None:
    storage = JobStorage(tmp_path / "jobs.sqlite3")
    storage.upsert_job(_job("old-job", discovered_at="2026-06-20T12:00:00+00:00"))
    storage.upsert_job(_job("new-job", discovered_at="2026-06-23T12:00:00+00:00"))

    jobs = storage.list_jobs(limit=10)

    assert [job.theirstack_id for job in jobs] == ["new-job", "old-job"]
    assert all(isinstance(job, JobRecord) for job in jobs)
    with pytest.raises(ValueError, match="Job list limit must be at least 1"):
        storage.list_jobs(limit=0)


def test_analyze_latex_resume_extracts_facts() -> None:
    latex = b"""
Ada Candidate
\\section{Experience}
Built Python services for healthcare teams.
\\section{Skills}
Python, SQL, FastAPI
"""

    analysis = analyze_resume_upload("resume.tex", latex)

    assert analysis.kind == "latex"
    assert "Experience" in analysis.text
    assert "Skills" in analysis.text
    assert "Python" in analysis.text
    assert "Detected sections" in analysis.facts_markdown


def test_analyze_pdf_resume_uses_pypdf(monkeypatch: pytest.MonkeyPatch) -> None:
    class FakePage:
        def extract_text(self) -> str:
            return "PDF Candidate\nExperience\nPython"

    class FakeReader:
        def __init__(self, _content: object) -> None:
            self.pages = [FakePage()]

    monkeypatch.setattr("job_scraper.resume_uploads.PdfReader", FakeReader)

    analysis = analyze_resume_upload("resume.pdf", b"%PDF fake")

    assert analysis.kind == "pdf"
    assert "PDF Candidate" in analysis.text
    assert "Python" in analysis.text


def test_build_tailored_resume_prompt_requires_industry_and_includes_job_resume(tmp_path: Path) -> None:
    storage = JobStorage(tmp_path / "jobs.sqlite3")
    storage.upsert_job(_job("job-1", job_title="Healthcare Data Engineer", company_name="Acme Health"))
    job = storage.get_job("job-1")
    assert job is not None
    analysis = UploadedResumeAnalysis(
        filename="resume.md",
        kind="text",
        text="Candidate\nExperience\nBuilt Python services",
        facts_markdown="**Source:** resume.md",
    )

    with pytest.raises(ResumeUploadError, match="Target industry is required"):
        build_tailored_resume_prompt(job=job, industry="", analysis=analysis)

    prompt = build_tailored_resume_prompt(job=job, industry="Healthcare", analysis=analysis)

    assert "Healthcare" in prompt
    assert "Healthcare Data Engineer" in prompt
    assert "Acme Health" in prompt
    assert "Python services" in prompt


def test_webui_lists_jobs_and_generates_prompt_from_latex_upload(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JOB_SCRAPER_DB_PATH", str(tmp_path / "jobs.sqlite3"))
    settings = AppSettings(_env_file=None)
    storage = JobStorage(settings.job_scraper_db_path)
    storage.upsert_job(_job("job-1", job_title="Clinical Data Engineer", company_name="Acme Health"))
    app = create_app(settings)
    client = TestClient(app)

    response = client.get("/")

    assert response.status_code == 200
    assert "Clinical Data Engineer" in response.text
    assert "Create tailored prompt" in response.text

    latex = b"""
Ada Candidate
\\section{Experience}
Built Python services.
\\section{Skills}
Python
"""
    response = client.post(
        "/jobs/job-1/prompt",
        data={"industry": "Healthcare"},
        files={"resume_file": ("resume.tex", latex, "application/x-tex")},
    )

    assert response.status_code == 200
    assert "Tailored Resume Prompt" in response.text
    assert "Healthcare" in response.text
    assert "Clinical Data Engineer" in response.text
    assert "<textarea" in response.text
    assert "readonly" in response.text


def _job(
    job_id: str,
    *,
    discovered_at: str = "2026-06-23T12:00:00+00:00",
    job_title: str = "Software Engineer",
    company_name: str = "Acme",
) -> dict[str, object]:
    return {
        "id": job_id,
        "job_title": job_title,
        "company_name": company_name,
        "company_domain": "acme.example",
        "job_country_code": "US",
        "remote": True,
        "date_posted": "2026-06-23",
        "discovered_at": discovered_at,
        "url": "https://www.linkedin.com/jobs/view/123",
        "source_url": "https://www.linkedin.com/jobs/view/123",
        "final_url": "https://acme.example/jobs/123",
        "job_description": "Build Python services for healthcare analytics.",
    }
