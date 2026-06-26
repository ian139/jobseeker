from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass
from pathlib import Path

from job_scraper.resume import ResumeLLM, TailoredResume, load_resume_profile, tailor_resume
from job_scraper.storage import ApplicationRecord, JobRecord, JobStorage


@dataclass(frozen=True)
class ApplicationPack:
    application: ApplicationRecord
    resume_version_id: int
    resume_path: Path
    resume: TailoredResume


def prepare_application_pack(
    storage: JobStorage,
    *,
    job_id: str,
    profile_path: Path,
    output_dir: Path,
    llm: ResumeLLM | None = None,
    notes: str = "",
) -> ApplicationPack:
    job = storage.get_job(job_id)
    if job is None:
        raise ValueError(f"Unknown job id: {job_id}")

    profile = load_resume_profile(profile_path)
    application = storage.ensure_application(job_id, notes=notes)
    merged_job = _merged_job_mapping(job)
    resume = tailor_resume(profile, merged_job, llm=llm)

    resume_path = _resume_path(output_dir, job)
    resume_path.parent.mkdir(parents=True, exist_ok=True)
    resume_path.write_text(resume.markdown, encoding="utf-8")

    resume_version_id = storage.save_resume_version(
        application_id=application.id,
        resume_markdown=resume.markdown,
        selected_bullets_json=json.dumps([asdict(bullet) for bullet in resume.selected_bullets], separators=(",", ":")),
        keywords_json=json.dumps(list(resume.keywords), separators=(",", ":")),
        llm_used=resume.llm_used,
        model=resume.model,
        output_path=str(resume_path),
    )
    updated_application = storage.update_application(job_id, status="tailored", resume_path=str(resume_path))
    return ApplicationPack(
        application=updated_application,
        resume_version_id=resume_version_id,
        resume_path=resume_path,
        resume=resume,
    )


def _merged_job_mapping(job: JobRecord) -> dict[str, object]:
    merged: dict[str, object] = dict(job.raw)
    normalized: dict[str, object | None] = {
        "title": job.title,
        "company": job.company,
        "company_domain": job.company_domain,
        "country_code": job.country_code,
        "remote": job.remote,
        "date_posted": job.date_posted,
        "discovered_at": job.discovered_at,
        "url": job.url,
        "source_url": job.source_url,
        "final_url": job.final_url,
        "role_kind": job.role_kind,
        "source": job.source,
        "description": job.description,
        "locations": job.locations or None,
        "skills": job.skills or None,
        "seniority": job.seniority,
        "employment_statuses": job.employment_statuses or None,
        "min_annual_salary_usd": job.min_annual_salary_usd,
        "max_annual_salary_usd": job.max_annual_salary_usd,
        "digest": job.digest or None,
    }
    for key, value in normalized.items():
        if value is not None and _is_empty_raw_value(merged.get(key)):
            merged[key] = value
    return merged


def _is_empty_raw_value(value: object) -> bool:
    return value is None or value == "" or value == [] or value == () or value == {}


def _resume_path(output_dir: Path, job: JobRecord) -> Path:
    company = _safe_segment(job.company, fallback="unknown-company")
    title = _safe_segment(job.title, fallback="unknown-role")
    job_id = _safe_segment(job.theirstack_id, fallback="job")
    return output_dir / f"{company}-{title}-{job_id}" / "resume.md"


def _safe_segment(value: str | None, *, fallback: str) -> str:
    if value is None or not value.strip():
        return fallback
    safe = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    if not safe:
        return fallback
    return safe[:60].strip("-") or fallback
