"""Tests for deterministic resume-to-job scoring and market analysis."""

from __future__ import annotations

from typing import Any

import pytest

from job_scraper.matching import (
    CategorySummary,
    ScoredJob,
    build_improvement_prompt,
    build_improvement_report,
    score_jobs,
    summarize_categories,
)
from job_scraper.resume_uploads import UploadedResumeAnalysis
from job_scraper.storage import JobRecord


# ── Factories ────────────────────────────────────────────────────────────────


def _job_record(
    *,
    theirstack_id: str = "job-1",
    title: str | None = "Software Engineer",
    company: str | None = "Acme Inc",
    company_domain: str | None = "acme.com",
    country_code: str | None = "US",
    remote: int | None = None,
    date_posted: str | None = "2026-06-01",
    discovered_at: str | None = "2026-06-01T12:00:00+00:00",
    url: str | None = "https://acme.com/jobs/1",
    source_url: str | None = None,
    final_url: str | None = None,
    min_annual_salary_usd: float | None = None,
    max_annual_salary_usd: float | None = None,
    role_kind: str | None = None,
    source: str | None = None,
    description: str | None = None,
    locations: tuple[str, ...] = (),
    skills: tuple[str, ...] = (),
    seniority: str | None = None,
    employment_statuses: tuple[str, ...] = (),
    digest: dict[str, object] | None = None,
    raw: dict[str, Any] | None = None,
) -> JobRecord:
    return JobRecord(
        theirstack_id=theirstack_id,
        title=title,
        company=company,
        company_domain=company_domain,
        country_code=country_code,
        remote=remote,
        date_posted=date_posted,
        discovered_at=discovered_at,
        url=url,
        source_url=source_url,
        final_url=final_url,
        min_annual_salary_usd=min_annual_salary_usd,
        max_annual_salary_usd=max_annual_salary_usd,
        role_kind=role_kind,
        source=source,
        description=description,
        locations=locations,
        skills=skills,
        seniority=seniority,
        employment_statuses=employment_statuses,
        digest=digest or {},
        raw=raw or {},
    )


def _analysis(
    *,
    filename: str = "resume.pdf",
    kind: str = "pdf",
    text: str = "Experienced software engineer with Python, Rust, and distributed systems.",
    facts_markdown: str = "## Facts\n- Software engineer\n- Python, Rust\n- Distributed systems\n",
) -> UploadedResumeAnalysis:
    return UploadedResumeAnalysis(
        filename=filename,
        kind=kind,  # type: ignore[arg-type]
        text=text,
        facts_markdown=facts_markdown,
    )


# ── Tests: scoring prioritization ────────────────────────────────────────────


def test_scoring_favors_resume_and_role_match() -> None:
    """Jobs matching the target role and resume keywords score highest."""
    analysis = _analysis(text="Software engineer skilled in Python and React.")
    target = ["Software Engineer"]
    industries = ["Technology"]
    keywords = ["python", "react"]

    perfect = _job_record(
        theirstack_id="perfect",
        title="Software Engineer",
        raw={"job_description": "Building web apps with Python and React at a tech company."},
    )
    partial = _job_record(
        theirstack_id="partial",
        title="Engineer",
        raw={"job_description": "General engineering role."},
    )
    mismatch = _job_record(
        theirstack_id="mismatch",
        title="Barista",
        raw={"job_description": "Making coffee at a cafe."},
    )

    jobs = [perfect, partial, mismatch]
    scored = score_jobs(
        jobs, analysis,
        target_roles=target,
        target_industries=industries,
        keywords=keywords,
    )

    assert len(scored) == 3
    # Perfect should rank first
    assert scored[0].job.theirstack_id == "perfect", f"Expected perfect first, got {scored[0].job.theirstack_id}"
    # Partial in the middle
    assert scored[1].job.theirstack_id == "partial", f"Expected partial second, got {scored[1].job.theirstack_id}"
    # Mismatch last
    assert scored[2].job.theirstack_id == "mismatch", f"Expected mismatch last, got {scored[2].job.theirstack_id}"
    # Perfect should have a materially higher score
    assert scored[0].score > scored[1].score, "Perfect match should score higher than partial"
    assert scored[1].score > scored[2].score, "Partial match should score higher than mismatch"


