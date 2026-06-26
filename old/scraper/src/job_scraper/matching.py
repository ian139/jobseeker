"""Deterministic resume-to-job scoring and market analysis.

Pure keyword-based scoring — no network calls, no LLM inference.
Score range 0-100 based on role fit, industry alignment, keyword relevance,
and resume-text overlap with the job description.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field, replace
from typing import Any, Sequence

from job_scraper.storage import JobRecord
from job_scraper.resume_uploads import MAX_PROMPT_RESUME_CHARS, UploadedResumeAnalysis

# ── Public dataclasses ───────────────────────────────────────────────────────


@dataclass(frozen=True)
class ScoreComponent:
    """One aggregate scoring component that contributes to a scored job."""

    name: str
    score: float
    max_score: float
    explanation: str


@dataclass(frozen=True)
class ParsedRequirement:
    """One traceable job requirement extracted from a stored job."""

    id: str
    text: str
    category: str
    importance: str
    weight: float
    keywords: tuple[str, ...]
    normalized_terms: tuple[str, ...]
    source_span: str
    evidence_type: str = "skill_or_experience"


@dataclass(frozen=True)
class ResumeClaim:
    """One reusable claim parsed from an uploaded resume/profile text."""

    id: str
    text: str
    category: str
    skills: tuple[str, ...]
    normalized_terms: tuple[str, ...]
    source_section: str
    source_item: str
    confidence: float
    evidence_strength: str


@dataclass(frozen=True)
class RequirementMatch:
    """Comparison result for one job requirement against resume claims."""

    requirement_id: str
    resume_claim_ids: tuple[str, ...]
    match_strength: str
    confidence: float
    matched_terms: tuple[str, ...]
    coverage: str
    explanation: str


@dataclass(frozen=True)
class MissingRequirement:
    """Requirement that is missing, weak, or only partially supported."""

    requirement_id: str
    requirement_text: str
    category: str
    importance: str
    coverage: str
    score_impact: float
    related_resume_claims: tuple[str, ...]
    explanation: str
    improvement_hint: str


@dataclass(frozen=True)
class ResumeImprovement:
    """Actionable resume recommendation grounded in parsed evidence/gaps."""

    id: str
    group: str
    requirement_id: str
    missing_requirement_id: str | None
    evidence_ids: tuple[str, ...]
    resume_claim_ids: tuple[str, ...]
    target_section: str
    recommendation: str
    why_it_matters: str
    suggested_wording: str
    honesty_constraint: str
    impact: str


@dataclass(frozen=True)
class EvidenceMatch:
    """A resume evidence item matched to one extracted job requirement."""

    requirement: str
    resume_excerpt: str
    contribution_score: float
    confidence: float
    matched_keywords: tuple[str, ...]
    explanation: str
    id: str = ""
    requirement_id: str = ""
    requirement_text: str = ""
    requirement_category: str = ""
    resume_claim_id: str = ""
    resume_claim_text: str = ""
    source_resume_section: str = ""
    normalized_terms: tuple[str, ...] = ()
    strength: str = ""


@dataclass(frozen=True)
class JobResumeAnalysis:
    """Internal contract consumed by scorer, UI, and improvement generation."""

    job_id: str
    summary: dict[str, object]
    requirements: tuple[ParsedRequirement, ...]
    resume_claims: tuple[ResumeClaim, ...]
    matches: tuple[RequirementMatch, ...]
    evidence: tuple[EvidenceMatch, ...]
    missing_requirements: tuple[MissingRequirement, ...]
    improvements: tuple[ResumeImprovement, ...]
    debug: dict[str, object]


@dataclass(frozen=True)
class ScoredJob:
    """Result of scoring a single job against a resume and target criteria."""

    job: JobRecord
    score: float
    category: str
    matched_terms: tuple[str, ...]
    missing_terms: tuple[str, ...]
    region: str
    remote_label: str
    category_fit: str
    key_strengths: tuple[str, ...]
    missing_requirements: tuple[str, ...]
    relevant_resume_evidence: tuple[str, ...]
    concerns: tuple[str, ...]
    explanation: str
    score_components: tuple[ScoreComponent, ...] = ()
    evidence_matches: tuple[EvidenceMatch, ...] = ()
    analysis: JobResumeAnalysis | None = None


@dataclass(frozen=True)
class CategorySummary:
    """Aggregate stats for one industry / market category."""

    category: str
    count: int
    avg_score: float
    top_jobs: tuple[ScoredJob, ...]


# ── Public API ───────────────────────────────────────────────────────────────


def score_jobs(
    jobs: Sequence[JobRecord],
    analysis: UploadedResumeAnalysis,
    *,
    target_roles: Sequence[str],
    target_industries: Sequence[str],
    keywords: Sequence[str],
    detailed_limit: int | None = None,
) -> list[ScoredJob]:
    """Score every job in *jobs* and return them sorted descending by score.

    The scorer is intentionally local and cheap: it builds one resume index,
    one normalized job index per job, and combines role fit, requested filters,
    job requirement coverage, and direct resume evidence into a 0-100 score.
    """
    if not jobs:
        return []

    resume_index = _build_resume_index(analysis)
    norm_industries = tuple(_normalize_text(term) for term in target_industries if term.strip())
    norm_keywords = tuple(_normalize_text(term) for term in keywords if term.strip())

    scored: list[ScoredJob] = []
    job_index_cache: dict[str, _JobIndex] = {}
    for job in jobs:
        job_index = job_index_cache.setdefault(job.theirstack_id, _build_job_index(job))
        role_pts = max(_score_role_fit(job.title, target_roles), _score_resume_title_fit(job_index, resume_index))
        industry_pts = _score_industry(job, target_industries)
        kw_pts = _score_keywords(job, keywords)
        requirement_terms = _job_requirement_terms(job, job_index, norm_industries, norm_keywords)
        matched_requirements, missing_requirements = _split_supported_terms(requirement_terms, resume_index)
        requirement_pts = _score_requirement_coverage(matched_requirements, requirement_terms)
        resume_pts = _score_resume_overlap(job, resume_index.tokens)
        score_components = _score_components(
            role_pts=role_pts,
            industry_pts=industry_pts,
            kw_pts=kw_pts,
            requirement_pts=requirement_pts,
            resume_pts=resume_pts,
            requirement_count=len(requirement_terms),
            matched_requirement_count=len(matched_requirements),
        )
        score = min(100.0, role_pts + industry_pts + kw_pts + requirement_pts + resume_pts)

        category = _categorize_job(job, target_industries)
        matched = _matched_terms(job, target_roles, target_industries, keywords)
        region, remote_label = _analyze_region(job)
        missing = _missing_terms(job, target_roles, target_industries, keywords)
        evidence = _evidence_for_terms(resume_index, matched_requirements)
        evidence_matches = _evidence_matches_for_terms(
            resume_index,
            matched_requirements,
            requirement_count=len(requirement_terms),
        )
        concerns = _concerns_for_job(job, resume_index, missing_requirements)
        category_fit = _category_fit(category, target_industries, resume_index)
        strengths = _key_strengths(matched_requirements, evidence)
        explanation = _match_explanation(
            score=score,
            role_pts=role_pts,
            industry_pts=industry_pts,
            kw_pts=kw_pts,
            requirement_pts=requirement_pts,
            resume_pts=resume_pts,
            matched_count=len(matched_requirements),
            requirement_count=len(requirement_terms),
            category_fit=category_fit,
        )

        scored.append(
            ScoredJob(
                job=job,
                score=round(score, 1),
                category=category,
                matched_terms=matched,
                missing_terms=missing,
                region=region,
                remote_label=remote_label,
                category_fit=category_fit,
                key_strengths=strengths,
                missing_requirements=missing_requirements,
                relevant_resume_evidence=evidence,
                concerns=concerns,
                explanation=explanation,
                score_components=score_components,
                evidence_matches=evidence_matches,
            )
        )

    scored.sort(key=lambda s: s.job.theirstack_id)
    scored.sort(key=lambda s: (s.job.date_posted or "", s.job.discovered_at or ""), reverse=True)
    scored.sort(key=lambda s: s.score, reverse=True)
    if detailed_limit is None:
        detail_indexes = range(len(scored))
    else:
        detail_indexes = range(min(max(detailed_limit, 0), len(scored)))
    for index in detail_indexes:
        scored[index] = replace(scored[index], analysis=analyze_job_resume(scored[index].job, analysis))
    return scored


def summarize_categories(scored_jobs: Sequence[ScoredJob]) -> list[CategorySummary]:
    """Group *scored_jobs* by category and return aggregate summaries.

    Results are sorted by average score descending.
    """
    groups: dict[str, list[ScoredJob]] = {}
    for sj in scored_jobs:
        groups.setdefault(sj.category, []).append(sj)

    summaries: list[CategorySummary] = []
    for cat, items in groups.items():
        avg = sum(s.score for s in items) / len(items)
        top = tuple(sorted(items, key=lambda s: s.score, reverse=True)[:5])
        summaries.append(CategorySummary(category=cat, count=len(items), avg_score=avg, top_jobs=top))

    summaries.sort(key=lambda cs: cs.avg_score, reverse=True)
    return summaries


def analyze_job_resume(job: JobRecord, analysis: UploadedResumeAnalysis) -> JobResumeAnalysis:
    """Build the structured, inspectable job/resume analysis contract."""
    requirements = parse_job_requirements(job)
    resume_claims = parse_resume_claims(analysis)
    matches, evidence, missing = match_requirements_to_claims(requirements, resume_claims)
    improvements = generate_resume_improvements(requirements, resume_claims, evidence, missing)
    strong_or_partial = sum(1 for match in matches if match.coverage in {"satisfied", "partial"})
    coverage = strong_or_partial / len(requirements) if requirements else 0.0
    bottleneck = missing[0].requirement_text if missing else "No missing requirements detected"
    return JobResumeAnalysis(
        job_id=job.theirstack_id,
        summary={
            "overall_score": round(coverage, 2),
            "confidence": _analysis_confidence(requirements, resume_claims, evidence),
            "requirement_coverage": f"{strong_or_partial}/{len(requirements)}" if requirements else "0/0",
            "bottleneck": bottleneck,
        },
        requirements=requirements,
        resume_claims=resume_claims,
        matches=matches,
        evidence=evidence,
        missing_requirements=missing,
        improvements=improvements,
        debug={
            "job_text_chars": len(_job_text(job)),
            "resume_text_chars": len(analysis.text or ""),
            "parser": "deterministic_keyword_sections_v1",
        },
    )


def parse_job_requirements(job: JobRecord) -> tuple[ParsedRequirement, ...]:
    """Extract structured requirements from source JSON without hallucinating."""
    candidates: list[tuple[str, str, str]] = []
    raw = job.raw

    structured_sources = (
        ("requirements", "required", "requirements"),
        ("job_requirements", "required", "requirements"),
        ("required_qualifications", "required", "requirements"),
        ("minimum_qualifications", "required", "requirements"),
        ("qualifications", "required", "requirements"),
        ("candidate_requirements", "required", "requirements"),
        ("preferred_qualifications", "preferred", "preferred"),
        ("nice_to_have", "preferred", "preferred"),
        ("preferred_skills", "preferred", "preferred"),
        ("bonus_points", "preferred", "preferred"),
        ("responsibilities", "required", "responsibilities"),
        ("job_responsibilities", "required", "responsibilities"),
        ("core_responsibilities", "required", "responsibilities"),
        ("role_responsibilities", "required", "responsibilities"),
        ("skills", "required", "skills"),
        ("technologies", "required", "skills"),
        ("technology_stack", "required", "skills"),
        ("tech_stack", "required", "skills"),
        ("tools", "required", "tools"),
    )
    for key, importance, source_label in structured_sources:
        for text in _flatten_requirement_value(raw.get(key)):
            candidates.extend((item, importance, source_label) for item in _split_requirement_items(text, source_label))

    for key in ("job_description", "description", "job_text", "summary", "overview"):
        value = raw.get(key)
        if not isinstance(value, str):
            continue
        candidates.extend(_requirements_from_description(value))
    if job.description:
        candidates.extend(_requirements_from_description(job.description))
    for skill in job.skills:
        candidates.extend((item, "required", "skills") for item in _split_requirement_items(skill, "skills"))

    seen: set[str] = set()
    requirements: list[ParsedRequirement] = []
    for text, importance, source_label in candidates:
        cleaned = _clean_requirement_text(text)
        if not _useful_requirement_text(cleaned):
            continue
        normalized_key = _normalize_text(cleaned)
        if normalized_key in seen:
            continue
        seen.add(normalized_key)
        category = _requirement_category(cleaned, fallback=source_label)
        keywords = _requirement_keywords(cleaned)
        normalized_terms = _normalized_requirement_terms(cleaned, keywords)
        if not normalized_terms:
            continue
        req_id = f"req_{len(requirements) + 1:03d}"
        requirements.append(
            ParsedRequirement(
                id=req_id,
                text=cleaned,
                category=category,
                importance=importance,
                weight=_requirement_weight(importance, category),
                keywords=keywords,
                normalized_terms=normalized_terms,
                source_span=cleaned,
                evidence_type=_evidence_type_for_category(category),
            )
        )
        if len(requirements) >= 40:
            break

    if requirements:
        return tuple(requirements)

    fallback_text = _job_text(job)
    if not fallback_text:
        return ()
    keywords = tuple(term for term in _keyword_terms(fallback_text)[:8] if term not in _STOP_WORDS)
    if not keywords:
        return ()
    return (
        ParsedRequirement(
            id="req_001",
            text="Job description contains limited structured requirement data.",
            category="general",
            importance="required",
            weight=0.4,
            keywords=keywords,
            normalized_terms=tuple(_normalize_alias(term) for term in keywords),
            source_span=fallback_text[:240],
            evidence_type="general_context",
        ),
    )


def parse_resume_claims(analysis: UploadedResumeAnalysis) -> tuple[ResumeClaim, ...]:
    """Parse uploaded resume/profile text into reusable evidence claims."""
    claims: list[ResumeClaim] = []
    current_section = "resume"
    seen: set[str] = set()
    for raw_line in re.split(r"[\r\n•]+", analysis.text or ""):
        line = _clean_resume_line(raw_line)
        if not line:
            continue
        if _looks_like_section_heading(line):
            current_section = line.strip(":#").lower()
            continue
        if len(line) < 10 or line.casefold() in seen:
            continue
        seen.add(line.casefold())
        skills = _requirement_keywords(line)
        terms = _normalized_requirement_terms(line, skills)
        if not terms and not _claim_has_signal(line):
            continue
        category = _requirement_category(line, fallback=current_section)
        claim_id = f"claim_{len(claims) + 1:03d}"
        claims.append(
            ResumeClaim(
                id=claim_id,
                text=line,
                category=category,
                skills=skills,
                normalized_terms=terms,
                source_section=current_section,
                source_item=_claim_source_item(line),
                confidence=_claim_confidence(line, terms),
                evidence_strength=_claim_strength(line, terms),
            )
        )
        if len(claims) >= 120:
            break
    return tuple(claims)


def match_requirements_to_claims(
    requirements: Sequence[ParsedRequirement],
    resume_claims: Sequence[ResumeClaim],
) -> tuple[tuple[RequirementMatch, ...], tuple[EvidenceMatch, ...], tuple[MissingRequirement, ...]]:
    """Compare requirements to resume claims and return matches/evidence/gaps."""
    matches: list[RequirementMatch] = []
    evidence: list[EvidenceMatch] = []
    missing: list[MissingRequirement] = []
    requirement_count = max(len(requirements), 1)
    for requirement in requirements:
        ranked = sorted(
            (
                _claim_match_score(requirement, claim)
                for claim in resume_claims
            ),
            key=lambda item: (-item[0], -item[1].confidence, item[1].id),
        )
        ranked = [item for item in ranked if item[0] > 0.0]
        best_score, best_claim, matched_terms = (0.0, None, ()) if not ranked else ranked[0]
        related_claims = tuple(claim.id for score, claim, _terms in ranked[:3] if score >= 0.18)
        coverage = _coverage_label(best_score)
        confidence = round(min(1.0, best_score + (best_claim.confidence * 0.25 if best_claim else 0.0)), 2)
        matches.append(
            RequirementMatch(
                requirement_id=requirement.id,
                resume_claim_ids=related_claims,
                match_strength=_strength_label(best_score),
                confidence=confidence,
                matched_terms=matched_terms,
                coverage=coverage,
                explanation=_requirement_match_explanation(requirement, best_claim, matched_terms, coverage),
            )
        )
        if best_claim is not None and coverage in {"satisfied", "partial", "weak"}:
            contribution = round((35.0 / requirement_count) * min(best_score, 1.0), 1)
            evidence.append(
                EvidenceMatch(
                    requirement=requirement.text,
                    resume_excerpt=best_claim.text,
                    contribution_score=contribution,
                    confidence=confidence,
                    matched_keywords=matched_terms,
                    explanation=_evidence_explanation(requirement, best_claim, matched_terms, coverage),
                    id=f"evidence_{len(evidence) + 1:03d}",
                    requirement_id=requirement.id,
                    requirement_text=requirement.text,
                    requirement_category=requirement.category,
                    resume_claim_id=best_claim.id,
                    resume_claim_text=best_claim.text,
                    source_resume_section=best_claim.source_section,
                    normalized_terms=tuple(dict.fromkeys((*requirement.normalized_terms, *best_claim.normalized_terms))),
                    strength=_strength_label(best_score),
                )
            )
        if coverage in {"missing", "weak", "partial"}:
            missing.append(
                MissingRequirement(
                    requirement_id=requirement.id,
                    requirement_text=requirement.text,
                    category=requirement.category,
                    importance=requirement.importance,
                    coverage=coverage,
                    score_impact=round((35.0 / requirement_count) * (1.0 - min(best_score, 1.0)), 2),
                    related_resume_claims=related_claims,
                    explanation=_missing_explanation(requirement, best_claim, coverage),
                    improvement_hint=_improvement_hint(requirement, best_claim, coverage),
                )
            )
    evidence.sort(key=lambda item: (-item.contribution_score, -item.confidence, item.requirement))
    missing.sort(key=lambda item: (-item.score_impact, item.category, item.requirement_text))
    return tuple(matches), tuple(evidence[:12]), tuple(missing[:16])


def generate_resume_improvements(
    requirements: Sequence[ParsedRequirement],
    resume_claims: Sequence[ResumeClaim],
    evidence: Sequence[EvidenceMatch],
    missing_requirements: Sequence[MissingRequirement],
) -> tuple[ResumeImprovement, ...]:
    """Generate grouped, honesty-constrained resume recommendations."""
    improvements: list[ResumeImprovement] = []
    evidence_by_req = {item.requirement_id: item for item in evidence}
    claims_by_id = {claim.id: claim for claim in resume_claims}
    requirements_by_id = {requirement.id: requirement for requirement in requirements}

    for item in missing_requirements[:10]:
        requirement = requirements_by_id.get(item.requirement_id)
        linked_evidence = evidence_by_req.get(item.requirement_id)
        related_claims = tuple(claim_id for claim_id in item.related_resume_claims if claim_id in claims_by_id)
        group = _improvement_group(item)
        target_section = _target_section_for_gap(item, related_claims, claims_by_id)
        suggested_wording = _suggested_wording(item, related_claims, claims_by_id)
        improvements.append(
            ResumeImprovement(
                id=f"improvement_{len(improvements) + 1:03d}",
                group=group,
                requirement_id=item.requirement_id,
                missing_requirement_id=item.requirement_id,
                evidence_ids=(linked_evidence.id,) if linked_evidence and linked_evidence.id else (),
                resume_claim_ids=related_claims,
                target_section=target_section,
                recommendation=_recommendation_text(item, requirement, related_claims, claims_by_id),
                why_it_matters=f"The job explicitly asks for `{item.requirement_text}`; current coverage is {item.coverage}.",
                suggested_wording=suggested_wording,
                honesty_constraint=_honesty_constraint(item, related_claims),
                impact="high" if item.importance == "required" and item.score_impact >= 2.0 else "medium",
            )
        )

    supported_evidence = [item for item in evidence if item.strength in {"strong", "direct"}]
    for item in supported_evidence[:4]:
        improvements.append(
            ResumeImprovement(
                id=f"improvement_{len(improvements) + 1:03d}",
                group="Quick Wins",
                requirement_id=item.requirement_id,
                missing_requirement_id=None,
                evidence_ids=(item.id,) if item.id else (),
                resume_claim_ids=(item.resume_claim_id,) if item.resume_claim_id else (),
                target_section=item.source_resume_section or "experience",
                recommendation=f"Move or preserve the resume evidence for `{item.requirement_text or item.requirement}` near the top of the relevant section.",
                why_it_matters="This is already truthful support for the selected job and should be easy for a reviewer or ATS to find.",
                suggested_wording=_supported_evidence_wording(item),
                honesty_constraint="Use this only if the excerpt remains factually accurate; do not add scope, metrics, or tools that are not in the source claim.",
                impact="medium",
            )
        )
    return tuple(improvements[:16])

def build_improvement_prompt(
    scored_job: ScoredJob,
    analysis: UploadedResumeAnalysis,
    *,
    target_roles: Sequence[str],
    target_industries: Sequence[str],
) -> str:
    """Build a Markdown prompt that generates a comprehensive resume review report."""
    job = scored_job.job
    format_map = {
        "pdf": (
            "The resume was uploaded as a PDF. Analyze only the extracted text below; if text order looks "
            "damaged by PDF extraction, call that out in Source and Parsing Notes. Recommend edits to the "
            "source document, then re-export to PDF. Do not ask anyone to edit the PDF binary directly."
        ),
        "latex": (
            "The resume was uploaded as LaTeX. Treat the extracted text as semantic resume content, but "
            "write recommendations that can be applied safely to the .tex source. Preserve LaTeX structure "
            "unless a section, bullet, or ordering change is explicitly justified."
        ),
        "text": (
            "The resume was uploaded as plain text or Markdown. Review the text directly and include layout "
            "and ATS guidance for producing a clean final PDF if the candidate will submit one."
        ),
    }
    format_guide = format_map.get(analysis.kind, format_map["text"])
    role_norm = " ".join(_normalize_text(r) for r in target_roles)
    title_norm = _normalize_text(job.title or "")
    role_match_tokens = sorted(_tokenize(role_norm) & _tokenize(title_norm))
    role_fit_pct = _score_role_fit(job.title, target_roles) / 40.0 * 100 if target_roles else 0.0
    industry_pct = _score_industry(job, target_industries) / 30.0 * 100 if target_industries else 0.0
    target_role_text = ", ".join(target_roles) or "Not specified"
    target_industry_text = ", ".join(target_industries) or "Not specified"
    role_token_text = ", ".join(role_match_tokens) or "None"
    matched_terms = ", ".join(scored_job.matched_terms) or "None detected"
    missing_terms = ", ".join(scored_job.missing_terms) or "None detected"
    strengths = ", ".join(scored_job.key_strengths) or "None detected"
    missing_requirements = ", ".join(scored_job.missing_requirements) or "None detected"
    evidence = "\n".join(f"- {line}" for line in scored_job.relevant_resume_evidence)
    concerns = "\n".join(f"- {concern}" for concern in scored_job.concerns)
    url = job.final_url or job.url or "Not available"

    resume_text = analysis.text.strip()
    if len(resume_text) > MAX_PROMPT_RESUME_CHARS:
        resume_text = f"{resume_text[:MAX_PROMPT_RESUME_CHARS]}\n... [resume text truncated for prompt size]"

    job_context = _job_context(job)
    if len(job_context) > 16_000:
        job_context = f"{job_context[:16_000]}\n... [job context truncated for prompt size]"

    return f"""# Resume Review Report Generator — {job.title or 'Untitled Role'}

