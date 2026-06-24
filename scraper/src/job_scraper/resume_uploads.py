from __future__ import annotations

import json
import re
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
from typing import Literal

from pypdf import PdfReader

from job_scraper.applications import _merged_job_mapping
from job_scraper.storage import JobRecord

MAX_UPLOAD_BYTES = 3_000_000
MAX_PROMPT_RESUME_CHARS = 12_000
SUPPORTED_RESUME_SUFFIXES: tuple[str, ...] = (".pdf", ".tex", ".latex", ".txt", ".md")

_COMMON_HEADINGS = {
    "summary",
    "experience",
    "work experience",
    "projects",
    "education",
    "skills",
    "certifications",
}


class ResumeUploadError(ValueError):
    pass


@dataclass(frozen=True)
class UploadedResumeAnalysis:
    filename: str
    kind: Literal["pdf", "latex", "text"]
    text: str
    facts_markdown: str


def analyze_resume_upload(filename: str, content: bytes) -> UploadedResumeAnalysis:
    if not filename:
        raise ResumeUploadError("Resume upload filename is required")
    if len(content) > MAX_UPLOAD_BYTES:
        raise ResumeUploadError("Resume upload is too large; maximum size is 3000000 bytes")

    suffix = Path(filename).suffix.lower()
    if suffix == ".pdf":
        kind: Literal["pdf", "latex", "text"] = "pdf"
        text = _extract_pdf_text(content)
    elif suffix in {".tex", ".latex"}:
        kind = "latex"
        text = _extract_latex_text(content.decode("utf-8", errors="replace"))
    elif suffix in {".txt", ".md"}:
        kind = "text"
        text = _collapse_text_lines(content.decode("utf-8", errors="replace"))
    else:
        raise ResumeUploadError(f"Unsupported resume upload type: {suffix or '<none>'}")

    if not text.strip():
        raise ResumeUploadError("Uploaded resume did not contain extractable text")

    return UploadedResumeAnalysis(
        filename=filename,
        kind=kind,
        text=text,
        facts_markdown=_facts_markdown(filename=filename, kind=kind, text=text),
    )


def build_tailored_resume_prompt(*, job: JobRecord, industry: str, analysis: UploadedResumeAnalysis) -> str:
    industry = industry.strip()
    if not industry:
        raise ResumeUploadError("Target industry is required")

    job_json = json.dumps(_merged_job_mapping(job), default=str, sort_keys=True, separators=(",", ":"))
    if len(job_json) > 20_000:
        job_json = f"{job_json[:20_000]}\n... [truncated]"

    resume_text = analysis.text
    if len(resume_text) > MAX_PROMPT_RESUME_CHARS:
        resume_text = f"{resume_text[:MAX_PROMPT_RESUME_CHARS]}\n... [truncated]"

    format_guidance = {
        "pdf": (
            "The resume was uploaded as a PDF. Analyze the extracted text below, note any parsing/order risks, "
            "and recommend edits to the source document before re-exporting to PDF."
        ),
        "latex": (
            "The resume was uploaded as LaTeX. Treat the extracted text as semantic content and make recommendations "
            "that can be applied safely to the `.tex` source without unnecessary template churn."
        ),
        "text": (
            "The resume was uploaded as plain text or Markdown. Review the text directly and include formatting / ATS "
            "guidance for producing the final submission file."
        ),
    }.get(analysis.kind, "Review the uploaded resume text directly.")

    return f"""# Resume Review Report Generator — {job.title or 'Target Role'}

You are an expert resume reviewer and technical hiring strategist. Produce a verbose Markdown report for tailoring the uploaded resume to the selected job and target industry. The report must be detailed enough for another writing or coding agent to implement step by step.

## Non-Negotiable Rules
- Use only facts present in the uploaded resume and job context. Do not invent employers, dates, titles, degrees, certifications, tools, metrics, or contact details.
- Preserve nearly all existing resume content unless you explicitly say to change, add, remove, or reorder it.
- Prefer precise targeted edits over broad rewrites.
- Rewritten bullets must preserve the original claim and seniority. Use `[add metric if true]` placeholders instead of fabricated numbers.
- Separate missing job/industry keywords from evidence the resume actually supports.
- Warn against edits that would make the resume worse: keyword stuffing, inflated claims, generic phrasing, deleting differentiating projects, or damaging ATS readability.
- Return Markdown only, suitable to save as a `.md` file.

## Required Markdown Report Structure
Use these headings:
1. `# Resume Review Report — {job.title or 'Target Role'}`
2. `## Executive Summary`
3. `## Source and Parsing Notes`
4. `## What the Resume Does Well`
5. `## Weaknesses and Risks`
6. `## What to Preserve`
7. `## Recommended Changes`
8. `## Tailoring Plan for {industry}`
9. `## Specific Edits and Rewritten Bullets`
10. `## Missing Keywords and Evidence`
11. `## Structure, Formatting, and ATS Feedback`
12. `## Project and Experience Prioritization`
13. `## Warnings: Changes That Would Make This Worse`
14. `## Agent TODO Checklist`

The `Agent TODO Checklist` must contain checkbox subsections for `Keep`, `Change`, `Add`, `Remove`, and `Do Not Touch`.

## Input Guidance
{format_guidance}

## Target Industry
{industry}

## Job
- ID: {job.theirstack_id}
- Title: {job.title or ""}
- Company: {job.company or ""}
- Location/Country: {job.country_code or ""}
- URL: {job.final_url or job.url or ""}

## Job Context JSON
```json
{job_json}
```

## Uploaded Resume Metadata
{analysis.facts_markdown}

## Uploaded Resume Text to Review
```text
{resume_text}
```
"""


