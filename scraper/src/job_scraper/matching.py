"""Deterministic resume-to-job scoring and market analysis.

Pure keyword-based scoring — no network calls, no LLM inference.
Score range 0-100 based on role fit, industry alignment, keyword relevance,
and resume-text overlap with the job description.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Sequence

from job_scraper.storage import JobRecord
from job_scraper.resume_uploads import UploadedResumeAnalysis

# ── Public dataclasses ───────────────────────────────────────────────────────


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
        score = min(100.0, role_pts + industry_pts + kw_pts + requirement_pts + resume_pts)

        category = _categorize_job(job, target_industries)
        matched = _matched_terms(job, target_roles, target_industries, keywords)
        region, remote_label = _analyze_region(job)
        missing = _missing_terms(job, target_roles, target_industries, keywords)
        evidence = _evidence_for_terms(resume_index, matched_requirements)
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
            )
        )

    scored.sort(key=lambda s: (-s.score, s.job.date_posted or "", s.job.discovered_at or "", s.job.theirstack_id))
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


def build_improvement_prompt(
    scored_job: ScoredJob,
    analysis: UploadedResumeAnalysis,
    *,
    target_roles: Sequence[str],
    target_industries: Sequence[str],
) -> str:
    """Build a comprehensive resume-improvement prompt for one scored job.

    The prompt covers:
      1. LaTeX / PDF input guidance (based on the uploaded file kind)
      2. Role fit assessment
      3. Missing skills / gaps
      4. Industry alignment
      5. Evidence gaps
      6. Step-by-step revision prompts
    """
    job = scored_job.job
    job_text = _job_text(job)

    # ── Format guidance ──────────────────────────────────────────────────
    format_map = {
        "pdf": (
            "# PDF Input Guidance\n"
            "Your resume was uploaded as a **PDF**. The text extracted below was used for analysis. "
            "When revising, edit the source document (Word/InDesign/LaTeX) rather than the PDF, "
            "then re-export to PDF for submission."
        ),
        "latex": (
            "# LaTeX Input Guidance\n"
            "Your resume was uploaded as a **LaTeX / .tex** file. "
            "Edit the LaTeX source directly, recompiling to PDF after changes."
        ),
        "text": (
            "# Plain Text Input Guidance\n"
            "Your resume was uploaded as **plain text**. Consider converting to a PDF with a clean "
            "layout (e.g. LaTeX template) before submitting to employers."
        ),
    }
    format_guide = format_map.get(
        analysis.kind,
        "# Input Guidance\nUploaded resume analyzed as plain text.",
    )

    # ── Role/category assessment ─────────────────────────────────────────
    title_norm = _normalize_text(job.title or "")
    role_norm = " ".join(_normalize_text(r) for r in target_roles)
    role_match_tokens = _tokenize(role_norm) & _tokenize(title_norm)
    role_fit_pct = _score_role_fit(job.title, target_roles) / 40.0 * 100 if target_roles else 0.0

    if target_roles and role_fit_pct >= 80:
        role_assessment = f"Strong target-role fit ({role_fit_pct:.0f}%) for \"{job.title or 'N/A'}\"."
    elif target_roles and role_fit_pct >= 40:
        role_assessment = f"Moderate target-role fit ({role_fit_pct:.0f}%). Emphasize: {', '.join(sorted(role_match_tokens)) if role_match_tokens else 'transferable role evidence'}."
    elif target_roles:
        role_assessment = f"Weak target-role fit ({role_fit_pct:.0f}%). Reframe transferable experience toward \"{job.title or 'N/A'}\"."
    else:
        role_assessment = "No target roles supplied; ranking is driven by job requirements supported by the uploaded resume."

    # ── Structured match reasoning ───────────────────────────────────────
    strengths_section = "## Key Strengths\n" + "\n".join(f"- {term}" for term in scored_job.key_strengths)
    missing = scored_job.missing_requirements
    if missing:
        missing_block = "\n".join(f"- **{term}** — add only if your resume has truthful supporting evidence." for term in missing[:12])
        missing_section = f"## Missing Requirements / Missing Skills & Keywords\n{missing_block}"
    else:
        missing_section = "## Missing Requirements / Missing Skills & Keywords\nNo missing requirements detected from extracted job signals."

    evidence_section = "## Relevant Resume Evidence\n" + "\n".join(
        f"- {line}" for line in scored_job.relevant_resume_evidence
    )
    concerns_section = "## Concerns\n" + "\n".join(f"- {concern}" for concern in scored_job.concerns)
    industry_section = f"## Category Fit\n{scored_job.category_fit}"

    # ── Step-by-step revision prompts ────────────────────────────────────
    revision_steps: list[str] = []
    if role_match_tokens:
        revision_steps.append(f"1. **Highlight role keywords.** Ensure \"{', '.join(sorted(role_match_tokens))}\" appear prominently in your summary and experience sections.")
    else:
        revision_steps.append("1. **Lead with strongest evidence.** Move the most relevant supported skills and accomplishments into the first half of the resume.")
    if missing:
        revision_steps.append(f"2. **Close real gaps.** If truthful, add evidence for: {', '.join(missing[:8])}.")
    else:
        revision_steps.append("2. **Preserve requirement coverage.** Keep the matched skills visible and concrete.")
    if scored_job.category == "Uncategorized" or scored_job.category_fit.startswith("Transferable"):
        revision_steps.append("3. **Strengthen category signal.** Add domain-specific outcomes, users, metrics, or regulated-context details where truthful.")
    revision_steps.append("4. **Quantify achievements.** Replace vague claims with numbers: '% improvement', 'team size', 'latency', 'revenue', or 'cost' impact.")
    revision_steps.append(f"5. **Proofread for context.** Re-read the job description at {job.final_url or job.url or '(URL not available)'} and align bullet order with its listed requirements.")

    revision_block = "\n".join(revision_steps)

    # ── Assemble prompt ──────────────────────────────────────────────────
    region_info = f"**Region:** {scored_job.region}  \n**Work model:** {scored_job.remote_label}" if scored_job.region != "Unknown" else ""

    return f"""# Resume Improvement Prompt — {job.title or 'Untitled Role'}