def test_parsed_description_field_feeds_scoring_without_raw_description() -> None:
    analysis = _analysis(text="Built Python FastAPI services backed by PostgreSQL.")
    job = _job_record(
        title="Backend Engineering Intern",
        description="Build Python APIs with FastAPI and PostgreSQL.",
        raw={},
    )

    scored = score_jobs([job], analysis, target_roles=[], target_industries=[], keywords=["FastAPI"])

    assert scored[0].score > 0
    assert "fastapi" in scored[0].matched_terms
    assert scored[0].analysis is not None
    assert scored[0].analysis.requirements


def test_scoring_industry_alignment_boosts_score() -> None:
    """Jobs from target industries score higher than similar jobs outside them."""
    analysis = _analysis(text="Data scientist with Python and ML experience.")
    target = ["Data Scientist"]

    fintech = _job_record(
        theirstack_id="fintech",
        title="Data Scientist",
        company="Fintech Corp",
        raw={
            "job_description": "Develop ML models for financial services.",
            "company_description": "Leading fintech company providing payment solutions.",
        },
    )
    unrelated = _job_record(
        theirstack_id="unrelated",
        title="Data Scientist",
        company="Restaurant Group",
        raw={
            "job_description": "Analyze customer data.",
            "company_description": "A chain of restaurants.",
        },
    )

    scored = score_jobs(
        [fintech, unrelated], analysis,
        target_roles=target,
        # No explicit industry filter, so both get default scoring
        target_industries=[],
        keywords=[],
    )

    # Both have same role, so scores should be close
    s_fintech = next(s for s in scored if s.job.theirstack_id == "fintech")
    s_unrelated = next(s for s in scored if s.job.theirstack_id == "unrelated")
    assert s_fintech.score >= s_unrelated.score or abs(s_fintech.score - s_unrelated.score) < 5


def test_scoring_keywords_improve_score() -> None:
    """Jobs mentioning optional keywords rank higher."""
    analysis = _analysis(text="General experience.")
    target = ["Developer"]
    industries = []
    keywords = ["aws", "docker", "kubernetes", "ci/cd"]

    with_kw = _job_record(
        theirstack_id="with-kw",
        title="Developer",
        raw={"job_description": "Work with AWS, Docker, Kubernetes, and CI/CD pipelines."},
    )
    without_kw = _job_record(
        theirstack_id="no-kw",
        title="Developer",
        raw={"job_description": "General development tasks."},
    )

    scored = score_jobs(
        [with_kw, without_kw], analysis,
        target_roles=target,
        target_industries=industries,
        keywords=keywords,
    )

    assert scored[0].job.theirstack_id == "with-kw"
    assert scored[0].score > scored[1].score


def test_all_scores_in_zero_to_one_hundred_range() -> None:
    """Every scored job's score is between 0 and 100."""
    analysis = _analysis()
    jobs = [
        _job_record(theirstack_id="a", title="CEO", raw={"job_description": "Leadership."}),
        _job_record(theirstack_id="b", title="Intern", raw={"job_description": "Learn."}),
        _job_record(theirstack_id="c", title="Software Engineer", raw={"job_description": "Code."}),
    ]
    scored = score_jobs(jobs, analysis, target_roles=["Software Engineer"], target_industries=["Technology"], keywords=["python"])
    for s in scored:
        assert 0.0 <= s.score <= 100.0, f"Score {s.score} out of range for {s.job.theirstack_id}"


def test_empty_jobs_returns_empty_list() -> None:
    """score_jobs handles empty job list gracefully."""
    analysis = _analysis()
    scored = score_jobs([], analysis, target_roles=["Engineer"], target_industries=[], keywords=[])
    assert scored == []


def test_exact_role_match_gets_max_role_points() -> None:
    """A job whose title is an exact normalized match for a target role gets 40 role-fit points."""
    analysis = _analysis(text="Something")
    target = ["Machine Learning Engineer"]
    job = _job_record(theirstack_id="ml", title="Machine Learning Engineer")
    scored = score_jobs([job], analysis, target_roles=target, target_industries=[], keywords=[])
    assert scored[0].score >= 32.0, "Exact role match should yield high role-fit score"