def _extract_pdf_text(content: bytes) -> str:
    reader = PdfReader(BytesIO(content))
    page_text = [(page.extract_text() or "") for page in reader.pages]
    return _collapse_text_lines("\n\n".join(page_text))


def _extract_latex_text(source: str) -> str:
    text = _strip_latex_comments(source)
    text = re.sub(r"\\(section|subsection|subsubsection|paragraph|subparagraph)\*?(?:\[[^\]]*\])?\{([^{}]*)\}", r"\n\2\n", text)
    text = re.sub(r"\\(textbf|textit|emph|underline|href|url)(?:\[[^\]]*\])?\{([^{}]*)\}", r"\2", text)
    text = re.sub(r"\\(begin|end)\{[^{}]*\}", "\n", text)

    previous = None
    while previous != text:
        previous = text
        text = re.sub(r"\\[a-zA-Z]+\*?(?:\[[^\]]*\])?\{([^{}]*)\}", r"\1", text)

    text = re.sub(r"\\[a-zA-Z]+\*?(?:\[[^\]]*\])?", " ", text)
    text = re.sub(r"\\[^a-zA-Z\s]", " ", text)
    text = text.replace("{", " ").replace("}", " ")
    text = text.replace("~", " ").replace("&", " ")
    return _collapse_text_lines(text)


def _strip_latex_comments(source: str) -> str:
    stripped_lines: list[str] = []
    for line in source.splitlines():
        comment_at = len(line)
        for index, char in enumerate(line):
            if char == "%" and not _is_escaped(line, index):
                comment_at = index
                break
        stripped_lines.append(line[:comment_at])
    return "\n".join(stripped_lines)


def _is_escaped(text: str, index: int) -> bool:
    backslashes = 0
    cursor = index - 1
    while cursor >= 0 and text[cursor] == "\\":
        backslashes += 1
        cursor -= 1
    return backslashes % 2 == 1


def _collapse_text_lines(text: str) -> str:
    lines = [re.sub(r"\s+", " ", line).strip() for line in text.splitlines()]
    return "\n".join(line for line in lines if line)


def _facts_markdown(*, filename: str, kind: Literal["pdf", "latex", "text"], text: str) -> str:
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    headline = lines[0] if lines else ""
    headings = _detected_headings(lines)
    sections = ", ".join(headings) if headings else "None detected"
    preview = "\n".join(f"- {line}" for line in lines[:20])
    return "\n".join(
        [
            f"**Source:** {filename}",
            f"**Detected type:** {kind}",
            f"**Likely headline/name:** {headline}",
            f"**Detected sections:** {sections}",
            "**Resume text preview:**",
            preview,
        ]
    )


def _detected_headings(lines: list[str]) -> list[str]:
    headings: list[str] = []
    seen: set[str] = set()
    for line in lines:
        candidate = line.strip().rstrip(":")
        normalized = candidate.casefold()
        if not candidate or len(candidate) >= 80:
            continue
        if normalized in _COMMON_HEADINGS or candidate.isupper() or candidate.istitle():
            if normalized not in seen:
                seen.add(normalized)
                headings.append(candidate)
    return headings
