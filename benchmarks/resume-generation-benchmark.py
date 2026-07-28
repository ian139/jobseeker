#!/usr/bin/env python3
"""Offline two-platform resume workload for the autoresearch harness."""

from __future__ import annotations

import json
import os
import stat
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Any

from resume_generation.generator import ResumeJob, generate_resume


SOURCE_ID = "synthetic-profile"
EXPECTED_ARTIFACTS = {
    "resume.tex",
    "resume.pdf",
    "optimization.json",
    "job_description.txt",
    "manifest.json",
}


def source() -> dict[str, Any]:
    return {
        "id": SOURCE_ID,
        "type": "fixture",
        "location": "fixture://synthetic-profile",
        "sha256": "a" * 64,
        "retrieved_at": "2026-01-01T00:00:00Z",
        "notes": "public synthetic benchmark evidence",
    }


def dates(start: str, end: str, display: str) -> dict[str, str]:
    return {"start": start, "end": end, "display": display}


def bullet(identifier: str, text: str, keywords: list[str]) -> dict[str, Any]:
    return {
        "id": identifier,
        "text": text,
        "keywords": keywords,
        "sources": [SOURCE_ID],
    }


def profile() -> dict[str, Any]:
    return {
        "schema_version": 1,
        "skills": {
            "Languages": [
                {"name": "Python", "keywords": ["Python"], "sources": [SOURCE_ID]},
                {"name": "TypeScript", "keywords": ["TypeScript"], "sources": [SOURCE_ID]},
                {"name": "SQL", "keywords": ["SQL"], "sources": [SOURCE_ID]},
            ],
            "Frameworks": [
                {"name": "FastAPI", "keywords": ["FastAPI", "API"], "sources": [SOURCE_ID]},
                {"name": "React", "keywords": ["React"], "sources": [SOURCE_ID]},
            ],
        },
        "experience": [
            {
                "id": "backend-experience",
                "title": "Backend Engineering Intern",
                "organization": "Synthetic Systems",
                "location": "Remote",
                "dates": dates("2025-01", "Present", "Jan 2025 - Present"),
                "bullets": [
                    bullet(
                        "backend-python-api",
                        "Built reliable Python API services backed by SQL",
                        ["Python", "API", "SQL"],
                    ),
                    bullet(
                        "backend-testing",
                        "Added deterministic integration testing for data workflows",
                        ["testing", "data"],
                    ),
                ],
                "keywords": ["Python", "FastAPI", "API", "SQL", "data"],
                "sources": [SOURCE_ID],
            },
            {
                "id": "frontend-experience",
                "title": "Frontend Engineering Intern",
                "organization": "Synthetic Interfaces",
                "location": "Remote",
                "dates": dates("2024-01", "2024-12", "Jan 2024 - Dec 2024"),
                "bullets": [
                    bullet(
                        "frontend-react",
                        "Shipped accessible React interfaces in TypeScript",
                        ["React", "TypeScript", "accessibility"],
                    ),
                ],
                "keywords": ["React", "TypeScript", "accessibility"],
                "sources": [SOURCE_ID],
            },
        ],
        "leadership": [],
        "education": [
            {
                "id": "education",
                "institution": "Example University",
                "location": "Example, NY",
                "degree": "B.S. Computer Science",
                "dates": dates("2022-09", "2026-12", "Sep 2022 - Dec 2026"),
                "graduation": {
                    "default": "December 2026",
                    "rules": [
                        {
                            "id": "spring_coop",
                            "value": "May 2027",
                            "all_keyword_groups": [["spring"], ["co-op", "coop", "internship"]],
                            "sources": [SOURCE_ID],
                        }
                    ],
                },
                "keywords": ["Computer Science"],
                "sources": [SOURCE_ID],
            }
        ],
        "projects": [
            {
                "id": "api-project",
                "name": "Deterministic API",
                "link": "https://example.test/api-project",
                "dates": dates("2024-01", "2024-06", "Jan 2024 - Jun 2024"),
                "technologies": ["Python", "FastAPI", "SQL"],
                "bullets": [
                    bullet("api-project-bullet", "Created a Python FastAPI service", ["Python", "FastAPI"])
                ],
                "keywords": ["Python", "FastAPI", "SQL"],
                "sources": [SOURCE_ID],
                "enabled": True,
            },
            {
                "id": "ui-project",
                "name": "Accessible Dashboard",
                "link": "https://example.test/ui-project",
                "dates": dates("2023-01", "2023-06", "Jan 2023 - Jun 2023"),
                "technologies": ["React", "TypeScript"],
                "bullets": [
                    bullet("ui-project-bullet", "Built an accessible React dashboard", ["React", "TypeScript"])
                ],
                "keywords": ["React", "TypeScript", "accessibility"],
                "sources": [SOURCE_ID],
                "enabled": True,
            },
        ],
        "others": {
            "contact": {
                "full_name": "Ada Example",
                "phone": "+1-555-0100",
                "email": "ada@example.test",
            },
            "links": {
                "linkedin": "https://linkedin.example.test/ada",
                "github": "https://github.example.test/ada",
                "website": "https://ada.example.test",
            },
            "sources": [source()],
            "public_repositories": [],
            "open_questions": [],
        },
    }


