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
    storage.upsert_job(_job("undated-job", discovered_at=None, date_posted=None))

    jobs = storage.list_jobs(limit=10)

    assert [job.theirstack_id for job in jobs] == ["new-job", "old-job", "undated-job"]
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
    assert "Resume Review Report Generator" in prompt
    assert "Agent TODO Checklist" in prompt
    assert "Do Not Touch" in prompt


def test_webui_scores_resume_against_market_and_downloads_prompt(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JOB_SCRAPER_DB_PATH", str(tmp_path / "jobs.sqlite3"))
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    settings = AppSettings(_env_file=None)
    storage = JobStorage(settings.job_scraper_db_path)
    storage.upsert_job(_job("job-1", job_title="Clinical Data Engineer", company_name="Acme Health"))
    storage.upsert_job(
        _job(
            "job-2",
            job_title="Retail Operations Analyst",
            company_name="ShopCo",
            discovered_at="2026-06-22T12:00:00+00:00",
            job_description="Manage store staffing, retail shifts, and vendor escalations.",
        )
    )
    app = create_app(settings)
    client = TestClient(app)

    response = client.get("/")

    assert response.status_code == 200
    assert "Market Signal Console" in response.text
    assert 'id="input-strip"' in response.text
    assert 'name="target_roles"' in response.text
    assert 'name="target_industries"' in response.text
    assert 'id="resume-file"' in response.text
    assert "Clinical Data Engineer" in response.text
    assert 'href="/jobs/job-1/prompt"' in response.text
    assert 'href="/jobs/job-1"' in response.text

    latex = b"""
Ada Candidate
\\section{Experience}
Built Python services for healthcare analytics teams.
\\section{Skills}
Python, SQL, FastAPI
"""
    response = client.post(
        "/matches",
        data={
            "target_roles": "Data Engineer",
            "target_industries": "Healthcare",
            "keywords": "Python, analytics",
        },
        files={"resume_file": ("resume.tex", latex, "application/x-tex")},
    )

    assert response.status_code == 200
    assert "Top scored matches" in response.text
    assert 'id="category-tabs"' in response.text
    assert 'id="job-table-container"' in response.text
    assert 'id="detail-panel"' in response.text
    assert 'data-job-id="job-1"' in response.text
    assert "Best-supported US regions" in response.text
    assert "Resume strengths" in response.text
    assert "Download Markdown review report" in response.text
    assert "Copy Markdown report" in response.text
    assert "/jobs/job-1/improvement-report" in response.text
    assert "Category fit" in response.text
    assert "Key strengths" in response.text
    assert "Missing requirements" in response.text
    assert "Relevant resume evidence" in response.text
    assert "Why this rank" in response.text
    assert 'data-detail-target="job-job-1"' in response.text
    assert "Original listing:" in response.text
    assert "Application link:" in response.text
    assert "https://acme.example/jobs/123" in response.text
    assert "Build Python services for healthcare analytics." in response.text
    assert "Requirements" in response.text
    assert "Healthcare analytics" in response.text
    assert "const panel = document.getElementById(&quot;detail-panel&quot;);" not in response.text
    assert 'document.getElementById("detail-panel")' in response.text
    assert response.text.count('name="resume_text"') == 1
    download = client.post(
        "/jobs/job-1/improvement-report",
        data={
            "resume_filename": "resume.tex",
            "resume_kind": "latex",
            "resume_text": "Ada Candidate\nExperience\nBuilt Python services for healthcare analytics teams.\nSkills\nPython SQL FastAPI",
            "target_roles": "Data Engineer",
            "target_industries": "Healthcare",
            "keywords": "Python, analytics",
        },
    )

    assert download.status_code == 200
    assert download.headers["content-disposition"].startswith("attachment;")
    assert download.headers["content-type"].startswith("text/markdown")
    assert download.headers["content-disposition"].endswith('resume-review-report.md"')
    assert download.text.startswith("# Resume Review Report — Clinical Data Engineer")
    assert "Resume Review Report Generator" not in download.text
    assert "Clinical Data Engineer" in download.text
    assert "LaTeX" in download.text
    assert "## Executive Summary" in download.text
    assert "## Overall Evaluation" in download.text
    assert "## Major Strengths" in download.text
    assert "## Major Weaknesses" in download.text
    assert "## Missing Keywords" in download.text
    assert "## Missing Experiences" in download.text
    assert "## Formatting Feedback" in download.text
    assert "## Structure Feedback" in download.text
    assert "## Job-Specific Tailoring Advice" in download.text
    assert "## Rewritten Bullet Suggestions" in download.text
    assert "## Project Recommendations" in download.text
    assert "## Content Prioritization Recommendations" in download.text
    assert "## Risks and Warnings" in download.text
    assert "## KEEP" in download.text
    assert "## CHANGE" in download.text
    assert "## ADD" in download.text
    assert "## REMOVE" in download.text
    assert "## DO NOT TOUCH" in download.text
    assert "## Agent-Friendly Implementation Checklist" in download.text
    assert "Built Python services for healthcare analytics teams." in download.text



def test_job_detail_route_shows_complete_listing_context(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JOB_SCRAPER_DB_PATH", str(tmp_path / "jobs.sqlite3"))
    settings = AppSettings(_env_file=None)
    storage = JobStorage(settings.job_scraper_db_path)
    storage.upsert_job(_job("job-1", job_title="Clinical Data Engineer", company_name="Acme Health"))
    app = create_app(settings)
    client = TestClient(app)

    response = client.get("/jobs/job-1")

    assert response.status_code == 200
    assert "Clinical Data Engineer" in response.text
    assert "Acme Health" in response.text
    assert "US, Remote" in response.text
    assert "Score / match:" in response.text
    assert "Not scored in this view" in response.text
    assert "Original listing:" in response.text
    assert "https://www.linkedin.com/jobs/view/123" in response.text
    assert "Application link:" in response.text
    assert "https://acme.example/jobs/123" in response.text
    assert "Build Python services for healthcare analytics." in response.text
    assert "Requirements" in response.text
    assert "Healthcare analytics" in response.text
    assert 'target="_blank" rel="noopener noreferrer"' in response.text
    assert 'href="/jobs/job-1/prompt"' in response.text


def test_webui_handles_matches_refresh_and_favicon(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JOB_SCRAPER_DB_PATH", str(tmp_path / "jobs.sqlite3"))
    settings = AppSettings(_env_file=None)
    app = create_app(settings)
    client = TestClient(app, follow_redirects=False)

    matches = client.get("/matches")
    favicon = client.get("/favicon.ico")
    legacy_prompt = client.post("/jobs/job-1/improvement-prompt")

    assert matches.status_code == 303
    assert matches.headers["location"] == "/"
    assert favicon.status_code == 204
    assert legacy_prompt.status_code == 307
    assert legacy_prompt.headers["location"] == "/jobs/job-1/improvement-report"

def _job(
    job_id: str,
    *,
    discovered_at: str | None = "2026-06-23T12:00:00+00:00",
    date_posted: str | None = "2026-06-23",
    job_title: str = "Software Engineer",
    company_name: str = "Acme",
    job_description: str = "Build Python services for healthcare analytics.",
) -> dict[str, object]:
    return {
        "id": job_id,
        "job_title": job_title,
        "company_name": company_name,
        "company_domain": "acme.example",
        "job_country_code": "US",
        "remote": True,
        "date_posted": date_posted,
        "discovered_at": discovered_at,
        "url": "https://www.linkedin.com/jobs/view/123",
        "source_url": "https://www.linkedin.com/jobs/view/123",
        "final_url": "https://acme.example/jobs/123",
        "job_description": job_description,
        "requirements": ["Python", "Healthcare analytics", "Stakeholder collaboration"],
    }