You are an expert resume reviewer and technical hiring strategist. Generate a finished Markdown report, not instructions. The report must deeply analyze the uploaded resume against the selected job or category while preserving strong existing content by default.

## Non-Negotiable Rules
- Use only facts present in the uploaded resume and job context. Do not invent employers, dates, titles, degrees, certifications, tools, metrics, awards, publications, clearances, or contact details.
- Default to preservation. Keep existing content unless the report explicitly says to CHANGE, ADD, REMOVE, or reorder it.
- Prefer targeted edits over rewrites. Explain what each edit fixes and what risk it avoids.
- Rewritten bullets must preserve the original meaning and seniority. If a metric is missing, write `[add measured impact if true]` rather than fabricating a number.
- Separate proven resume evidence from keywords that are missing, weakly supported, or unsupported.
- Warn against changes that would make the resume worse: keyword stuffing, inflated claims, generic phrasing, deleting differentiating projects, or damaging ATS readability.
- Return Markdown only, suitable to save directly as a `.md` file.

## Required Markdown Report Structure
Use these top-level sections exactly:
1. `# Resume Review Report — {job.title or scored_job.category or 'Target Role'}`
2. `## Executive Summary`
3. `## Overall Evaluation`
4. `## Source and Parsing Notes`
5. `## Major Strengths`
6. `## Major Weaknesses`
7. `## Missing Keywords`
8. `## Missing Experiences`
9. `## Formatting Feedback`
10. `## Structure Feedback`
11. `## Job-Specific Tailoring Advice`
12. `## Rewritten Bullet Suggestions`
13. `## Project Recommendations`
14. `## Content Prioritization Recommendations`
15. `## Risks and Warnings`
16. `## KEEP`
17. `## CHANGE`
18. `## ADD`
19. `## REMOVE`
20. `## DO NOT TOUCH`
21. `## Agent-Friendly Implementation Checklist`