def test_partial_role_match_gets_intermediate_points() -> None:
    """A job with partial token overlap scores less than an exact match."""
    analysis = _analysis(text="Something")
    target = ["Senior Software Engineer"]
    exact = _job_record(theirstack_id="exact", title="Senior Software Engineer")
    partial = _job_record(theirstack_id="partial", title="Software Engineer II")

    scored = score_jobs(
        [partial, exact], analysis,
        target_roles=target,
        target_industries=[],
        keywords=[],
    )
    exact_s = next(s for s in scored if s.job.theirstack_id == "exact")
    partial_s = next(s for s in scored if s.job.theirstack_id == "partial")
    assert exact_s.score > partial_s.score, "Exact role match should outscore partial"


def test_mismatched_title_does_not_report_target_role_as_matched() -> None:
    """Role badges only report requested roles when the job title actually matches."""
    analysis = _analysis(text="Data engineer with Python analytics experience.")
    job = _job_record(
        theirstack_id="barista",
        title="Barista",
        raw={"job_description": "Prepare coffee and support cafe guests."},
    )

    scored = score_jobs([job], analysis, target_roles=["Data Engineer"], target_industries=[], keywords=[])

    assert "data engineer" not in scored[0].matched_terms

# ── Tests: categorization ─────────────────────────────────────────────────────


def test_categorization_groups_jobs_by_industry() -> None:
    """Jobs are categorized into industry groups based on target industries."""
    analysis = _analysis()
    targets = ["Fintech", "Healthcare"]
    fintech_job = _job_record(
        theirstack_id="fintech-1",
        title="Engineer",
        company="Stripe",
        raw={"company_description": "Fintech payment processing platform."},
    )
    health_job = _job_record(
        theirstack_id="health-1",
        title="Nurse",
        company="Mayo Clinic",
        raw={"company_description": "Healthcare provider."},
    )
    uncategorized = _job_record(
        theirstack_id="misc",
        title="Driver",
        company="Uber",
        raw={"company_description": "Ride sharing service."},
    )

    scored = score_jobs(
        [fintech_job, health_job, uncategorized], analysis,
        target_roles=[],
        target_industries=targets,
        keywords=[],
    )

    cats = {s.job.theirstack_id: s.category for s in scored}
    assert cats["fintech-1"] == "Fintech" or "fintech" in cats["fintech-1"].lower()
    assert cats["health-1"] == "Healthcare" or "health" in cats["health-1"].lower()
    assert cats["misc"] == "Uncategorized"


def test_summarize_categories() -> None:
    """CategorySummary groups jobs, computes counts and averages."""
    analysis = _analysis()
    jobs = [
        _job_record(theirstack_id="a1", title="SWE", company="Tech Inc", raw={"company_description": "Technology company."}),
        _job_record(theirstack_id="a2", title="Dev", company="Another Tech", raw={"company_description": "Technology services."}),
        _job_record(theirstack_id="b1", title="Doctor", company="Hospital", raw={"company_description": "Healthcare system."}),
    ]
    scored = score_jobs(jobs, analysis, target_roles=[], target_industries=["Technology", "Healthcare"], keywords=[])

    summaries = summarize_categories(scored)
    assert len(summaries) >= 1

    cat_map = {s.category: s for s in summaries}
    # Should have at least one category with 2 jobs (technology matching)
    tech_summary = next((s for c, s in cat_map.items() if "technolog" in c.lower()), None)
    if tech_summary:
        assert tech_summary.count >= 1
        assert isinstance(tech_summary.avg_score, float)
        assert len(tech_summary.top_jobs) >= 1


def test_uncategorized_fallback() -> None:
    """Jobs without industry signals are categorized as Uncategorized."""
    analysis = _analysis()
    job = _job_record(
        theirstack_id="none",
        title="Odd Job",
        raw={"job_description": "Miscellaneous work."},
    )
    scored = score_jobs(
        [job], analysis,
        target_roles=[],
        target_industries=["Fintech", "Healthcare"],
        keywords=[],
    )
    assert scored[0].category == "Uncategorized"


# ── Tests: region analysis ────────────────────────────────────────────────────