def write_private(path: Path, contents: str) -> None:
    path.write_text(contents, encoding="utf-8")
    path.chmod(0o600)


def artifact_bundle_valid(result: Any, description: str) -> bool:
    directory = result.pdf_path.parent
    if {child.name for child in directory.iterdir()} != EXPECTED_ARTIFACTS:
        return False
    if any(stat.S_IMODE(child.stat().st_mode) != 0o600 for child in directory.iterdir()):
        return False
    if result.pages != 1 or result.pdf_path.stat().st_size == 0:
        return False
    return (directory / "job_description.txt").read_text(encoding="utf-8") == description


def main() -> int:
    payload = json.load(sys.stdin)
    jobs_payload = payload.get("jobs")
    if not isinstance(jobs_payload, list) or len(jobs_payload) != 2:
        raise ValueError("benchmark requires exactly two normalized platform jobs")

    repository = Path(__file__).resolve().parents[1]
    compiler_source = repository / "benchmarks" / "fake-latex-compiler.py"
    with tempfile.TemporaryDirectory(prefix="platform-resume-benchmark-") as temporary:
        root = Path(temporary).resolve()
        root.chmod(0o700)
        compiler = root / "pdflatex"
        shutil.copyfile(compiler_source, compiler)
        compiler.chmod(0o700)
        profile_path = root / "profile.json"
        template_path = root / "Resume.tex"
        skill_path = root / "SKILL.md"
        output_root = root / "output"
        counter_path = root / "compiler-count.txt"
        write_private(profile_path, json.dumps(profile(), sort_keys=True, separators=(",", ":")))
        write_private(
            template_path,
            "\\documentclass{article}\n\\begin{document}\n%%RESUME_HEADER%%\n%%RESUME_SECTIONS%%\n\\end{document}\n",
        )
        write_private(
            skill_path,
            "# Resume Generation Skill\n\nVersion: 2\n\n## Source-of-truth policy\n\n"
            "Use only source-backed claims.\n\n## Output invariants\n\nGenerate exactly one page.\n",
        )

        os.environ.pop("RESUME_ADVISORY_ENABLED", None)
        os.environ.pop("OLLAMA_CLOUD_API_KEY", None)
        os.environ.pop("OLLAMA_API_KEY", None)
        os.environ["BENCHMARK_COMPILER_COUNTER"] = str(counter_path)

        generated = []
        cache_hits = 0
        valid_bundles = 0
        keyword_matches = 0
        for index, value in enumerate(jobs_payload, 1):
            job = ResumeJob(
                id=index,
                title=value["title"],
                company=value["company"],
                description=value["description"],
                location=value.get("location"),
                posted_at="2026-01-01T00:00:00Z",
            )
            first = generate_resume(
                job,
                profile_path=profile_path,
                template_path=template_path,
                skill_path=skill_path,
                output_root=output_root,
                compiler=str(compiler),
            )
            second = generate_resume(
                job,
                profile_path=profile_path,
                template_path=template_path,
                skill_path=skill_path,
                output_root=output_root,
                compiler=str(compiler),
            )
            generated.append(first)
            cache_hits += int(first == second)
            valid_bundles += int(artifact_bundle_valid(first, job.description))
            required = {term.casefold() for term in value["expectedResumeTerms"]}
            matched = {term.casefold() for term in first.matched_keywords}
            keyword_matches += int(required.issubset(matched))

        compile_count = int(counter_path.read_text(encoding="utf-8"))
        result = {
            "jobs_generated": len(generated),
            "cache_hits": cache_hits,
            "valid_bundles": valid_bundles,
            "keyword_matches": keyword_matches,
            "compile_count": compile_count,
            "distinct_artifacts": len({item.artifact_ref for item in generated}),
            "one_page_resumes": sum(item.pages == 1 for item in generated),
        }
        print(json.dumps(result, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