The checklist must use checkbox bullets and be specific enough for another AI agent or human reviewer to execute step by step.

## Input Guidance
{format_guide}

## Job Snapshot
- **Title:** {job.title or 'N/A'}
- **Company:** {job.company or 'N/A'}
- **Score:** {scored_job.score:.1f}/100 — {scored_job.explanation}
- **Category:** {scored_job.category}
- **Category fit:** {scored_job.category_fit}
- **Target roles:** {target_role_text}
- **Target industries/categories:** {target_industry_text}
- **Matched terms:** {matched_terms}
- **Missing terms:** {missing_terms}
- **Key strengths:** {strengths}
- **Missing requirements:** {missing_requirements}
- **Role overlap tokens:** {role_token_text}
- **Role-fit assessment:** {role_fit_pct:.0f}%
- **Industry assessment:** {industry_pct:.0f}%
- **Region:** {scored_job.region}
- **Work model:** {scored_job.remote_label}
- **URL:** {url}

## Structured Resume Evidence
{evidence}

## Concerns
{concerns}

## Job Context
```text
{job_context}
```

## Uploaded Resume Metadata
{analysis.facts_markdown}

## Uploaded Resume Text to Review
```text
{resume_text}
```
"""


def build_improvement_report(
    scored_job: ScoredJob,
    analysis: UploadedResumeAnalysis,
    *,
    target_roles: Sequence[str],
    target_industries: Sequence[str],
    generation_note: str | None = None,
) -> str:
    """Build a finished Markdown resume review report without external calls."""
    job = scored_job.job
    resume_lines = _resume_lines(analysis.text)
    headline = resume_lines[0] if resume_lines else "No headline detected"
    target_role = job.title or (target_roles[0] if target_roles else "target role")
    target_category = scored_job.category if scored_job.category != "Uncategorized" else (target_industries[0] if target_industries else "selected category")
    note = generation_note or "Deterministic local report generated from extracted resume text and job metadata."
    job_keywords = _job_keywords(job, scored_job, target_roles, target_industries)
    keyword_rows = _keyword_evidence_rows(job_keywords, analysis.text)
    missing_rows = _keyword_evidence_rows(scored_job.missing_requirements or scored_job.missing_terms, analysis.text)
    evidence_lines = _evidence_lines(resume_lines, (*scored_job.matched_terms, *job_keywords))
    rewrite_candidates = _rewrite_candidates(resume_lines)
    matched_text = ", ".join(scored_job.matched_terms) or "None detected"
    missing_text = ", ".join(scored_job.missing_requirements or scored_job.missing_terms) or "None detected"
    strengths = "\n".join(f"- {value}" for value in scored_job.key_strengths) or "- Extractable resume content exists; preserve factual claims while improving targeting."
    weaknesses = "\n".join(f"- {value}" for value in scored_job.concerns) or "- No major weaknesses were detected from available text."
    keyword_text = "\n".join(keyword_rows) or "- No job/category keywords were available to classify."
    missing_experience_text = "\n".join(missing_rows) or "- No missing requirements were extracted."
    preserve = "\n".join(f"- Keep `{line}` because it provides relevant evidence." for line in evidence_lines[:5]) or f"- Keep the candidate identity/headline as extracted: `{headline}`."
    rewrites = _rewrite_items(rewrite_candidates, target_role)
    project_ordering = _prioritization_items(resume_lines, job_keywords)
    structured_improvements = _structured_improvement_sections(scored_job)

    return f"""# Resume Review Report — {target_role}

## Executive Summary
- **Generation mode:** {note}
- **Target:** {target_role} at {job.company or 'the selected company'}.
- **Fit signal:** {scored_job.score:.1f}/100 in `{target_category}`.
- **Matched terms:** {matched_text}.
- **Main gaps:** {missing_text}.
- **Preservation rule:** preserve strong existing resume content. Apply only justified edits listed below.

## Overall Evaluation
- {scored_job.explanation}
- {scored_job.category_fit}
- The resume should be tailored by moving supported evidence forward, not by rewriting the whole document.

## Source and Parsing Notes
- **Source file:** {analysis.filename}
- **Detected source type:** {analysis.kind}
- {_source_note(analysis.kind)}
- **Likely headline/name:** {headline}

## Major Strengths
{strengths}
{preserve}

## Major Weaknesses
{weaknesses}
- Weak or unsupported terms should be treated as candidate questions before becoming resume edits.

## Missing Keywords
{keyword_text}

## Missing Experiences
{missing_experience_text}
- Do not add any missing experience unless the candidate confirms real supporting work.

## Formatting Feedback
- Keep headings conventional: Summary, Skills, Experience, Projects, Education, Certifications.
- Ensure the final PDF preserves selectable text, links, punctuation, and reading order.
- Keep bullets concise and action-led; prefer one or two lines per bullet.

## Structure Feedback
- Put the strongest evidence for `{target_role}` in the first half of the resume.
- Keep skills grouped by recognizable tools/domains, not a long undifferentiated keyword list.
- Preserve chronological facts and section boundaries unless a move improves relevance without changing meaning.

## Job-Specific Tailoring Advice
- Lead with evidence connected to `{target_role}` and `{target_category}`.
- Surface supported job keywords naturally: {', '.join(job_keywords[:8]) or 'no reliable keyword list available'}.
- Keep unsupported keywords out of the resume until verified.

## Structured Resume Improvements
{structured_improvements}

## Rewritten Bullet Suggestions
{rewrites}

## Project Recommendations
{project_ordering}
- Add a project only if it reflects real work and closes a gap above.

## Content Prioritization Recommendations
- Highest priority: supported evidence for matched requirements and role-title overlap.
- Medium priority: formatting and order changes that improve ATS extraction.
- Lowest priority: optional wording polish that does not affect fit, clarity, or evidence.

## Risks and Warnings
- Do not keyword-stuff unsupported terms.
- Do not inflate seniority, scope, ownership, domains, tools, degrees, certifications, dates, or metrics.
- Do not delete differentiating technical evidence just because it is not a perfect keyword match.
- Do not edit the PDF binary directly; edit the source document and export again.

## KEEP
{preserve}
- Keep factual employers, titles, dates, education, certifications, and contact details unchanged unless verified.

## CHANGE
{_recommended_change_items(scored_job, target_role, target_category, keyword_rows, missing_rows)}

## ADD
- Add supported missing keywords only when they already have truthful resume evidence.
- Add verified metrics for scale, performance, adoption, revenue, reliability, compliance, or team size where true.
- Add a short targeted summary only if it improves clarity without repeating the skills list.

