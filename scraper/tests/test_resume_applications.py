from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any, Mapping, Sequence

import pytest

from job_scraper.applications import prepare_application_pack
from job_scraper.cli import main
from job_scraper.resume import ResumeBullet, ResumeItem, ResumeProfile, ResumeSection, SelectedBullet, tailor_resume
from job_scraper.storage import JobStorage


class FakeResumeLLM:
    model_name = "fake-model"

    def rewrite(
        self,
        *,
        draft_markdown: str,
        job: Mapping[str, Any],
        selected_bullets: Sequence[SelectedBullet],
    ) -> str:
        return "# Candidate\n\n## Summary\n- LLM sentinel rewrite\n"


def test_deterministic_resume_selects_job_relevant_points() -> None:
    profile = ResumeProfile(
        name="Candidate",
        sections=[
            ResumeSection(
                heading="Experience",
                items=[
                    ResumeItem(
                        title="Engineer",
                        organization="Acme",
                        dates="2021 — Present",
                        bullets=[
                            ResumeBullet(text="Built Python services.", skills=["python"]),
                            ResumeBullet(text="Delivered frontend tools.", tags=["frontend"]),
                            ResumeBullet(text="Closed sales pipeline.", tags=["sales"]),
                        ],
                    )
                ],
            )
        ],
    )
    job = {
        "job_title": "Frontend Python Engineer",
        "job_description": "Build Python services and frontend tooling.",
    }

    resume = tailor_resume(profile, job, max_bullets_per_item=2)

    assert "Built Python services." in resume.markdown
    assert "Delivered frontend tools." in resume.markdown
    assert "Closed sales pipeline." not in resume.markdown


def test_prepare_application_pack_creates_crm_row_resume_file_and_version(tmp_path: Path) -> None:
    storage = JobStorage(tmp_path / "jobs.sqlite3")
    storage.upsert_job(
        _job(
            "job-1",
            job_title="Frontend Python Engineer",
            company_name="Acme",
            job_description="Build Python services and frontend tools",
        )
    )
    profile_path = _write_profile(tmp_path)

    pack = prepare_application_pack(
        storage,
        job_id="job-1",
        profile_path=profile_path,
        output_dir=tmp_path / "packs",
    )

    assert pack.application.status == "tailored"
    assert pack.resume_path.name == "resume.md"
    assert pack.resume_path.exists()
    assert pack.application.resume_path == str(pack.resume_path)
    assert "Built Python services." in pack.resume_path.read_text(encoding="utf-8")
    with sqlite3.connect(tmp_path / "jobs.sqlite3") as connection:
        rows = connection.execute(
            "SELECT resume_markdown, llm_used FROM resume_versions WHERE application_id = ?",
            (pack.application.id,),
        ).fetchall()
    assert len(rows) == 1
    assert "Built Python services." in rows[0][0]
    assert rows[0][1] == 0


def test_prepare_application_pack_uses_llm_rewrite_when_available(tmp_path: Path) -> None:
    storage = JobStorage(tmp_path / "jobs.sqlite3")
    storage.upsert_job(
        _job(
            "job-1",
            job_title="Frontend Python Engineer",
            company_name="Acme",
            job_description="Build Python services and frontend tools",
        )
    )
    profile_path = _write_profile(tmp_path)

    pack = prepare_application_pack(
        storage,
        job_id="job-1",
        profile_path=profile_path,
        output_dir=tmp_path / "packs",
        llm=FakeResumeLLM(),
    )

    assert pack.resume.llm_used is True
    assert "LLM sentinel rewrite" in pack.resume_path.read_text(encoding="utf-8")
    with sqlite3.connect(tmp_path / "jobs.sqlite3") as connection:
        row = connection.execute("SELECT resume_markdown, llm_used FROM resume_versions").fetchone()
    assert row is not None
    assert "LLM sentinel rewrite" in row[0]
    assert row[1] == 1


def test_storage_rejects_unknown_job_and_invalid_status(tmp_path: Path) -> None:
    storage = JobStorage(tmp_path / "jobs.sqlite3")
    with pytest.raises(ValueError, match="Unknown job id: missing"):
        storage.ensure_application("missing")

    storage.upsert_job(_job("job-1"))
    with pytest.raises(ValueError, match="Invalid application status: bad"):
        storage.update_application("job-1", status="bad")  # type: ignore[arg-type]


def test_cli_list_applications_empty_database(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setenv("JOB_SCRAPER_DB_PATH", str(tmp_path / "jobs.sqlite3"))

    code = main(["list-applications"])

    captured = capsys.readouterr()
    assert code == 0
    assert "No applications found" in captured.out


def _write_profile(tmp_path: Path) -> Path:
    profile_path = tmp_path / "profile.yaml"
    profile_path.write_text(
        """
name: Candidate
headline: Product engineer
contact:
  email: candidate@example.com
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
          - text: Delivered frontend tools.
            tags: [frontend]
          - text: Closed sales pipeline.
            tags: [sales]
""".strip(),
        encoding="utf-8",
    )
    return profile_path


def _job(
    job_id: str,
    *,
    discovered_at: str = "2026-06-23T12:00:00+00:00",
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
        "discovered_at": discovered_at,
        "url": "https://www.linkedin.com/jobs/view/123",
        "source_url": "https://www.linkedin.com/jobs/view/123",
        "final_url": "https://acme.example/jobs/123",
        "job_description": job_description,
    }