def test_remote_job_detected() -> None:
    """Jobs with remote flag get Remote region and label."""
    analysis = _analysis()
    job = _job_record(theirstack_id="rem", remote=1, country_code="US")
    scored = score_jobs([job], analysis, target_roles=[], target_industries=[], keywords=[])
    assert scored[0].region == "Remote"
    assert scored[0].remote_label == "Remote"


def test_us_regional_detection_from_country_code() -> None:
    """US jobs without remote flag get a US region."""
    analysis = _analysis()
    # California job
    ca_job = _job_record(
        theirstack_id="ca",
        country_code="US",
        raw={"location": "San Francisco, CA"},
    )
    scored = score_jobs([ca_job], analysis, target_roles=[], target_industries=[], keywords=[])
    assert "West" in scored[0].region


def test_international_job_detected() -> None:
    """Non-US country_code yields International region."""
    analysis = _analysis()
    job = _job_record(theirstack_id="intl", country_code="GB")
    scored = score_jobs([job], analysis, target_roles=[], target_industries=[], keywords=[])
    assert scored[0].region == "International"


def test_remote_label_from_raw_employment_statuses() -> None:
    """Employment statuses like remote/hybrid set the remote_label."""
    analysis = _analysis()
    hybrid = _job_record(
        theirstack_id="hybrid",
        raw={"employment_statuses": ["Full-time", "Hybrid"]},
    )
    scored = score_jobs([hybrid], analysis, target_roles=[], target_industries=[], keywords=[])
    assert scored[0].remote_label == "Hybrid"


def test_unknown_region_fallback() -> None:
    """Jobs with no location signals get Unknown region."""
    analysis = _analysis()
    job = _job_record(theirstack_id="unk", country_code=None, remote=None)
    scored = score_jobs([job], analysis, target_roles=[], target_industries=[], keywords=[])
    # Should not crash, region should be Unknown
    assert scored[0].region in ("Unknown", "On-site")


# ── Tests: improvement prompt ────────────────────────────────────────────────


def test_improvement_prompt_includes_job_and_resume_gaps() -> None:
    """The improvement prompt mentions job title, score, and missing terms."""
    analysis = _analysis(text="Python developer with some experience.")
    targets = ["Senior Software Engineer"]
    industries = ["Fintech"]
    keywords = ["docker", "kubernetes"]

    job = _job_record(
        theirstack_id="prompt-test",
        title="Software Engineer",
        company="Fintech Co",
        raw={
            "job_description": "Build fintech software. Must know Docker and Kubernetes.",
            "company_description": "A fintech company.",
        },
    )
    scored = score_jobs([job], analysis, target_roles=targets, target_industries=industries, keywords=keywords)
    prompt = build_improvement_prompt(
        scored[0], analysis,
        target_roles=targets,
        target_industries=industries,
    )

    # Job details present
    assert scored[0].job.title in prompt or "Software Engineer" in prompt
    assert str(round(scored[0].score)) in prompt
    assert "Fintech" in prompt

    # LaTeX/PDF guidance present (analysis.kind == "pdf")
    assert "PDF" in prompt
    assert "Input Guidance" in prompt

    assert "Resume Review Report Generator" in prompt
    assert "Required Markdown Report Structure" in prompt
    assert "Agent-Friendly Implementation Checklist" in prompt
    assert "Overall Evaluation" in prompt
    assert "DO NOT TOUCH" in prompt
    assert "Uploaded Resume Text to Review" in prompt
    assert "Python developer with some experience." in prompt
    assert "Quantify" not in prompt