## REMOVE
- Remove duplicate bullets that repeat the same tool/action without new impact.
- Remove unsupported keyword stuffing.
- Remove obsolete or low-signal details only after stronger evidence is preserved elsewhere.

## DO NOT TOUCH
- Do not change facts not discussed in this report.
- Do not rewrite strong bullets that already provide relevant evidence.
- Do not invent metrics, employers, dates, tools, degrees, certifications, or responsibilities.

## Agent-Friendly Implementation Checklist
- [ ] Read `KEEP` and `DO NOT TOUCH` before editing anything.
- [ ] Move the strongest supported evidence for `{target_role}` into the first half of the resume.
- [ ] Apply each `CHANGE` item only where it preserves the original fact.
- [ ] Add only supported items from `ADD`; ask the candidate before adding unsupported gaps.
- [ ] Remove only duplicate, unsupported, or low-signal content listed in `REMOVE`.
- [ ] Re-export and inspect the final PDF or rendered resume for text selection, link preservation, and reading order.
"""


# ── Internal helpers ─────────────────────────────────────────────────────────

_TERM_ALIASES: dict[str, str] = {
    "js": "javascript",
    "ts": "typescript",
    "postgres": "postgresql",
    "postgresql": "postgresql",
    "react.js": "react",
    "node": "node.js",
    "nodejs": "node.js",
    "ci/cd": "continuous integration",
    "llm": "large language model",
    "genai": "generative ai",
    "ml": "machine learning",
    "ai": "artificial intelligence",
    "k8s": "kubernetes",
    "aws lambda": "serverless",
    "fast api": "fastapi",
}

_CATEGORY_KEYWORDS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("programming_languages", ("python", "typescript", "javascript", "java", "go", "golang", "rust", "c++", "c#", "sql")),
    ("frontend", ("react", "vue", "angular", "frontend", "front-end", "html", "css", "ui", "accessibility")),
    ("backend", ("api", "apis", "backend", "service", "services", "microservice", "fastapi", "django", "flask", "node.js", "server")),
    ("cloud_infrastructure", ("aws", "gcp", "azure", "kubernetes", "docker", "terraform", "serverless", "lambda", "cloud", "infrastructure")),
    ("data_ml_ai", ("machine learning", "ml", "ai", "llm", "generative ai", "data", "analytics", "model", "tensorflow", "pytorch")),
    ("databases", ("postgres", "postgresql", "mysql", "sqlite", "redis", "database", "sql", "nosql", "snowflake")),
    ("security", ("security", "compliance", "soc2", "soc 2", "auth", "oauth", "iam", "vulnerability")),
    ("testing_qa", ("test", "testing", "qa", "pytest", "unit test", "integration test", "ci/cd", "quality")),
    ("architecture_system_design", ("architecture", "system design", "distributed", "scalable", "reliability", "observability")),
    ("leadership_ownership", ("lead", "leadership", "mentor", "ownership", "roadmap", "stakeholder", "strategy")),
    ("communication_collaboration", ("communicat", "collaborat", "cross-functional", "partner", "customer", "sales")),
    ("education", ("degree", "bachelor", "master", "phd", "education", "certification", "certified")),
    ("experience_years", ("years", "yrs", "experience")),
    ("responsibilities", ("build", "develop", "design", "own", "ship", "maintain", "partner")),
)


def _flatten_requirement_value(value: object) -> list[str]:
    if value in (None, "", [], {}):
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        flattened: list[str] = []
        for key, nested in value.items():
            for item in _flatten_requirement_value(nested):
                flattened.append(f"{key}: {item}" if len(str(key)) <= 40 else item)
        return flattened
    if isinstance(value, (list, tuple, set)):
        flattened = []
        for item in value:
            flattened.extend(_flatten_requirement_value(item))
        return flattened
    return [str(value)]


def _split_requirement_items(text: str, source_label: str) -> list[str]:
    cleaned = str(text).replace("\r", "\n")
    if source_label in {"skills", "tools"} and len(cleaned) <= 160:
        pieces = re.split(r"[,;/|]+", cleaned)
    else:
        pieces = re.split(r"\n+|(?:^|\s)[•*-]\s+|(?<=[.!?])\s+(?=[A-Z0-9])", cleaned)
    items = [_clean_requirement_text(piece) for piece in pieces]
    return [item for item in items if _useful_requirement_text(item)]


def _requirements_from_description(text: str) -> list[tuple[str, str, str]]:
    candidates: list[tuple[str, str, str]] = []
    current_importance = "required"
    current_source = "description"
    section_cue = ""
    for raw_line in text.replace("\r", "\n").split("\n"):
        line = raw_line.strip()
        if not line:
            continue
        lowered = line.casefold().strip(":")
        if len(line) <= 80 and re.search(r"(requirements|qualifications|skills|responsibilities|what you|you have|preferred|nice|bonus)", lowered):
            section_cue = lowered
            current_importance = "preferred" if re.search(r"(preferred|nice|bonus|plus)", lowered) else "required"
            current_source = "preferred" if current_importance == "preferred" else ("responsibilities" if "responsib" in lowered or "what you" in lowered else "requirements")
            continue
        item_importance = current_importance
        if re.search(r"\b(preferred|nice to have|bonus|plus)\b", lowered):
            item_importance = "preferred"
        elif re.search(r"\b(required|must|required qualifications|minimum)\b", lowered):
            item_importance = "required"
        for item in _split_requirement_items(line, current_source):
            if _line_is_requirement_like(item, section_cue):
                candidates.append((item, item_importance, current_source))
    return candidates


def _clean_requirement_text(text: object) -> str:
    cleaned = re.sub(r"\s+", " ", str(text).strip(" \t\r\n-•*"))
    cleaned = re.sub(r"^(requirements?|qualifications?|responsibilities|skills?|preferred|nice to have):\s*", "", cleaned, flags=re.IGNORECASE)
    return cleaned.strip()


def _useful_requirement_text(text: str) -> bool:
    if len(text) < 3 or len(text) > 320:
        return False
    if text.count(" ") > 45:
        return False
    lowered = text.casefold()
    if lowered in {"about us", "benefits", "what we offer", "equal opportunity employer"}:
        return False
    return bool(re.search(r"[a-zA-Z]", text))


def _line_is_requirement_like(text: str, section_cue: str) -> bool:
    lowered = text.casefold()
    if section_cue and re.search(r"(requirements|qualifications|skills|responsibilities|preferred|nice|bonus|what you|you have)", section_cue):
        return True
    return bool(
        re.search(
            r"\b(experience|proficient|knowledge|familiar|build|design|develop|own|lead|manage|work with|skills?|required|must|responsible|ability)\b",
            lowered,
        )
    )


def _requirement_category(text: str, *, fallback: str) -> str:
    lowered = _normalize_text(text)
    best_category = ""
    best_hits = 0
    for category, needles in _CATEGORY_KEYWORDS:
        hits = sum(1 for needle in needles if _normalize_alias(needle) in lowered)
        if hits > best_hits:
            best_category = category
            best_hits = hits
    if best_category:
        return best_category
    if fallback in {"skills", "tools", "preferred", "requirements", "responsibilities"}:
        return fallback
    return "general"


def _requirement_keywords(text: str) -> tuple[str, ...]:
    normalized = _normalize_text(text)
    terms: list[str] = []
    known_terms = (*_HIGH_VALUE_TERMS, *(_TERM_ALIASES.keys()), *(_TERM_ALIASES.values()))
    for term in known_terms:
        normalized_term = _normalize_alias(term)
        if normalized_term and _text_has_term(normalized, normalized_term):
            terms.append(normalized_term)
    terms.extend(_keyword_terms(text))
    return tuple(dict.fromkeys(term for term in terms if term and term not in _STOP_WORDS))[:10]


def _normalized_requirement_terms(text: str, keywords: Sequence[str]) -> tuple[str, ...]:
    terms: list[str] = []
    terms.extend(_normalize_alias(term) for term in keywords)
    tokens = [token for token in _keyword_terms(text) if token not in _STOP_WORDS]
    terms.extend(_normalize_alias(token) for token in tokens[:8])
    if not terms and text.strip():
        terms.append(_normalize_text(text)[:80])
    return tuple(dict.fromkeys(term for term in terms if term))


def _keyword_terms(text: str) -> list[str]:
    normalized = _normalize_text(text)
    terms = []
    for token in _TOKEN_RE.findall(normalized):
        token = token.strip(".,;:!?()[]{}\"'")
        if len(token) >= 3 or token in {"go", "c#", "c++", "ai", "ml"}:
            terms.append(_normalize_alias(token))
    return terms


def _normalize_alias(term: str) -> str:
    normalized = _normalize_text(str(term).strip())
    return _TERM_ALIASES.get(normalized, normalized)


def _requirement_weight(importance: str, category: str) -> float:
    base = 0.9 if importance == "required" else 0.55
    if category in {"programming_languages", "backend", "cloud_infrastructure", "architecture_system_design", "leadership_ownership"}:
        base += 0.05
    return round(min(base, 1.0), 2)


def _evidence_type_for_category(category: str) -> str:
    if category in {"education", "experience_years"}:
        return "credential_or_duration"
    if category in {"leadership_ownership", "communication_collaboration", "responsibilities"}:
        return "responsibility_or_behavior"
    return "skill_or_experience"


def _clean_resume_line(text: str) -> str:
    return re.sub(r"\s+", " ", text.strip(" \t\r\n-•*"))


def _looks_like_section_heading(line: str) -> bool:
    stripped = line.strip(" :#")
    if not stripped or len(stripped) > 42:
        return False
    known = {"summary", "skills", "experience", "work experience", "projects", "education", "certifications", "accomplishments"}
    return stripped.casefold() in known or (stripped.isupper() and len(stripped.split()) <= 4)


def _claim_has_signal(line: str) -> bool:
    return bool(re.search(r"\b(built|created|designed|led|owned|improved|reduced|increased|delivered|managed|python|api|react|aws|data|sql|test)\b", line, re.IGNORECASE))


def _claim_source_item(line: str) -> str:
    match = re.match(r"([^:]{3,60}):", line)
    if match:
        return match.group(1).strip()
    return line[:64]


def _claim_confidence(line: str, terms: Sequence[str]) -> float:
    confidence = 0.55
    if terms:
        confidence += 0.2
    if re.search(r"\b(built|created|designed|led|owned|delivered|improved|managed|reduced|increased)\b", line, re.IGNORECASE):
        confidence += 0.15
    if re.search(r"\d+[%x]|\$|\b\d+\+?\s*(users|customers|engineers|requests|ms|seconds|hours|days)\b", line, re.IGNORECASE):
        confidence += 0.1
    return round(min(confidence, 0.95), 2)


def _claim_strength(line: str, terms: Sequence[str]) -> str:
    if _claim_confidence(line, terms) >= 0.85:
        return "strong"
    if terms:
        return "moderate"
    return "weak"


def _claim_match_score(requirement: ParsedRequirement, claim: ResumeClaim) -> tuple[float, ResumeClaim, tuple[str, ...]]:
    req_terms = set(requirement.normalized_terms)
    claim_terms = set(claim.normalized_terms)
    matched_terms = tuple(sorted(req_terms & claim_terms))
    if not req_terms:
        return 0.0, claim, ()
    overlap = len(matched_terms) / len(req_terms)
    category_bonus = 0.15 if requirement.category == claim.category else 0.0
    phrase_bonus = 0.25 if _text_has_term(_normalize_text(claim.text), _normalize_text(requirement.text)) else 0.0
    score = min(1.0, overlap + category_bonus + phrase_bonus)
    return round(score, 3), claim, matched_terms


def _coverage_label(score: float) -> str:
    if score >= 0.65:
        return "satisfied"
    if score >= 0.35:
        return "partial"
    if score > 0.0:
        return "weak"
    return "missing"


def _strength_label(score: float) -> str:
    if score >= 0.75:
        return "strong"
    if score >= 0.45:
        return "partial"
    if score > 0.0:
        return "weak"
    return "missing"


def _requirement_match_explanation(
    requirement: ParsedRequirement,
    claim: ResumeClaim | None,
    matched_terms: Sequence[str],
    coverage: str,
) -> str:
    if claim is None:
        return f"No resume claim clearly supports `{requirement.text}`."
    terms = ", ".join(matched_terms) or "adjacent category/context"
    return f"{coverage.title()} match: `{requirement.text}` maps to resume claim `{claim.text}` through {terms}."


def _evidence_explanation(requirement: ParsedRequirement, claim: ResumeClaim, matched_terms: Sequence[str], coverage: str) -> str:
    terms = ", ".join(matched_terms) or requirement.category.replace("_", " ")
    return f"The resume {coverage}ly supports this {requirement.importance} requirement via {terms} evidence in `{claim.source_section}`."


def _missing_explanation(requirement: ParsedRequirement, claim: ResumeClaim | None, coverage: str) -> str:
    if claim is None:
        return f"No clear resume claim was found for `{requirement.text}`."
    return f"The closest resume claim is weak/partial, so `{requirement.text}` is not clearly satisfied."


def _improvement_hint(requirement: ParsedRequirement, claim: ResumeClaim | None, coverage: str) -> str:
    if claim is None or coverage == "missing":
        return f"Add truthful evidence for {requirement.text} only if the experience exists; otherwise leave it out or build a relevant project."
    return f"Strengthen the existing `{claim.source_section}` evidence by making the {requirement.category.replace('_', ' ')} connection explicit if accurate."


def _analysis_confidence(
    requirements: Sequence[ParsedRequirement],
    claims: Sequence[ResumeClaim],
    evidence: Sequence[EvidenceMatch],
) -> float:
    if not requirements or not claims:
        return 0.35
    return round(min(0.95, 0.5 + min(len(evidence), 6) * 0.05 + min(len(requirements), 20) * 0.01), 2)


def _improvement_group(item: MissingRequirement) -> str:
    if item.importance == "required" and item.coverage == "missing":
        return "Highest Impact"
    if item.coverage == "partial":
        return "Quick Wins"
    if item.related_resume_claims:
        return "Rewrite Suggestions"
    if item.category in {"programming_languages", "cloud_infrastructure", "data_ml_ai", "databases"}:
        return "Missing Keywords"
    return "Missing Evidence"


def _target_section_for_gap(item: MissingRequirement, related_claims: Sequence[str], claims_by_id: dict[str, ResumeClaim]) -> str:
    for claim_id in related_claims:
        claim = claims_by_id.get(claim_id)
        if claim is not None:
            return claim.source_section
    if item.category in {"programming_languages", "cloud_infrastructure", "databases"}:
        return "skills"
    if item.category == "education":
        return "education"
    return "experience or projects"


def _suggested_wording(item: MissingRequirement, related_claims: Sequence[str], claims_by_id: dict[str, ResumeClaim]) -> str:
    for claim_id in related_claims:
        claim = claims_by_id.get(claim_id)
        if claim is not None:
            return f"Revise only if true: `{claim.text}` → add explicit context for {item.requirement_text} without changing the underlying fact."
    return f"If true, add a concise bullet showing real experience with {item.requirement_text}; otherwise do not add this claim."


def _recommendation_text(
    item: MissingRequirement,
    requirement: ParsedRequirement | None,
    related_claims: Sequence[str],
    claims_by_id: dict[str, ResumeClaim],
) -> str:
    del requirement
    if related_claims:
        claim = claims_by_id.get(related_claims[0])
        if claim is not None:
            return f"Clarify the existing `{claim.source_section}` claim so it visibly supports `{item.requirement_text}`."
    return f"Create or surface truthful evidence for `{item.requirement_text}` before treating this job as a strong match."


def _honesty_constraint(item: MissingRequirement, related_claims: Sequence[str]) -> str:
    if related_claims:
        return "Reframe adjacent experience only; do not claim direct ownership, production use, credentials, or metrics unless the resume source supports them."
    return "Do not invent this experience. Add it only after real work, learning, or a verified project creates evidence."


def _supported_evidence_wording(item: EvidenceMatch) -> str:
    if item.resume_claim_text:
        return f"Supported wording seed: `{item.resume_claim_text}`. Keep the fact intact; optionally foreground `{item.requirement_text or item.requirement}`."
    return "Keep the supported evidence prominent and factual."


def _structured_improvement_sections(scored_job: ScoredJob) -> str:
    analysis = scored_job.analysis
    improvements = tuple(getattr(analysis, "improvements", ()) or ())
    if not improvements:
        return "- No structured improvement recommendations were generated from parsed gaps."
    groups: dict[str, list[ResumeImprovement]] = {}
    for item in improvements:
        groups.setdefault(item.group, []).append(item)
    sections: list[str] = []
    for group, items in groups.items():
        sections.append(f"### {group}")
        for item in items:
            sections.append(f"- **{item.impact.title()} impact** — {item.recommendation}")
            sections.append(f"  - Requirement: `{item.requirement_id}`")
            if item.resume_claim_ids:
                sections.append(f"  - Resume claim(s): {', '.join(item.resume_claim_ids)}")
            if item.evidence_ids:
                sections.append(f"  - Evidence: {', '.join(item.evidence_ids)}")
            sections.append(f"  - Why: {item.why_it_matters}")
            sections.append(f"  - Suggested wording: {item.suggested_wording}")
            sections.append(f"  - Honesty constraint: {item.honesty_constraint}")
    return "\n".join(sections)


def _job_context(job: JobRecord) -> str:
    raw = job.raw
    parts: list[str] = []
    for key in (
        "job_description",
        "description",
        "summary",
        "company_description",
        "job_seniority",
        "employment_statuses",
        "requirements",
        "qualifications",
        "skills",
        "location",
    ):
        value = raw.get(key)
        if isinstance(value, str) and value.strip():
            parts.append(f"{key}: {value.strip()}")
        elif isinstance(value, (list, tuple)) and value:
            parts.append(f"{key}: {', '.join(str(item) for item in value)}")
        elif isinstance(value, dict) and value:
            parts.append(f"{key}: {value}")
    return "\n\n".join(parts).strip() or "No detailed job description was available; use only the job snapshot and scoring terms."


def _resume_lines(text: str) -> list[str]:
    return [line.strip() for line in text.splitlines() if line.strip()]


def _source_note(kind: str) -> str:
    if kind == "pdf":
        return "PDF text was extracted before analysis. Verify reading order, columns, links, and special characters against the original PDF."
    if kind == "latex":
        return "LaTeX source was parsed to text before analysis. Apply edits in the `.tex` source and recompile."
    return "Plain text or Markdown was reviewed directly. Export the final resume to a clean PDF for submission if needed."


def _job_keywords(job: JobRecord, scored_job: ScoredJob, target_roles: Sequence[str], target_industries: Sequence[str]) -> list[str]:
    ordered: list[str] = []
    for value in (*target_roles, *target_industries, *scored_job.matched_terms, *scored_job.missing_terms, *scored_job.missing_requirements):
        normalized = _normalize_text(str(value))
        if normalized and normalized not in ordered:
            ordered.append(normalized)
    counts: dict[str, int] = {}
    for token in _TOKEN_RE.findall(_job_text(job)):
        if len(token) < 3 or token in _STOP_WORDS:
            continue
        counts[token] = counts.get(token, 0) + 1
    for token, _count in sorted(counts.items(), key=lambda item: (-item[1], item[0])):
        if token not in ordered:
            ordered.append(token)
        if len(ordered) >= 18:
            break
    return ordered[:18]


def _keyword_evidence_rows(keywords: Sequence[str], resume_text: str) -> list[str]:
    resume_norm = _normalize_text(resume_text)
    resume_tokens = _tokenize(resume_norm)
    rows: list[str] = []
    for keyword in keywords:
        normalized = _normalize_text(str(keyword))
        if not normalized:
            continue
        keyword_tokens = _tokenize(normalized)
        if _text_has_term(resume_norm, normalized):
            status = "supported"
            action = "keep visible and use naturally"
        elif keyword_tokens and resume_tokens & keyword_tokens:
            status = "weakly supported"
            action = "clarify only if the candidate can point to real work"
        else:
            status = "not supported by resume text"
            action = "do not add unless verified by the candidate"
        rows.append(f"- **{keyword}** — `{status}`; {action}.")
    return rows


def _evidence_lines(lines: Sequence[str], terms: Sequence[str]) -> list[str]:
    normalized_terms = [_normalize_text(str(term)) for term in terms if str(term).strip()]
    selected: list[str] = []
    for line in lines:
        normalized_line = _normalize_text(line)
        if any(term and _contains_phrase(normalized_line, term) for term in normalized_terms):
            selected.append(line)
        elif len(selected) < 3 and len(line) >= 35:
            selected.append(line)
        if len(selected) >= 8:
            break
    return selected


def _rewrite_candidates(lines: Sequence[str]) -> list[str]:
    candidates: list[str] = []
    action_tokens = {"built", "created", "developed", "designed", "led", "launched", "managed", "improved", "reduced", "increased", "implemented"}
    for line in lines:
        tokens = _tokenize(_normalize_text(line))
        if len(line) >= 35 and (tokens & action_tokens):
            candidates.append(line)
        if len(candidates) >= 4:
            break
    if not candidates:
        candidates = [line for line in lines if len(line) >= 35][:3]
    return candidates


def _recommended_change_items(
    scored_job: ScoredJob,
    target_role: str,
    target_category: str,
    supported_rows: Sequence[str],
    unsupported_rows: Sequence[str],
) -> str:
    items = [
        f"- Tune the summary and first experience bullets toward `{target_role}` while preserving original facts.",
        f"- Place `{target_category}` evidence before less relevant experience when the resume already supports that category.",
        "- Convert responsibility-only bullets into action + scope + outcome bullets where a truthful outcome is available.",
    ]
    if supported_rows:
        items.append("- Surface supported keywords from the evidence table; these are safer because they already overlap the resume.")
    if unsupported_rows or scored_job.missing_requirements:
        items.append("- Treat unsupported terms as candidate questions, not resume edits. Add them only after verification.")
    return "\n".join(items)


def _rewrite_items(candidates: Sequence[str], target_role: str) -> str:
    if not candidates:
        return "- No safe bullet-level rewrite candidates were obvious from extracted text. Preserve content and ask the candidate for impact details."
    rows: list[str] = []
    for candidate in candidates:
        rows.append(
            "\n".join(
                [
                    f"- **Original:** {candidate}",
                    f"  - **Rewrite pattern:** {candidate} — emphasize relevance to `{target_role}` and append `[add measured impact if true]`.",
                    "  - **Reason:** preserves the original fact while prompting a verified outcome instead of inventing one.",
                ]
            )
        )
    return "\n".join(rows)


def _prioritization_items(lines: Sequence[str], keywords: Sequence[str]) -> str:
    evidence = _evidence_lines(lines, keywords)
    if not evidence:
        return "- Keep current ordering until the candidate identifies which project or role best matches the selected job."
    items = [f"- Prioritize this evidence near the top: {line}" for line in evidence[:5]]
    items.append("- De-prioritize older or less relevant entries only after targeted evidence is visible.")
    return "\n".join(items)


_TOKEN_RE = re.compile(r"[a-z0-9][a-z0-9+.#-]{1,}")


@dataclass(frozen=True)
class _ResumeIndex:
    text: str
    normalized: str
    tokens: frozenset[str]
    evidence_lines: tuple[str, ...]


@dataclass(frozen=True)
class _JobIndex:
    text: str
    tokens: frozenset[str]
    title_tokens: frozenset[str]


_STOP_WORDS: frozenset[str] = frozenset(
    {
        "about", "across", "after", "also", "and", "are", "based", "build", "can",
        "company", "customer", "customers", "data", "deliver", "design", "develop",
        "development", "experience", "for", "from", "have", "help", "into", "job",
        "lead", "new", "our", "own", "product", "products", "requirements", "role",
        "services", "team", "teams", "that", "the", "their", "this", "through",
        "to", "using", "with", "work", "working", "you", "your",
    }
)


_HIGH_VALUE_TERMS: tuple[str, ...] = (
    "python", "sql", "typescript", "javascript", "react", "vue", "angular", "node",
    "fastapi", "django", "flask", "java", "kotlin", "scala", "go", "golang", "rust",
    "c++", "c#", "ruby", "rails", "php", "swift", "objective-c", "aws", "gcp",
    "azure", "docker", "kubernetes", "terraform", "pulumi", "ci/cd", "github",
    "gitlab", "jenkins", "airflow", "spark", "dbt", "snowflake", "bigquery",
    "redshift", "postgres", "postgresql", "mysql", "mongodb", "redis", "kafka",
    "graphql", "grpc", "rest", "api", "apis", "microservices", "distributed systems",
    "machine learning", "ml", "ai", "llm", "rag", "nlp", "pytorch", "tensorflow",
    "scikit-learn", "pandas", "numpy", "analytics", "experimentation", "a/b",
    "observability", "monitoring", "security", "privacy", "hipaa", "soc2", "fintech",
    "healthcare", "clinical", "payments", "climate", "developer tools", "platform",
    "frontend", "backend", "full stack", "mobile", "ios", "android", "product",
    "design systems", "etl", "elt", "pipeline", "pipelines", "warehouse", "crm",
    "salesforce", "hubspot", "growth", "marketing", "sales", "operations",
)

# US state/territory -> region mapping
_US_REGIONS: dict[str, str] = {
    # Northeast
    "CT": "Northeast",
    "ME": "Northeast",
    "MA": "Northeast",
    "NH": "Northeast",
    "RI": "Northeast",
    "VT": "Northeast",
    "NJ": "Northeast",
    "NY": "Northeast",
    "PA": "Northeast",
    # Midwest
    "IL": "Midwest",
    "IN": "Midwest",
    "IA": "Midwest",
    "KS": "Midwest",
    "MI": "Midwest",
    "MN": "Midwest",
    "MO": "Midwest",
    "NE": "Midwest",
    "ND": "Midwest",
    "OH": "Midwest",
    "SD": "Midwest",
    "WI": "Midwest",
    # South
    "AL": "South",
    "AR": "South",
    "DE": "South",
    "DC": "South",
    "FL": "South",
    "GA": "South",
    "KY": "South",
    "LA": "South",
    "MD": "South",
    "MS": "South",
    "NC": "South",
    "OK": "South",
    "SC": "South",
    "TN": "South",
    "TX": "South",
    "VA": "South",
    "WV": "South",
    # West
    "AK": "West",
    "AZ": "West",
    "CA": "West",
    "CO": "West",
    "HI": "West",
    "ID": "West",
    "MT": "West",
    "NV": "West",
    "NM": "West",
    "OR": "West",
    "UT": "West",
    "WA": "West",
    "WY": "West",
}

_US_ABBREVS: frozenset[str] = frozenset(_US_REGIONS)
_US_STATE_NAMES: frozenset[str] = frozenset(
    {
        "alabama",
        "alaska",
        "arizona",
        "arkansas",
        "california",
        "colorado",
        "connecticut",
        "delaware",
        "district of columbia",
        "florida",
        "georgia",
        "hawaii",
        "idaho",
        "illinois",
        "indiana",
        "iowa",
        "kansas",
        "kentucky",
        "louisiana",
        "maine",
        "maryland",
        "massachusetts",
        "michigan",
        "minnesota",
        "mississippi",
        "missouri",
        "montana",
        "nebraska",
        "nevada",
        "new hampshire",
        "new jersey",
        "new mexico",
        "new york",
        "north carolina",
        "north dakota",
        "ohio",
        "oklahoma",
        "oregon",
        "pennsylvania",
        "rhode island",
        "south carolina",
        "south dakota",
        "tennessee",
        "texas",
        "utah",
        "vermont",
        "virginia",
        "washington",
        "west virginia",
        "wisconsin",
        "wyoming",
    }
)


def _job_text(job: JobRecord) -> str:
    """Assemble searchable text from normalized + raw job fields."""
    parts: list[str] = [
        job.title or "",
        job.description or "",
        job.source or "",
        " ".join(job.locations),
        " ".join(job.skills),
        " ".join(job.employment_statuses),
        " ".join(str(value) for value in job.digest.values() if value not in (None, "", [], {})),
    ]
    raw = job.raw

    for key in (
        "job_description",
        "description",
        "responsibilities",
        "requirements",
        "benefits",
        "company_name",
        "company",
        "company_description",
        "job_seniority",
    ):
        val = raw.get(key)
        if val is not None:
            if isinstance(val, str):
                parts.append(val)
            elif isinstance(val, (list, tuple)):
                parts.extend(str(v) for v in val)

    statuses = raw.get("employment_statuses")
    if isinstance(statuses, (list, tuple)):
        parts.extend(str(s) for s in statuses)
    elif isinstance(statuses, str):
        parts.append(statuses)

    skills = raw.get("skills")
    if isinstance(skills, (list, tuple)):
        parts.extend(str(s) for s in skills)
    elif isinstance(skills, str):
        parts.append(skills)

    for loc_key in ("location", "city", "state", "country"):
        loc_val = raw.get(loc_key)
        if isinstance(loc_val, str):
            parts.append(loc_val)

    return _normalize_text(" ".join(p for p in parts if p))


def _build_resume_index(analysis: UploadedResumeAnalysis) -> _ResumeIndex:
    text = analysis.text or ""
    normalized = _normalize_text(text)
    lines = tuple(
        line.strip()
        for line in re.split(r"[\r\n•]+", text)
        if len(line.strip()) >= 8
    )
    return _ResumeIndex(
        text=text,
        normalized=normalized,
        tokens=frozenset(_tokenize(normalized)),
        evidence_lines=lines[:80],
    )


def _build_job_index(job: JobRecord) -> _JobIndex:
    text = _job_text(job)
    return _JobIndex(
        text=text,
        tokens=frozenset(_tokenize(text)),
        title_tokens=frozenset(_tokenize(_normalize_text(job.title or ""))),
    )


def _score_resume_title_fit(job_index: _JobIndex, resume_index: _ResumeIndex) -> float:
    title_terms = {term for term in job_index.title_tokens if term not in _STOP_WORDS}
    if not title_terms:
        return 0.0
    overlap = len(title_terms & resume_index.tokens)
    ratio = overlap / len(title_terms)
    if ratio >= 0.75:
        return 12.0
    if ratio >= 0.5:
        return 8.0
    if ratio > 0:
        return 4.0
    return 0.0


def _job_requirement_terms(
    job: JobRecord,
    job_index: _JobIndex,
    target_industries: Sequence[str],
    keywords: Sequence[str],
) -> tuple[str, ...]:
    raw = job.raw
    terms: list[str] = []

    for source in (target_industries, keywords):
        for term in source:
            normalized = _normalize_requirement(term)
            if normalized and _text_has_term(job_index.text, normalized):
                terms.append(normalized)

    skills = raw.get("skills")
    if isinstance(skills, str):
        terms.extend(_split_skill_text(skills))
    elif isinstance(skills, (list, tuple)):
        terms.extend(str(skill) for skill in skills if str(skill).strip())
    terms.extend(job.skills)

    for term in _HIGH_VALUE_TERMS:
        if _text_has_term(job_index.text, term):
            terms.append(term)

    terms.extend(_important_requirement_tokens(job))
    normalized = [_normalize_requirement(term) for term in terms]
    filtered = [term for term in normalized if _is_requirement_term(term)]
    return tuple(dict.fromkeys(filtered))


def _split_skill_text(value: str) -> list[str]:
    return [part.strip() for part in re.split(r"[,;/|]+", value) if part.strip()]


def _important_requirement_tokens(job: JobRecord) -> tuple[str, ...]:
    raw = job.raw
    parts: list[str] = [job.description or ""]
    parts.extend(job.skills)
    parts.extend(job.employment_statuses)
    for key in ("requirements", "qualifications", "responsibilities", "job_description", "description", "job_text", "summary"):
        value = raw.get(key)
        if isinstance(value, str):
            parts.append(value)
        elif isinstance(value, (list, tuple)):
            parts.extend(str(item) for item in value)
    text = " ".join(parts)
    sentences = re.split(r"(?<=[.!?])\s+|[\r\n]+", text)
    cue_re = re.compile(r"\b(required|requires|requirement|must|need|needs|experience with|proficient|knowledge of|familiar|skills?)\b", re.IGNORECASE)
    cue_words = {"required", "requires", "requirement", "must", "need", "needs", "experience", "proficient", "knowledge", "familiar", "skill", "skills"}
    terms: list[str] = []
    for sentence in sentences:
        if not cue_re.search(sentence):
            continue
        normalized_sentence = _normalize_text(sentence)
        for token in _TOKEN_RE.findall(normalized_sentence):
            normalized = token.strip(".,;:!?()[]{}\"'")
            if len(normalized) >= 5 and normalized not in _STOP_WORDS and normalized not in cue_words:
                terms.append(normalized)
    return tuple(dict.fromkeys(terms[:12]))


def _normalize_requirement(value: str) -> str:
    return _normalize_text(str(value).strip(" -•\t\n\r"))


def _is_requirement_term(term: str) -> bool:
    if not term or term in _STOP_WORDS:
        return False
    if len(term) <= 1:
        return False
    tokens = _tokenize(term)
    return bool(tokens and any(token not in _STOP_WORDS for token in tokens))


def _split_supported_terms(
    requirement_terms: Sequence[str],
    resume_index: _ResumeIndex,
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    matched: list[str] = []
    missing: list[str] = []
    for term in requirement_terms:
        if _resume_supports_term(resume_index, term):
            matched.append(term)
        else:
            missing.append(term)
    return tuple(matched), tuple(missing)


def _resume_supports_term(resume_index: _ResumeIndex, term: str) -> bool:
    if _text_has_term(resume_index.normalized, term):
        return True
    tokens = _tokenize(term)
    meaningful = {token for token in tokens if token not in _STOP_WORDS}
    return bool(meaningful and meaningful <= resume_index.tokens)


def _score_requirement_coverage(matched: Sequence[str], requirements: Sequence[str]) -> float:
    if not requirements:
        return 0.0
    coverage = len(matched) / len(requirements)
    return min(35.0, 35.0 * coverage)

def _score_components(
    *,
    role_pts: float,
    industry_pts: float,
    kw_pts: float,
    requirement_pts: float,
    resume_pts: float,
    requirement_count: int,
    matched_requirement_count: int,
) -> tuple[ScoreComponent, ...]:
    return (
        ScoreComponent("Role fit", round(role_pts, 1), 40.0, "Job title overlap with target roles or resume title signals."),
        ScoreComponent("Category fit", round(industry_pts, 1), 30.0, "Industry/category terms found in job or company context."),
        ScoreComponent("Keyword relevance", round(kw_pts, 1), 20.0, "Optional filter keywords found in the job record."),
        ScoreComponent(
            "Requirement evidence",
            round(requirement_pts, 1),
            35.0,
            f"{matched_requirement_count}/{requirement_count or 0} extracted requirements have direct resume support.",
        ),
        ScoreComponent("Resume overlap", round(resume_pts, 1), 10.0, "Token overlap between uploaded resume text and job text."),
    )


def _evidence_matches_for_terms(
    resume_index: _ResumeIndex,
    matched_terms: Sequence[str],
    *,
    requirement_count: int,
) -> tuple[EvidenceMatch, ...]:
    if not matched_terms or requirement_count <= 0:
        return ()
    per_requirement_points = round(35.0 / requirement_count, 1)
    matches: list[EvidenceMatch] = []
    for term in matched_terms[:12]:
        line, confidence = _evidence_line_for_term(resume_index, term)
        if not line:
            continue
        matches.append(
            EvidenceMatch(
                requirement=term,
                resume_excerpt=line,
                contribution_score=per_requirement_points,
                confidence=confidence,
                matched_keywords=_evidence_keywords_for_term(term, line),
                explanation=(
                    f"Matched `{term}` to a resume excerpt. This evidence contributes "
                    f"{per_requirement_points:.1f} points to the requirement-evidence component."
                ),
            )
        )
        if len(matches) >= 8:
            break
    matches.sort(key=lambda match: (-match.contribution_score, -match.confidence, match.requirement))
    return tuple(matches)


def _evidence_line_for_term(resume_index: _ResumeIndex, term: str) -> tuple[str, float]:
    term_tokens = _tokenize(term)
    for line in resume_index.evidence_lines:
        normalized = _normalize_text(line)
        if _text_has_term(normalized, term):
            return line, 1.0
        if term_tokens and term_tokens <= _tokenize(normalized):
            return line, 0.85
    return "", 0.0


def _evidence_keywords_for_term(term: str, line: str) -> tuple[str, ...]:
    normalized_line = _normalize_text(line)
    keywords: list[str] = []
    if _text_has_term(normalized_line, term):
        keywords.append(term)
    for token in _tokenize(term):
        if token not in _STOP_WORDS and _text_has_term(normalized_line, token):
            keywords.append(token)
    return tuple(dict.fromkeys(keywords[:6]))


def _evidence_for_terms(resume_index: _ResumeIndex, matched_terms: Sequence[str]) -> tuple[str, ...]:
    evidence: list[str] = []
    for term in matched_terms[:12]:
        line, _confidence = _evidence_line_for_term(resume_index, term)
        if line:
            evidence.append(line)
        if len(evidence) >= 4:
            break
    if evidence:
        return tuple(dict.fromkeys(evidence))
    return ("No direct resume evidence found for the extracted job requirements.",)


def _key_strengths(matched_terms: Sequence[str], evidence: Sequence[str]) -> tuple[str, ...]:
    if not matched_terms:
        return ("No strong requirement matches found in the uploaded resume.",)
    strengths = [f"Resume supports {term}" for term in matched_terms[:5]]
    if evidence and not evidence[0].startswith("No direct"):
        strengths.append("Direct evidence found in uploaded resume text.")
    return tuple(strengths[:6])


def _concerns_for_job(
    job: JobRecord,
    resume_index: _ResumeIndex,
    missing_requirements: Sequence[str],
) -> tuple[str, ...]:
    job_text = _job_text(job)
    concerns: list[str] = []
    if missing_requirements:
        concerns.append("Missing direct resume evidence for " + ", ".join(missing_requirements[:5]) + ".")
    if len(resume_index.tokens) < 50:
        concerns.append("Uploaded resume text is short; extracted evidence may be incomplete.")
    if _contains_phrase(job_text, "years") and not _years_in_text(resume_index.text):
        concerns.append("Job mentions years of experience but the resume text does not clearly quantify tenure.")
    if (_contains_phrase(job_text, "degree") or _contains_phrase(job_text, "bachelor") or _contains_phrase(job_text, "master")) and not (
        _contains_phrase(resume_index.normalized, "degree")
        or _contains_phrase(resume_index.normalized, "bachelor")
        or _contains_phrase(resume_index.normalized, "master")
    ):
        concerns.append("Job mentions education requirements not clearly evidenced in the resume text.")
    return tuple(concerns) if concerns else ("No major concerns detected from the available resume text.",)


def _category_fit(category: str, target_industries: Sequence[str], resume_index: _ResumeIndex) -> str:
    if category == "Uncategorized":
        if not target_industries:
            return "No target industry supplied; category fit is based on resume and job requirement evidence."
        return "No clear target-industry signal found in this job."
    category_norm = _normalize_text(category)
    if _resume_supports_term(resume_index, category_norm):
        return f"Strong category fit: resume and job both reference {category}."
    return f"Transferable category fit: job matches {category}, but resume evidence is indirect."


def _match_explanation(
    *,
    score: float,
    role_pts: float,
    industry_pts: float,
    kw_pts: float,
    requirement_pts: float,
    resume_pts: float,
    matched_count: int,
    requirement_count: int,
    category_fit: str,
) -> str:
    if score >= 75:
        tier = "Strong"
    elif score >= 45:
        tier = "Moderate"
    else:
        tier = "Weak"
    return (
        f"{tier} match: {matched_count}/{requirement_count or 0} extracted requirements have resume evidence. "
        f"Components — role {role_pts:.0f}, category {industry_pts:.0f}, keywords {kw_pts:.0f}, "
        f"requirements {requirement_pts:.0f}, resume overlap {resume_pts:.0f}. {category_fit}"
    )


def _location_text(job: JobRecord) -> str:
    """Extract location-related text from job raw fields."""
    raw = job.raw
    parts: list[str] = []
    for key in ("location", "city", "state", "country", "job_location", "job_city"):
        val = raw.get(key)
        if isinstance(val, str) and val.strip():
            parts.append(val.strip())
    return " ".join(parts)


def _analyze_region(job: JobRecord) -> tuple[str, str]:
    """Determine (region, remote_label) for a job.

    Region is one of: Remote, Northeast, Midwest, South, West, International, Unknown.
    Remote label is: Remote, On-site, Hybrid, Unknown.
    """
    # Remote label from normalized remote field
    remote_flag = job.remote
    raw = job.raw

    # Determine remote_label from raw employment_statuses or remote field
    employment_statuses = raw.get("employment_statuses")
    if isinstance(employment_statuses, str) and employment_statuses.lower() in ("remote", "fully remote"):
        remote_label = "Remote"
    elif isinstance(employment_statuses, (list, tuple)):
        es_lower = [s.lower() for s in employment_statuses if isinstance(s, str)]
        if any("remote" in s for s in es_lower):
            remote_label = "Remote"
        elif any("hybrid" in s for s in es_lower):
            remote_label = "Hybrid"
        else:
            remote_label = "On-site"
    elif remote_flag == 1:
        remote_label = "Remote"
    elif remote_flag == 0:
        remote_label = "On-site"
    else:
        # Check raw remote field
        raw_remote = raw.get("remote")
        if isinstance(raw_remote, bool):
            remote_label = "Remote" if raw_remote else "On-site"
        elif isinstance(raw_remote, str):
            rl = raw_remote.lower()
            if rl in ("remote", "yes", "true", "1"):
                remote_label = "Remote"
            elif rl in ("hybrid", "partial"):
                remote_label = "Hybrid"
            elif rl in ("on-site", "onsite", "no", "false", "0"):
                remote_label = "On-site"
            else:
                remote_label = "Unknown"
        else:
            remote_label = "Unknown"

    # If remote, region is "Remote" — but also try to add location context
    if remote_label == "Remote":
        # Check for a known region from location text even for remote jobs
        loc = _location_text(job)
        found = _region_from_text(loc)
        if found:
            region = f"Remote - {found}"
        elif job.country_code and job.country_code.upper() != "US":
            region = "Remote - International"
        else:
            region = "Remote"
        return region, remote_label

    # Geographic region
    cc = (job.country_code or "").strip().upper()

    if cc == "US":
        loc = _location_text(job)
        region = _region_from_text(loc)
        if region is None:
            # Try raw job fields for state/city info
            raw_state = raw.get("state")
            if isinstance(raw_state, str):
                abbr = raw_state.strip().upper()[:2]
                region = _US_REGIONS.get(abbr) or _region_from_text(raw_state.lower())
            if region is None:
                region = "Unknown"
        return region, remote_label

    if cc:
        return "International", remote_label

    # No country code — try location text
    loc = _location_text(job)
    region = _region_from_text(loc)
    return (region or "Unknown"), remote_label


def _region_from_text(text: str) -> str | None:
    """Scan free-text location for a US state -> region mapping."""
    normalized = _normalize_text(text)

    # Try state abbreviations (e.g. "CA", "NY", "san francisco, ca")
    words = normalized.split()
    for word in words:
        word = word.strip(" ,;.-()")
        if word.upper() in _US_ABBREVS:
            return _US_REGIONS[word.upper()]

    # Try full state names
    for state_name, region in _US_REGIONS.items():
        # state_name is the abbreviation; we need to check the full names list
        pass

    for state_full in _US_STATE_NAMES:
        if state_full in normalized:
            # Find the abbreviation for this full state name
            for abbr, region in _US_REGIONS.items():
                if _full_state_name(abbr) == state_full:
                    return region

    return None


def _full_state_name(abbrev: str) -> str:
    """Map a US state abbreviation to its full lowercase name."""
    names: dict[str, str] = {
        "AL": "alabama", "AK": "alaska", "AZ": "arizona", "AR": "arkansas",
        "CA": "california", "CO": "colorado", "CT": "connecticut",
        "DE": "delaware", "DC": "district of columbia", "FL": "florida",
        "GA": "georgia", "HI": "hawaii", "ID": "idaho", "IL": "illinois",
        "IN": "indiana", "IA": "iowa", "KS": "kansas", "KY": "kentucky",
        "LA": "louisiana", "ME": "maine", "MD": "maryland", "MA": "massachusetts",
        "MI": "michigan", "MN": "minnesota", "MS": "mississippi", "MO": "missouri",
        "MT": "montana", "NE": "nebraska", "NV": "nevada", "NH": "new hampshire",
        "NJ": "new jersey", "NM": "new mexico", "NY": "new york",
        "NC": "north carolina", "ND": "north dakota", "OH": "ohio",
        "OK": "oklahoma", "OR": "oregon", "PA": "pennsylvania",
        "RI": "rhode island", "SC": "south carolina", "SD": "south dakota",
        "TN": "tennessee", "TX": "texas", "UT": "utah", "VT": "vermont",
        "VA": "virginia", "WA": "washington", "WV": "west virginia",
        "WI": "wisconsin", "WY": "wyoming",
    }
    return names.get(abbrev.upper(), "")


def _categorize_job(job: JobRecord, target_industries: Sequence[str]) -> str:
    """Assign an industry category to a job.

    Priority:
      1. A target_industry keyword found in company name, company domain, or company description
      2. A target_industry keyword found in job title or job description
      3. Keyword-based inference from the raw ``skills`` or ``job_seniority`` fields
      4. ``Uncategorized``
    """
    if not target_industries:
        return "Uncategorized"

    text = _job_text(job)
    norm_industries = _normalize_text_set(target_industries)
    matched_in_text = _which_match(norm_industries, text)

    # Priority 1: company-level fields → strong signal
    raw = job.raw
    company_hint = _normalize_text(" ".join(
        str(raw.get(k, "")) for k in ("company_name", "company", "company_description")
        if raw.get(k)
    ))
    matched_company = _which_match(norm_industries, company_hint)
    if matched_company:
        return max(matched_company, key=len)  # most specific industry name

    # Priority 2: title / description
    if matched_in_text:
        return max(matched_in_text, key=len)

    # Priority 3: skills and seniority fields
    extra_text = ""
    skills = raw.get("skills")
    if isinstance(skills, str):
        extra_text += " " + skills
    elif isinstance(skills, (list, tuple)):
        extra_text += " " + " ".join(str(s) for s in skills)
    seniority = raw.get("job_seniority")
    if isinstance(seniority, str):
        extra_text += " " + seniority

    if extra_text:
        matched_extra = _which_match(norm_industries, _normalize_text(extra_text))
        if matched_extra:
            return max(matched_extra, key=len)

    return "Uncategorized"


def _score_role_fit(title: str | None, target_roles: Sequence[str]) -> float:
    """Score 0-40 for role fit between the job title and target roles."""
    if not title or not target_roles:
        return 0.0

    title_norm = _normalize_text(title)
    title_tokens = _tokenize(title_norm)

    best = 0.0
    for role in target_roles:
        role_norm = _normalize_text(role)
        role_tokens = _tokenize(role_norm)

        # Exact normalized match -> 40
        if title_norm == role_norm:
            return 40.0

        # All role tokens in title -> 35
        if role_tokens and role_tokens <= title_tokens:
            best = max(best, 35.0)
            continue

        # Partial overlap
        overlap = len(title_tokens & role_tokens)
        if overlap > 0:
            total = len(role_tokens)
            ratio = overlap / total
            if ratio >= 0.5:
                best = max(best, 20.0 + 15.0 * ratio)
            else:
                best = max(best, 5.0 + 15.0 * ratio)

    return min(best, 40.0)


def _score_industry(job: JobRecord, target_industries: Sequence[str]) -> float:
    """Score 0-30 for industry alignment."""
    if not target_industries:
        return 0.0

    text = _job_text(job)
    norm_industries = _normalize_text_set(target_industries)
    matched = _which_match(norm_industries, text)

    if not matched:
        return 0.0

    # Count distinct industry matches
    raw = job.raw
    company_text = _normalize_text(" ".join(
        str(raw.get(k, "")) for k in ("company_name", "company", "company_description")
        if raw.get(k)
    ))

    company_hits = sum(1 for ind in norm_industries if _text_has_term(company_text, ind))
    text_hits = sum(1 for ind in norm_industries if _text_has_term(text, ind))

    if company_hits >= 2:
        return 30.0
    if company_hits == 1:
        return 25.0 if text_hits >= 2 else 20.0
    if text_hits >= 3:
        return 25.0
    if text_hits >= 1:
        return 15.0

    return 0.0


def _score_keywords(job: JobRecord, keywords: Sequence[str]) -> float:
    """Score 0-20 for optional keyword relevance."""
    if not keywords:
        return 0.0

    text = _job_text(job)
    norm_kw = _normalize_text_set(keywords)

    hits = sum(1 for kw in norm_kw if _text_has_term(text, kw))
    ratio = hits / len(norm_kw)
    return min(ratio * 20.0, 20.0)


def _score_resume_overlap(job: JobRecord, resume_tokens: set[str]) -> float:
    """Score 0-10 based on token overlap between resume text and job text."""
    if not resume_tokens:
        return 0.0

    jt = _tokenize(_job_text(job))
    if not jt:
        return 0.0

    overlap = len(resume_tokens & jt)
    total = len(jt)
    ratio = overlap / total

    # Scale: 0%→0, 2%→1, 5%→3, 10%→5, 20%→8, 30%+→10
    if ratio >= 0.30:
        return 10.0
    if ratio >= 0.20:
        return 8.0
    if ratio >= 0.10:
        return 5.0
    if ratio >= 0.05:
        return 3.0
    if ratio >= 0.02:
        return 1.0
    return 0.0


def _matched_terms(
    job: JobRecord,
    target_roles: Sequence[str],
    target_industries: Sequence[str],
    keywords: Sequence[str],
) -> tuple[str, ...]:
    """Collect all terms (role, industry, keyword) found in the job text."""
    text = _job_text(job)
    matched: list[str] = []

    for role in target_roles:
        role_norm = _normalize_text(role)
        role_tokens = _tokenize(role_norm)
        if _text_has_term(text, role_norm):
            matched.append(role_norm)
            continue
        common = sorted(token for token in (job.title and _tokenize(_normalize_text(job.title)) or set()) & role_tokens if token not in _STOP_WORDS)
        matched.extend(common)
    # Industries
    norm_ind = _normalize_text_set(target_industries)
    for ind in norm_ind:
        if _text_has_term(text, ind):
            matched.append(ind)

    # Keywords
    norm_kw = _normalize_text_set(keywords)
    for kw in norm_kw:
        if _text_has_term(text, kw):
            matched.append(kw)

    return tuple(dict.fromkeys(matched))


def _missing_terms(
    job: JobRecord,
    target_roles: Sequence[str],
    target_industries: Sequence[str],
    keywords: Sequence[str],
) -> tuple[str, ...]:
    """Collect target search terms that were NOT found in the job."""
    text = _job_text(job)
    missing: list[str] = []

    for role in target_roles:
        role_norm = _normalize_text(role)
        if not _text_has_term(text, role_norm):
            missing.append(role)

    for ind in target_industries:
        ind_norm = _normalize_text(ind)
        if not _text_has_term(text, ind_norm):
            missing.append(ind)

    for kw in keywords:
        kw_norm = _normalize_text(kw)
        if not _text_has_term(text, kw_norm):
            missing.append(kw)

    return tuple(dict.fromkeys(missing))


# ── Text utilities ──────────────────────────────────────────────────────────


def _normalize_text(value: str) -> str:
    """Lowercase, ASCII-fold, collapse whitespace."""
    import unicodedata
    normalized = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode("ascii")
    return " ".join(normalized.lower().split())


def _normalize_text_set(values: Sequence[str]) -> set[str]:
    """Normalize each string in a sequence and return as a set."""
    return {_normalize_text(v) for v in values if v.strip()}


def _tokenize(text: str) -> set[str]:
    """Extract normalized search tokens and trim surrounding punctuation."""
    return {token for token in (match.strip(".,;:!?()[]{}\"'") for match in _TOKEN_RE.findall(text)) if token}


def _contains_phrase(text: str, phrase: str) -> bool:
    """Check if *phrase* appears as a substring of *text*."""
    if not phrase:
        return False
    return phrase in text


def _text_has_term(text: str, term: str) -> bool:
    """Match multi-word phrases by phrase and one-token terms by token."""
    if not term:
        return False
    term_tokens = _tokenize(term)
    if len(term_tokens) == 1 and term in term_tokens:
        return next(iter(term_tokens)) in _tokenize(text)
    return _contains_phrase(text, term)


def _which_match(candidates: set[str], text: str) -> list[str]:
    """Return all candidates that appear in *text*."""
    return [c for c in candidates if _text_has_term(text, c)]


def _years_in_text(text: str) -> bool:
    """Check whether a number + ``years`` pattern appears in text."""
    return bool(re.search(r"\d+\s*years?", text))