## Job Snapshot
- **Title:** {job.title or 'N/A'}
- **Company:** {job.company or 'N/A'}
- **Score:** {scored_job.score:.1f}/100 — {scored_job.explanation}
- **Category:** {scored_job.category}
{region_info}

{format_guide}

---

{role_assessment}

---

{strengths_section}

---

{missing_section}

---

{industry_section}

---

{evidence_section}

---

{concerns_section}

---

## Recommended Revision Steps
{revision_block}
"""


# ── Internal helpers ─────────────────────────────────────────────────────────

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
    parts: list[str] = [job.title or ""]
    raw = job.raw

    for key in ("job_description", "description", "company_name", "company", "company_description", "job_seniority"):
        val = raw.get(key)
        if val is not None:
            if isinstance(val, str):
                parts.append(val)
            elif isinstance(val, (list, tuple)):
                parts.extend(str(v) for v in val)

    # employment_statuses could be a list like ["Full-time", "Contract"]
    statuses = raw.get("employment_statuses")
    if isinstance(statuses, (list, tuple)):
        parts.extend(str(s) for s in statuses)
    elif isinstance(statuses, str):
        parts.append(statuses)

    # skills could be a list or comma-separated string
    skills = raw.get("skills")
    if isinstance(skills, (list, tuple)):
        parts.extend(str(s) for s in skills)
    elif isinstance(skills, str):
        parts.append(skills)

    # location info
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
    parts: list[str] = []
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


def _evidence_for_terms(resume_index: _ResumeIndex, matched_terms: Sequence[str]) -> tuple[str, ...]:
    evidence: list[str] = []
    for term in matched_terms[:12]:
        term_tokens = _tokenize(term)
        for line in resume_index.evidence_lines:
            normalized = _normalize_text(line)
            if _text_has_term(normalized, term) or (term_tokens and term_tokens <= _tokenize(normalized)):
                evidence.append(line)
                break
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