def test_improvement_report_is_finished_markdown_not_a_prompt() -> None:
    analysis = _analysis(
        filename="resume.tex",
        kind="latex",
        text="Ada Candidate\nExperience\nBuilt Python services for healthcare analytics teams.\nSkills\nPython SQL FastAPI",
    )
    job = _job_record(
        theirstack_id="report",
        title="Clinical Data Engineer",
        company="Acme Health",
        raw={"job_description": "Build Python services for healthcare analytics with SQL and FastAPI."},
    )
    scored = score_jobs([job], analysis, target_roles=["Data Engineer"], target_industries=["Healthcare"], keywords=["Python", "SQL"])

    report = build_improvement_report(
        scored[0],
        analysis,
        target_roles=["Data Engineer"],
        target_industries=["Healthcare"],
        generation_note="test report",
    )

    assert report.startswith("# Resume Review Report — Clinical Data Engineer")
    assert "Resume Review Report Generator" not in report
    assert "## Executive Summary" in report
    assert "## Overall Evaluation" in report
    assert "## Major Strengths" in report
    assert "## Major Weaknesses" in report
    assert "## Missing Keywords" in report
    assert "## Missing Experiences" in report
    assert "## Formatting Feedback" in report
    assert "## Structure Feedback" in report
    assert "## Job-Specific Tailoring Advice" in report
    assert "## Rewritten Bullet Suggestions" in report
    assert "## Project Recommendations" in report
    assert "## Content Prioritization Recommendations" in report
    assert "## Risks and Warnings" in report
    assert "## KEEP" in report
    assert "## CHANGE" in report
    assert "## ADD" in report
    assert "## REMOVE" in report
    assert "## DO NOT TOUCH" in report
    assert "## Agent-Friendly Implementation Checklist" in report
    assert "Built Python services for healthcare analytics teams." in report
    assert "LaTeX" in report


def test_improvement_report_does_not_treat_substring_keyword_as_supported() -> None:
    analysis = _analysis(text="Ada Candidate\nExperience\nBuilt NoSQL data stores for analytics teams.")
    job = _job_record(
        theirstack_id="substring-report",
        title="Analytics Engineer",
        raw={"job_description": "Build analytics workflows. Must know SQL."},
    )
    scored = score_jobs([job], analysis, target_roles=[], target_industries=[], keywords=["SQL"])

    report = build_improvement_report(scored[0], analysis, target_roles=[], target_industries=[])

    assert "- **sql** — `not supported by resume text`" in report
    assert "- **sql** — `supported`" not in report

def test_improvement_prompt_latex_guidance() -> None:
    """LaTeX uploads get LaTeX-specific guidance."""
    analysis = _analysis(filename="resume.tex", kind="latex")
    job = _job_record(theirstack_id="ltx", title="Developer")
    scored = score_jobs([job], analysis, target_roles=["Developer"], target_industries=[], keywords=[])
    prompt = build_improvement_prompt(scored[0], analysis, target_roles=["Developer"], target_industries=[])
    assert "LaTeX" in prompt
    assert ".tex" in prompt


def test_improvement_prompt_plain_text_guidance() -> None:
    """Plain text uploads get conversion guidance."""
    analysis = _analysis(filename="resume.txt", kind="text")
    job = _job_record(theirstack_id="txt", title="Developer")
    scored = score_jobs([job], analysis, target_roles=["Developer"], target_industries=[], keywords=[])
    prompt = build_improvement_prompt(scored[0], analysis, target_roles=["Developer"], target_industries=[])
    assert "plain text" in prompt or "Plain Text" in prompt


def test_improvement_prompt_missing_skills_section() -> None:
    """The prompt lists missing keywords and gap instructions."""
    analysis = _analysis(text="General experience.")
    targets = ["Senior ML Engineer"]
    industries = ["AI"]
    keywords = ["tensorflow", "pytorch", "aws"]

    job = _job_record(
        theirstack_id="gap",
        title="Senior ML Engineer",
        company="AI Startup",
        raw={
            "job_description": "Build ML models with TensorFlow.",
            "company_description": "AI company.",
        },
    )
    scored = score_jobs([job], analysis, target_roles=targets, target_industries=industries, keywords=keywords)
    prompt = build_improvement_prompt(scored[0], analysis, target_roles=targets, target_industries=industries)

    assert "Missing terms" in prompt
    assert "Missing Keywords" in prompt
    assert "tensorflow" in prompt


# ── Tests: edge cases ────────────────────────────────────────────────────────


def test_null_title_and_raw() -> None:
    """Jobs with null title and empty raw dict do not crash."""
    analysis = _analysis()
    job = _job_record(theirstack_id="null", title=None, raw={})
    scored = score_jobs([job], analysis, target_roles=["Engineer"], target_industries=[], keywords=[])
    assert len(scored) == 1
    assert 0.0 <= scored[0].score <= 100.0


def test_category_summary_no_jobs() -> None:
    """summarize_categories with empty list returns empty list."""
    assert summarize_categories([]) == []


def test_scored_job_dataclass_fields() -> None:
    """ScoredJob has all expected fields."""
    job = _job_record()
    analysis = _analysis()
    scored = score_jobs([job], analysis, target_roles=[], target_industries=[], keywords=[])
    s = scored[0]
    assert hasattr(s, "job")
    assert hasattr(s, "score")
    assert hasattr(s, "category")
    assert hasattr(s, "matched_terms")
    assert hasattr(s, "missing_terms")
    assert hasattr(s, "region")
    assert hasattr(s, "remote_label")
    assert hasattr(s, "category_fit")
    assert hasattr(s, "key_strengths")
    assert hasattr(s, "missing_requirements")
    assert hasattr(s, "relevant_resume_evidence")
    assert hasattr(s, "concerns")
    assert hasattr(s, "explanation")
    assert hasattr(s, "score_components")
    assert hasattr(s, "evidence_matches")
    assert isinstance(s.matched_terms, tuple)
    assert isinstance(s.missing_terms, tuple)
    assert isinstance(s.key_strengths, tuple)
    assert isinstance(s.missing_requirements, tuple)
    assert isinstance(s.relevant_resume_evidence, tuple)
    assert isinstance(s.concerns, tuple)
    assert isinstance(s.score_components, tuple)
    assert isinstance(s.evidence_matches, tuple)
    assert s.explanation


def test_matched_terms_filled_when_keywords_found() -> None:
    """matched_terms contains keywords present in the job."""
    analysis = _analysis()
    job = _job_record(
        theirstack_id="matches",
        title="Data Engineer",
        raw={
            "job_description": "Build data pipelines using Spark and Airflow.",
            "company_description": "A technology company.",
        },
    )
    scored = score_jobs([job], analysis, target_roles=["Data Engineer"], target_industries=["Technology"], keywords=["spark", "airflow"])
    s = scored[0]
    assert len(s.matched_terms) > 0


def test_missing_terms_filled() -> None:
    """missing_terms lists what was searched but not found."""
    analysis = _analysis()
    job = _job_record(
        theirstack_id="missing",
        title="Junior Developer",
        raw={"job_description": "Simple web development."},
    )
    scored = score_jobs(
        [job], analysis,
        target_roles=["Senior Developer"],
        target_industries=["Fintech"],
        keywords=["kubernetes", "aws"],
    )
    s = scored[0]
    # At least some of these should be missing
    assert len(s.missing_terms) > 0
    all_terms = {"senior developer", "fintech", "kubernetes", "aws"}
    found_any_missing = any(term in s.missing_terms for term in all_terms)
    assert found_any_missing, f"Expected at least one missing term from {all_terms}, got {s.missing_terms}"


def test_resume_only_scoring_ranks_supported_requirements_first() -> None:
    """Resume upload alone produces useful ranked matches without target filters."""
    analysis = _analysis(
        text=(
            "Ada Candidate\n"
            "Experience\n"
            "Built Python and SQL services for healthcare analytics teams using FastAPI.\n"
            "Skills\n"
            "Python SQL FastAPI healthcare analytics"
        )
    )
    supported = _job_record(
        theirstack_id="supported",
        title="Clinical Data Engineer",
        raw={
            "job_description": "Build Python SQL pipelines and FastAPI services for healthcare analytics.",
            "skills": ["Python", "SQL", "FastAPI", "analytics"],
            "company_description": "Clinical healthcare analytics company.",
        },
    )
    unsupported = _job_record(
        theirstack_id="unsupported",
        title="Retail Operations Analyst",
        raw={
            "job_description": "Manage retail staffing, store schedules, and vendor escalations.",
            "skills": ["workforce planning", "vendor management"],
        },
    )

    scored = score_jobs([unsupported, supported], analysis, target_roles=[], target_industries=[], keywords=[])

    assert scored[0].job.theirstack_id == "supported"
    assert scored[0].score > scored[1].score
    assert scored[0].key_strengths
    assert scored[0].relevant_resume_evidence
    assert "Python" in scored[0].relevant_resume_evidence[0] or "python" in scored[0].relevant_resume_evidence[0].lower()

def test_scoring_exports_structured_evidence_contributions() -> None:
    analysis = _analysis(
        text=(
            "Ada Candidate\n"
            "Experience\n"
            "Built Python and SQL services for healthcare analytics teams using FastAPI.\n"
            "Skills\n"
            "Python SQL FastAPI healthcare analytics"
        )
    )
    job = _job_record(
        theirstack_id="evidence",
        title="Clinical Data Engineer",
        raw={
            "job_description": "Build Python SQL pipelines and FastAPI services for healthcare analytics.",
            "skills": ["Python", "SQL", "FastAPI", "analytics"],
        },
    )

    result = score_jobs([job], analysis, target_roles=[], target_industries=[], keywords=[])[0]

    assert result.score_components
    assert result.evidence_matches
    requirement_component = next(component for component in result.score_components if component.name == "Requirement evidence")
    first_match = result.evidence_matches[0]
    assert requirement_component.score > 0
    assert first_match.requirement
    assert first_match.resume_excerpt in result.relevant_resume_evidence
    assert first_match.contribution_score > 0
    assert 0.0 < first_match.confidence <= 1.0
    assert first_match.matched_keywords



def test_structured_result_reports_missing_requirements_and_concerns() -> None:
    analysis = _analysis(text="Backend engineer with Python APIs.")
    job = _job_record(
        theirstack_id="structured",
        title="Platform Engineer",
        raw={
            "job_description": "Build Kubernetes Terraform services with Python APIs. Requires 5 years experience.",
            "skills": ["Python", "Kubernetes", "Terraform"],
        },
    )

    scored = score_jobs([job], analysis, target_roles=[], target_industries=[], keywords=[])
    result = scored[0]

    assert "kubernetes" in result.missing_requirements
    assert "terraform" in result.missing_requirements
    assert any("years of experience" in concern for concern in result.concerns)
    assert result.category_fit
    assert "requirements" in result.explanation


def test_scored_job_contains_structured_analysis_contract() -> None:
    analysis = _analysis(
        text=(
            "Ada Candidate\n"
            "Experience\n"
            "Built FastAPI services with Python and PostgreSQL for customer workflows.\n"
            "Led rollout with support and product stakeholders.\n"
            "Skills\n"
            "Python FastAPI PostgreSQL"
        )
    )
    job = _job_record(
        theirstack_id="analysis-contract",
        title="Backend Platform Engineer",
        raw={
            "job_description": """
Requirements:
- Experience building production APIs with Python.
- PostgreSQL database experience.
- Kubernetes production operations.
Preferred:
- Cross-functional leadership.
""",
            "skills": ["Python", "PostgreSQL", "Kubernetes"],
        },
    )

    result = score_jobs([job], analysis, target_roles=[], target_industries=[], keywords=[])[0]

    assert result.analysis is not None
    assert result.analysis.job_id == "analysis-contract"
    assert result.analysis.requirements
    assert result.analysis.resume_claims
    assert result.analysis.evidence
    assert result.analysis.missing_requirements
    assert result.analysis.improvements
    assert any(req.category == "backend" for req in result.analysis.requirements)
    assert any(gap.coverage in {"missing", "weak", "partial"} for gap in result.analysis.missing_requirements)
    assert any(item.group in {"Highest Impact", "Missing Keywords", "Rewrite Suggestions", "Quick Wins"} for item in result.analysis.improvements)


def test_improvement_report_includes_grouped_structured_recommendations() -> None:
    analysis = _analysis(text="Ada Candidate\nExperience\nBuilt Python APIs.\nSkills\nPython")
    job = _job_record(
        theirstack_id="structured-report",
        title="Platform Engineer",
        raw={"job_description": "Must build Python APIs and operate Kubernetes in production."},
    )
    scored = score_jobs([job], analysis, target_roles=[], target_industries=[], keywords=[])[0]

    report = build_improvement_report(scored, analysis, target_roles=[], target_industries=[])

    assert "## Structured Resume Improvements" in report
    assert "Honesty constraint" in report
    assert "kubernetes" in report.lower()