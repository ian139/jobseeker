from __future__ import annotations

import copy
import io
import hashlib
import json
import os
import stat
import sys
from pathlib import Path
from typing import Any

import pytest
from pypdf import PdfReader

import resume_generation.generator as resume_module
import resume_generation.command as resume_command
from resume_generation.generator import ResumeJob, generate_resume, load_resume_profile, optimize_resume


SOURCE_ID = "source-profile"
SOURCE_SHA256 = "a" * 64


def _source(source_id: str = SOURCE_ID) -> dict[str, Any]:
    return {
        "id": source_id,
        "type": "fixture",
        "location": "fixture://profile",
        "sha256": SOURCE_SHA256,
        "retrieved_at": "2026-01-01T00:00:00Z",
        "notes": "deterministic test evidence",
    }


def _dates(start: str = "2025-01", end: str = "Present", display: str = "Jan 2025 - Present") -> dict[str, str]:
    return {"start": start, "end": end, "display": display}


def _bullet(
    identifier: str,
    text: str,
    *,
    keywords: list[str] | None = None,
    sources: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "id": identifier,
        "text": text,
        "keywords": keywords or ["Python"],
        "sources": sources or [SOURCE_ID],
    }


def _experience(
    identifier: str,
    title: str,
    *,
    start: str = "2025-01",
    end: str = "Present",
    display: str = "Jan 2025 - Present",
    bullets: list[dict[str, Any]] | None = None,
    keywords: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "id": identifier,
        "title": title,
        "organization": "Acme Labs",
        "location": "Remote",
        "dates": _dates(start, end, display),
        "bullets": bullets if bullets is not None else [_bullet(f"{identifier}-bullet", "Built Python API services")],
        "keywords": keywords or ["Python", "API"],
        "sources": [SOURCE_ID],
    }


def _leadership(identifier: str = "lead-main") -> dict[str, Any]:
    return {
        "id": identifier,
        "title": "Engineering Society President",
        "organization": "Campus Society",
        "location": "Remote",
        "dates": _dates("2024-01", "2024-12", "Jan 2024 - Dec 2024"),
        "bullets": [_bullet(f"{identifier}-bullet", "Led Python workshops")],
        "keywords": ["Python"],
        "sources": [SOURCE_ID],
    }


def _project(identifier: str = "project-main") -> dict[str, Any]:
    return {
        "id": identifier,
        "name": "Python Service",
        "link": "https://example.test/python-service",
        "dates": _dates("2023-01", "2023-12", "Jan 2023 - Dec 2023"),
        "technologies": ["Python", "FastAPI"],
        "bullets": [
            _bullet(f"{identifier}-bullet-1", "Shipped Python API", keywords=["Python", "API"]),
        ],
        "keywords": ["Python", "API"],
        "sources": [SOURCE_ID],
        "enabled": True,
    }


def _base_profile() -> dict[str, Any]:
    return {
        "schema_version": 1,
        "skills": {
            "Languages": [
                {"name": "Python", "keywords": ["Python"], "sources": [SOURCE_ID]},
            ],
            "Frameworks": [
                {"name": "FastAPI", "keywords": ["FastAPI", "API"], "sources": [SOURCE_ID]},
            ],
        },
        "experience": [_experience("exp-main", "Software Engineer")],
        "leadership": [],
        "education": [
            {
                "id": "edu-main",
                "institution": "Example University",
                "location": "Example, NY",
                "degree": "B.S. Computer Science",
                "dates": _dates("2022-09", "2026-12", "Sep 2022 - Dec 2026"),
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
        "projects": [],
        "others": {
            "contact": {
                "full_name": "Ada Lovelace",
                "phone": "+1-555-0100",
                "email": "ada_jobs@example.test",
            },
            "links": {
                "linkedin": "https://linkedin.example.test/ada",
                "github": "https://github.example.test/ada",
                "website": "https://ada.example.test",
            },
            "sources": [_source()],
            "public_repositories": [],
            "open_questions": [],
        },
    }



def _write_profile(path: Path, payload: dict[str, Any] | None = None, *, raw: str | None = None) -> Path:
    if raw is None:
        raw = json.dumps(payload if payload is not None else _base_profile(), sort_keys=True, indent=2) + "\n"
    path.write_text(raw, encoding="utf-8")
    path.chmod(0o600)
    return path


def _write_template(path: Path) -> Path:
    path.write_text(
        "\\documentclass{article}\n"
        "\\begin{document}\n"
        "%%RESUME_HEADER%%\n"
        "%%RESUME_SECTIONS%%\n"
        "\\end{document}\n",
        encoding="utf-8",
    )
    path.with_name("SKILL.md").write_text(
        "# Resume Generation Skill\n\n"
        "Version: 1\n\n"
        "## Source-of-truth policy\n\n"
        "Use only source-backed claims.\n\n"
        "## Output invariants\n\n"
        "Generate exactly one page.\n",
        encoding="utf-8",
    )
    return path


def _job(
    identifier: int = 7,
    *,
    title: str = "Backend Engineer",
    description: str = "Requirements:\n- Python\n- API",
) -> ResumeJob:
    return ResumeJob(
        id=identifier,
        title=title,
        company="Acme Labs",
        description=description,
        location="Remote",
        posted_at="2026-01-01T00:00:00Z",
    )


def _write_fake_compiler(path: Path) -> Path:
    """Write a real subprocess compiler replacement that emits deterministic PDFs."""
    path.write_text(
        "#!" + sys.executable + "\n" + r'''
from __future__ import annotations

import json
import re
import os
import sys
from pathlib import Path


def pdf_bytes(page_count: int, text: str = "Generated resume") -> bytes:
    objects: list[bytes] = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
    ]
    kids = " ".join(f"{3 + index * 2} 0 R" for index in range(page_count))
    objects.append(f"<< /Type /Pages /Kids [{kids}] /Count {page_count} >>".encode())
    font_number = 3 + 2 * page_count
    escaped = text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
    stream = f"BT /F1 12 Tf 72 720 Td ({escaped}) Tj ET".encode()
    for index in range(page_count):
        page_number = 3 + index * 2
        content_number = page_number + 1
        objects.append(
            (
                f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
                f"/Resources << /Font << /F1 {font_number} 0 R >> >> "
                f"/Contents {content_number} 0 R >>"
            ).encode()
        )
        objects.append(b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"\nendstream")
    objects.append(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")

    output = bytearray(b"%PDF-1.4\n")
    offsets = [0]
    for number, obj in enumerate(objects, 1):
        offsets.append(len(output))
        output += f"{number} 0 obj\n".encode() + obj + b"\nendobj\n"
    xref = len(output)
    output += f"xref\n0 {len(objects) + 1}\n".encode()
    output += b"0000000000 65535 f \n"
    for offset in offsets[1:]:
        output += f"{offset:010d} 00000 n \n".encode()
    output += (
        f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\n"
        f"startxref\n{xref}\n%%EOF\n"
    ).encode()
    return bytes(output)


def main() -> int:
    arguments = sys.argv[1:]
    if arguments == ["--version"]:
        if os.environ.get("FAKE_FAIL") == "1":
            return 23
        print("fake pdflatex 1.0")
        return 0
    if "-output-directory" in arguments:
        output_dir = Path(arguments[arguments.index("-output-directory") + 1])
    elif "--outdir" in arguments:
        output_dir = Path(arguments[arguments.index("--outdir") + 1])
    else:
        output_dir = Path(arguments[-1]).parent
    tex_path = Path(arguments[-1])
    source = tex_path.read_text(encoding="utf-8")
    argument_log = os.environ.get("FAKE_ARG_LOG")
    if argument_log:
        Path(argument_log).write_text(json.dumps(arguments), encoding="utf-8")
    if os.environ.get("FAKE_FAIL") == "1":
        raise SystemExit(23)
    if os.environ.get("FAKE_ALWAYS_TWO") == "1":
        page_count = 2
    elif os.environ.get("FAKE_PAGE_LIMIT"):
        page_count = int(len(source) > int(os.environ["FAKE_PAGE_LIMIT"])) + 1
    elif os.environ.get("FAKE_TRIM_MODE") == "1":
        page_count = int("Optional Experience" in source) + 1
    else:
        page_count = 1

    output_dir.mkdir(parents=True, exist_ok=True)
    visible_text = re.sub(r"\\[A-Za-z*]+", " ", source)
    visible_text = re.sub(r"[^A-Za-z0-9+#./-]+", " ", visible_text)
    rendered_text = " ".join(visible_text.split())
    rendered_text += " Python" * 6 if os.environ.get("FAKE_REPEAT_MATCHED_TERM") == "1" else ""
    pdf_data = pdf_bytes(page_count, text="" if os.environ.get("FAKE_EMPTY_TEXT") == "1" else rendered_text)
    if os.environ.get("FAKE_LARGE_PDF") == "1":
        desired_size = int(os.environ.get("FAKE_LARGE_PDF_SIZE", "1"))
        pdf_data += b"X" * max(0, desired_size - len(pdf_data))
    (output_dir / "resume.pdf").write_bytes(pdf_data)
    # These simulate compiler by-products. The generator must remove them before publication.
    for name in ("resume.aux", "resume.log", "resume.synctex.gz"):
        (output_dir / name).write_bytes(b"compiler by-product")

    if os.environ.get("FAKE_OVERWRITE_TEX") == "1":
        tex_path.write_text("compiler overwrote resume.tex", encoding="utf-8")
    if os.environ.get("FAKE_DELETE_TEX") == "1":
        tex_path.unlink()
    if os.environ.get("FAKE_DELETE_DESCRIPTION") == "1":
        (output_dir / "job_description.txt").unlink()
    if os.environ.get("FAKE_SYMLINK_BYPRODUCT") == "1":
        (output_dir / "unsafe-link").symlink_to(tex_path)
    if os.environ.get("FAKE_DIRECTORY_BYPRODUCT") == "1":
        (output_dir / "unsafe-directory").mkdir()
    if os.environ.get("FAKE_OVERSIZE_BYPRODUCT") == "1":
        with (output_dir / "oversized-byproduct").open("wb") as handle:
            handle.truncate(int(os.environ.get("FAKE_OVERSIZE_SIZE", "1")))

    counter = os.environ.get("FAKE_COUNTER")
    if counter:
        counter_path = Path(counter)
        current = int(counter_path.read_text(encoding="utf-8")) if counter_path.exists() else 0
        counter_path.write_text(str(current + 1), encoding="utf-8")

    log_path = os.environ.get("FAKE_LOG")
    if log_path:
        summary = {
            "projects": source.count("\\resumeProjectHeading"),
            "leadership": "\\section{Leadership}" in source,
            "primary3": "Primary bullet 3" in source,
            "optional": "Optional Experience" in source,
            "optional_bullets": source.count("Optional Experience bullet"),
        }
        with Path(log_path).open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(summary, sort_keys=True) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
''',
        encoding="utf-8",
    )
    os.chmod(path, 0o700)
    return path




def _artifact_modes(result: Any) -> tuple[dict[str, int], dict[str, int]]:
    artifact_dir = result.pdf_path.parent
    file_modes = {child.name: stat.S_IMODE(child.stat().st_mode) for child in artifact_dir.iterdir()}
    directory_modes = {
        str(directory.relative_to(result.pdf_path.parents[2])): stat.S_IMODE(directory.stat().st_mode)
        for directory in (result.pdf_path.parents[2], result.pdf_path.parents[1], result.pdf_path.parents[0])
    }
    return file_modes, directory_modes




def _command_paths(tmp_path: Path) -> tuple[Path, Path]:
    profile_path = _write_profile(tmp_path / "profile.json")
    profile_path.chmod(0o600)
    template_path = _write_template(tmp_path / "Resume.tex")
    return profile_path, template_path


def _failure_payload(stderr: str) -> dict[str, Any]:
    lines = stderr.strip().splitlines()
    assert len(lines) == 1
    payload = json.loads(lines[0])
    assert isinstance(payload, dict)
    return payload


def test_profile_loader_rejects_malformed_unknown_and_duplicate_keys(tmp_path: Path) -> None:
    malformed = tmp_path / "malformed.json"
    malformed.write_text("{\"schema_version\":", encoding="utf-8")
    malformed.chmod(0o600)
    with pytest.raises(ValueError, match="profile JSON is invalid"):
        load_resume_profile(malformed)

    unknown_payload = _base_profile()
    unknown_payload["unexpected"] = True
    unknown = _write_profile(tmp_path / "unknown.json", unknown_payload)
    with pytest.raises(ValueError, match=r"unknown key\(s\) in profile root"):
        load_resume_profile(unknown)

    valid_text = json.dumps(_base_profile(), sort_keys=True, separators=(",", ":"))
    duplicate_text = valid_text.replace('"schema_version":1,', '"schema_version":1,"schema_version":1,', 1)
    duplicate = _write_profile(tmp_path / "duplicate.json", raw=duplicate_text)
    with pytest.raises(ValueError, match=r"duplicate key\(s\) in profile: schema_version"):
        load_resume_profile(duplicate)


@pytest.mark.parametrize(
    ("section", "field", "value"),
    (
        ("contact", "id", "contact"),
        ("contact", "sources", [SOURCE_ID]),
        ("links", "id", "links"),
        ("links", "sources", [SOURCE_ID]),
    ),
)
def test_profile_loader_keeps_contact_and_links_at_exact_v1_shape(
    tmp_path: Path, section: str, field: str, value: Any
) -> None:
    payload = _base_profile()
    payload["others"][section][field] = value
    profile_path = _write_profile(tmp_path / f"{section}-{field}.json", payload)

    with pytest.raises(ValueError, match=rf"unknown key\(s\) in others\.{section}"):
        load_resume_profile(profile_path)


def test_shipped_profile_and_template_generate_with_fixture_compiler(tmp_path: Path) -> None:
    repository = Path(__file__).resolve().parents[2]
    profile_path = repository / "private" / "resume" / "profile.json"
    template_path = repository / "resume" / "Resume.tex"
    skill_path = repository / "resume" / "SKILL.md"
    profile = load_resume_profile(profile_path)
    template = template_path.read_text(encoding="utf-8")
    assert template.count("%%RESUME_HEADER%%") == 1
    assert template.count("%%RESUME_SECTIONS%%") == 1
    assert "## Source-of-truth policy" in skill_path.read_text(encoding="utf-8")
    assert "## One-page density policy" in skill_path.read_text(encoding="utf-8")
    assert "at least one rendered\n  line" in skill_path.read_text(encoding="utf-8")

    result = generate_resume(
        _job(description="Requirements:\n- Python\n- AWS\n- Docker"),
        profile_path=profile_path,
        template_path=template_path,
        output_root=tmp_path / "output",

        compiler=str(_write_fake_compiler(tmp_path / "pdflatex")),
    )

    assert result.pages == 1
    report = json.loads(result.report_path.read_text(encoding="utf-8"))
    claim_ids = {claim["id"] for claim in report["selected_claims"]}
    assert claim_ids
    assert {"contact", "links"}.isdisjoint(claim_ids)
    assert report["ats_audit"]["passed"] is True
    assert report["ats_audit"]["rendered_coverage_ratio"] == 1.0
    assert report["ats_audit"]["supported_but_missed_terms"] == []

def test_rendered_ats_audit_normalizes_terms_and_rejects_unsupported_insertion(tmp_path: Path) -> None:
    assert resume_module._ats_contains("C++ CI/CD .NET", "C++")
    assert resume_module._ats_contains("C++ CI/CD .NET", "CI/CD")
    assert resume_module._ats_contains("C++ CI/CD .NET", ".NET")

    profile = load_resume_profile(_write_profile(tmp_path / "ats-audit-profile.json"))
    job = _job(title="Software Engineer", description="Requirements:\n- Python")
    plan = optimize_resume(profile, job)
    audit = resume_module._audit_rendered_resume(
        profile,
        plan,
        job,
        "Software Engineer Python",
    )
    assert audit["passed"] is True

    unsupported_job = _job(description="Requirements:\n- Python\n- AWS\n- Kubernetes")
    unsupported_plan = optimize_resume(profile, unsupported_job)
    unsupported = resume_module._audit_rendered_resume(
        profile,
        unsupported_plan,
        unsupported_job,
        "Backend Engineer Python AWS Kubernetes",
    )
    assert "Kubernetes" in unsupported["unsupported_inserted_terms"]
    assert unsupported["passed"] is False
def test_requirement_terms_strip_edge_slashes_and_dashes_before_stop_words(tmp_path: Path) -> None:
    job = _job(
        title="Software Engineer",
        description=(
            "Requirements:\n"
            "- Experience/ Qualifications: designing systems\n"
            "- CI/CD pipelines\n"
            "- Rust/ Go or Zig-\n"
            "- fast-paced environment"
        ),
    )
    terms = resume_module._requirement_terms(job.title, job.description)
    lowered = {term.casefold() for term in terms}
    # "Experience/" is a section-heading style token; the trailing slash should be
    # stripped so it matches the "experience" stop word and is dropped.
    assert "experience" not in lowered
    # Internal slashes must be preserved.
    assert "ci/cd" in lowered
    # Trailing slash/dash should be stripped without corrupting the remaining token.
    assert "rust" in lowered
    assert "zig" in lowered

def test_requirement_terms_ignore_generic_projects_word() -> None:
    job = _job(
        description=(
            "You will work on application projects for global expansion.\n"
            "Required Experience:\n"
            "- Java\n"
        ),
    )
    terms = {term.casefold() for term in resume_module._requirement_terms(job.title, job.description)}
    assert "projects" not in terms
    assert "java" in terms

def test_generation_fills_skills_before_lower_priority_content(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    payload = copy.deepcopy(_base_profile())
    payload["skills"]["Platforms"] = [
        {
            "name": name,
            "keywords": [name],
            "sources": [SOURCE_ID],
        }
        for name in (
            "Distributed Systems",
            "Infrastructure Automation",
            "Kubernetes Platform",
            "Observability Engineering",
        )
    ]
    leadership = _leadership("lead-filler")
    leadership["keywords"] = ["Leadership"]
    leadership["title"] = "Student Organizer"
    leadership["bullets"][0]["text"] = "Coordinated student events"
    leadership["bullets"][0]["keywords"] = ["Leadership"]
    payload["leadership"] = [leadership]
    profile_path = _write_profile(tmp_path / "profile.json", payload)
    template_path = _write_template(tmp_path / "Resume.tex")
    profile = load_resume_profile(profile_path)
    template_text = template_path.read_text(encoding="utf-8")
    job = _job(description="Requirements:\n- Python")
    plan = optimize_resume(profile, job)
    skill_plan = plan
    for _attempt in range(resume_module._MAX_SKILLS):
        candidates = tuple(
            candidate
            for candidate in resume_module._expansion_candidates(skill_plan, profile, job)
            if candidate.selection == skill_plan.selection
            and candidate.compressed_skills != skill_plan.compressed_skills
        )
        if len(", ".join(skill_plan.compressed_skills)) >= resume_module._MIN_SKILLS_LINE_CHARS:
            break
        assert candidates
        skill_plan = candidates[0]
    assert len(", ".join(skill_plan.compressed_skills)) >= resume_module._MIN_SKILLS_LINE_CHARS
    monkeypatch.setenv(
        "FAKE_PAGE_LIMIT",
        str(len(resume_module._render_resume(skill_plan, template_text))),
    )

    result = generate_resume(
        job,
        profile_path=profile_path,
        template_path=template_path,
        output_root=tmp_path / "output",
        compiler=str(_write_fake_compiler(tmp_path / "pdflatex")),
    )

    report = json.loads(result.report_path.read_text(encoding="utf-8"))
    assert result.pages == 1
    assert report["compressed_skills"] == list(skill_plan.compressed_skills)
    assert len(", ".join(report["compressed_skills"])) >= resume_module._MIN_SKILLS_LINE_CHARS
    assert "Leadership" not in report["sections"]

def test_generation_requires_authoritative_skill_file(tmp_path: Path) -> None:
    profile_path = _write_profile(tmp_path / "profile.json")
    template_path = _write_template(tmp_path / "Resume.tex")
    template_path.with_name("SKILL.md").unlink()
    compiler = _write_fake_compiler(tmp_path / "pdflatex")
    with pytest.raises(FileNotFoundError, match="resume skill not found"):
        generate_resume(
            _job(),
            profile_path=profile_path,
            template_path=template_path,
            output_root=tmp_path / "output",
            compiler=str(compiler),
        )


def test_profile_loader_rejects_duplicate_stable_ids_and_invalid_source_refs(tmp_path: Path) -> None:
    duplicate_payload = _base_profile()
    duplicate_payload["experience"].append(copy.deepcopy(duplicate_payload["experience"][0]))
    duplicate_path = _write_profile(tmp_path / "duplicate-id.json", duplicate_payload)
    with pytest.raises(ValueError, match="duplicate stable id"):
        load_resume_profile(duplicate_path)

    missing_source_payload = _base_profile()
    missing_source_payload["experience"][0]["bullets"][0]["sources"] = ["missing-source"]
    missing_source = _write_profile(tmp_path / "missing-source.json", missing_source_payload)
    with pytest.raises(ValueError, match="references missing source"):
        load_resume_profile(missing_source)


def test_profile_loader_rejects_synthesized_skill_claim_id_collisions(tmp_path: Path) -> None:
    duplicate_skill = _base_profile()
    duplicate_skill["skills"]["Languages"].append(
        copy.deepcopy(duplicate_skill["skills"]["Languages"][0])
    )
    with pytest.raises(ValueError, match=r"duplicate stable id 'skill:Languages:Python'"):
        load_resume_profile(_write_profile(tmp_path / "duplicate-skill.json", duplicate_skill))

    entry_collision = _base_profile()
    entry_collision["experience"][0]["id"] = "skill:Languages:Python"
    with pytest.raises(ValueError, match=r"duplicate stable id 'skill:Languages:Python'"):
        load_resume_profile(_write_profile(tmp_path / "skill-entry-collision.json", entry_collision))


@pytest.mark.parametrize("mutation", ("empty-education", "wrong-default", "extra-rule"))
def test_profile_loader_enforces_fixed_graduation_policy(tmp_path: Path, mutation: str) -> None:
    payload = _base_profile()
    if mutation == "empty-education":
        payload["education"] = []
    elif mutation == "wrong-default":
        payload["education"][0]["graduation"]["default"] = "May 2027"
    else:
        payload["education"][0]["graduation"]["rules"].append(
            {
                "id": "another-rule",
                "value": "August 2027",
                "all_keyword_groups": [["summer"], ["internship"]],
                "sources": [SOURCE_ID],
            }
        )

    with pytest.raises(ValueError):
        load_resume_profile(_write_profile(tmp_path / f"{mutation}.json", payload))


def test_profile_loader_rejects_symlink_and_detects_mutation_during_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real_path = _write_profile(tmp_path / "real-profile.json")
    symlink_path = tmp_path / "profile-link.json"
    symlink_path.symlink_to(real_path)
    with pytest.raises(ValueError, match="must not be a symlink"):
        load_resume_profile(symlink_path)

    profile_path = _write_profile(tmp_path / "mutating-profile.json")
    original_read = resume_module.os.read
    changed = False

    def mutating_read(fd: int, size: int) -> bytes:
        nonlocal changed
        chunk = original_read(fd, size)
        if chunk and not changed:
            changed = True
            profile_path.write_bytes(b"{}")
        return chunk

    monkeypatch.setattr(resume_module.os, "read", mutating_read)
    with pytest.raises(ValueError, match="changed during read"):
        load_resume_profile(profile_path)
    assert changed


@pytest.mark.parametrize("symlink_kind", ("profile", "template"))
def test_generation_rejects_symlink_profile_or_template_input(
    tmp_path: Path, symlink_kind: str
) -> None:
    real_profile = _write_profile(tmp_path / "profile.json")
    real_template = _write_template(tmp_path / "Resume.tex")
    profile_arg = real_profile
    template_arg = real_template
    if symlink_kind == "profile":
        profile_arg = tmp_path / "profile-link.json"
        profile_arg.symlink_to(real_profile)
    else:
        template_arg = tmp_path / "template-link.tex"
        template_arg.symlink_to(real_template)
    compiler_dir = tmp_path / "compiler"
    compiler_dir.mkdir(mode=0o700)
    compiler = _write_fake_compiler(compiler_dir / "pdflatex")

    with pytest.raises(ValueError, match="symlink"):
        generate_resume(
            _job(),
            profile_path=profile_arg,
            template_path=template_arg,
            output_root=tmp_path / "output",
            compiler=str(compiler),
        )


def test_optimization_is_deterministic_title_first_and_uses_supported_claims(tmp_path: Path) -> None:
    payload = _base_profile()
    payload["leadership"] = [_leadership()]
    payload["projects"] = [_project()]
    profile = load_resume_profile(_write_profile(tmp_path / "profile.json", payload))

    job = _job(
        title="Frontend Engineer",
        description="Requirements:\n- Machine Learning\n- Python\n- Rust\n- Haskell",
    )
    first = optimize_resume(profile, job)
    second = optimize_resume(profile, job)

    assert first == second
    # A description term must not override the first matching title field.
    assert first.field == "Frontend Engineering"
    assert "Python" in first.matched_keywords
    assert "Rust" in first.unsupported_keywords
    assert "Haskell" in first.unsupported_keywords
    assert "Rust" not in "\n".join(body for _name, body in first.sections)
    assert "Haskell" not in "\n".join(body for _name, body in first.sections)

    known_claims = {
        "edu-main",
        "exp-main",
        "exp-main-bullet",
        "lead-main",
        "lead-main-bullet",
        "project-main",
        "project-main-bullet-1",
        "skill:Languages:Python",
        "skill:Frameworks:FastAPI",
    }
    source_ids = {SOURCE_ID}
    assert first.selected_claims
    assert {identifier for identifier, _sources in first.selected_claims} <= known_claims
    assert all(sources and set(sources) <= source_ids for _identifier, sources in first.selected_claims)
    assert "Rust" not in first.matched_keywords
    assert "Haskell" not in first.matched_keywords
    assert dict(first.selected_claims)["exp-main-bullet"] == (SOURCE_ID,)
    assert r"ada\_jobs@example.test" in first.header_text
    assert any(name == "Experience" for name, _body in first.sections)
    assert first.sections.index(next(section for section in first.sections if section[0] == "Experience")) < first.sections.index(next(section for section in first.sections if section[0] == "Leadership"))


def test_optimization_graduation_requires_spring_and_coop_or_internship(tmp_path: Path) -> None:
    profile = load_resume_profile(_write_profile(tmp_path / "profile.json"))

    for marker in ("co-op", "coop", "internship"):
        spring_job = _job(title=f"Software Engineer {marker}", description=f"Spring {marker} role")
        spring_plan = optimize_resume(profile, spring_job)
        assert spring_plan.graduation_date == "May 2027"
        claims = dict(spring_plan.selected_claims)
        assert "contact" not in claims
        assert "links" not in claims
        assert claims["spring_coop"] == (SOURCE_ID,)
        education = dict(spring_plan.sections)["Education"]
        assert "May 2027" in education
        assert "Dec 2026" not in education

    no_spring = _job(title="Software Engineer Co-op", description="Fall co-op role")
    no_placement = _job(title="Software Engineer", description="Spring software role")
    default_plan = optimize_resume(profile, no_spring)
    assert default_plan.graduation_date == "December 2026"
    default_education = dict(default_plan.sections)["Education"]
    assert "Dec 2026" in default_education
    assert "May 2027" not in default_education
    assert optimize_resume(profile, no_placement).graduation_date == "December 2026"


def test_optimization_keeps_experience_primary_before_optional_sections(tmp_path: Path) -> None:
    payload = _base_profile()
    payload["leadership"] = [_leadership()]
    payload["projects"] = [_project()]
    profile = load_resume_profile(_write_profile(tmp_path / "profile.json", payload))
    plan = optimize_resume(profile, _job())

    assert plan.selection is not None
    assert [identifier for identifier, _bullets in plan.selection.experience] == ["exp-main"]
    assert plan.selection.primary_experience == "exp-main"
    assert [identifier for identifier, _bullets in plan.selection.leadership] == ["lead-main"]
    assert [identifier for identifier, _bullets in plan.selection.projects] == ["project-main"]
    assert [name for name, _body in plan.sections[:4]] == ["Education", "Experience", "Leadership", "Projects"]


def test_generation_accepts_relative_root_and_publishes_only_private_artifacts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    profile_path = _write_profile(tmp_path / "profile.json")
    template_path = _write_template(tmp_path / "Resume.tex")
    compiler = _write_fake_compiler(tmp_path / "pdflatex")
    monkeypatch.chdir(tmp_path)
    argument_log = tmp_path / "compiler-args.json"
    monkeypatch.setenv("FAKE_ARG_LOG", str(argument_log))

    job = _job(title="Software Engineer Co-op", description="Spring co-op role")
    result = generate_resume(
        job,
        profile_path=profile_path,
        template_path=template_path,
        output_root=Path("relative-output"),
        compiler=str(compiler),
    )

    output_root = tmp_path / "relative-output"
    assert result.pdf_path.is_relative_to(output_root)
    assert result.artifact_ref.startswith("job-7/")
    assert set(path.name for path in result.pdf_path.parent.iterdir()) == {
        "resume.tex",
        "resume.pdf",
        "optimization.json",
        "job_description.txt",
        "manifest.json",
    }
    job_dir = output_root / "job-7"
    assert {child.name for child in job_dir.iterdir()} == {result.pdf_path.parent.name}
    report = json.loads(result.report_path.read_text(encoding="utf-8"))
    manifest = json.loads((result.pdf_path.parent / "manifest.json").read_text(encoding="utf-8"))
    assert "skill_sha256" in manifest["inputs"]
    assert report["skill_sha256"] == manifest["inputs"]["skill_sha256"]
    assert report["graduation_date"] == "May 2027"
    claim_ids = {claim["id"] for claim in report["selected_claims"]}
    assert "spring_coop" in claim_ids
    assert {"contact", "links"}.isdisjoint(claim_ids)
    file_modes, directory_modes = _artifact_modes(result)
    assert set(file_modes) == {"resume.tex", "resume.pdf", "optimization.json", "job_description.txt", "manifest.json"}
    assert set(file_modes.values()) == {0o600}
    assert set(directory_modes.values()) == {0o700}

    reader = PdfReader(str(result.pdf_path))
    assert result.pages == 1
    assert len(reader.pages) == 1
    assert (reader.pages[0].extract_text() or "").strip()
    assert "-no-shell-escape" in json.loads(argument_log.read_text(encoding="utf-8"))


def test_generation_rejects_intermediate_output_symlink(
    tmp_path: Path,
) -> None:
    profile_path = _write_profile(tmp_path / "profile.json")
    template_path = _write_template(tmp_path / "Resume.tex")
    compiler_dir = tmp_path / "compiler"
    compiler_dir.mkdir(mode=0o700)
    compiler = _write_fake_compiler(compiler_dir / "pdflatex")
    parent = tmp_path / "output-parent"
    target = tmp_path / "symlink-target"
    parent.mkdir(mode=0o700)
    target.mkdir(mode=0o700)
    (parent / "link").symlink_to(target, target_is_directory=True)

    with pytest.raises(RuntimeError):
        generate_resume(
            _job(),
            profile_path=profile_path,
            template_path=template_path,
            output_root=parent / "link" / "nested",
            compiler=str(compiler),
        )


def test_generation_rejects_nonprivate_existing_output_root_without_chmod(
    tmp_path: Path,
) -> None:
    profile_path = _write_profile(tmp_path / "profile.json")
    template_path = _write_template(tmp_path / "Resume.tex")
    compiler_dir = tmp_path / "compiler"
    compiler_dir.mkdir(mode=0o700)
    compiler = _write_fake_compiler(compiler_dir / "pdflatex")
    output_root = tmp_path / "existing-output"
    output_root.mkdir(mode=0o755)
    os.chmod(output_root, 0o755)
    before = stat.S_IMODE(output_root.stat().st_mode)

    with pytest.raises((RuntimeError, PermissionError)):
        generate_resume(
            _job(),
            profile_path=profile_path,
            template_path=template_path,
            output_root=output_root,
            compiler=str(compiler),
        )

    assert stat.S_IMODE(output_root.stat().st_mode) == before





def test_generation_rejects_pdf_over_named_snapshot_cap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    profile_path = _write_profile(tmp_path / "profile.json")
    template_path = _write_template(tmp_path / "Resume.tex")


    compiler = _write_fake_compiler(tmp_path / "pdflatex")
    monkeypatch.setenv("FAKE_LARGE_PDF", "1")
    monkeypatch.setenv(
        "FAKE_LARGE_PDF_SIZE",
        str(resume_module._MAX_RESUME_PDF_BYTES + 1),
    )

    with pytest.raises((RuntimeError, ValueError), match="exceeds"):
        generate_resume(
            _job(),
            profile_path=profile_path,
            template_path=template_path,
            output_root=tmp_path / "output",
            compiler=str(compiler),
        )


def test_generation_resolves_bare_compiler_from_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    profile_path = _write_profile(tmp_path / "profile.json")
    template_path = _write_template(tmp_path / "Resume.tex")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(mode=0o700)
    _write_fake_compiler(bin_dir / "pdflatex")
    counter = tmp_path / "compile-count"
    monkeypatch.setenv("PATH", str(bin_dir) + os.pathsep + os.environ.get("PATH", ""))
    monkeypatch.setenv("FAKE_COUNTER", str(counter))
    monkeypatch.chdir(tmp_path)

    result = generate_resume(
        _job(),
        profile_path=profile_path,
        template_path=template_path,
        output_root=tmp_path / "output",
        compiler="pdflatex",
    )

    assert result.pages == 1
    assert counter.read_text(encoding="utf-8") == "1"


def test_generation_resolves_relative_compiler_before_stage_chdir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    profile_path = _write_profile(tmp_path / "profile.json")
    template_path = _write_template(tmp_path / "Resume.tex")
    bin_dir = tmp_path / "compiler tools"
    bin_dir.mkdir(mode=0o700)
    _write_fake_compiler(bin_dir / "pdflatex")
    counter = tmp_path / "compile-count"
    monkeypatch.setenv("FAKE_COUNTER", str(counter))
    monkeypatch.chdir(tmp_path)

    result = generate_resume(
        _job(),
        profile_path=profile_path,
        template_path=template_path,
        output_root=tmp_path / "output",
        compiler=os.fspath(Path("compiler tools") / "pdflatex"),
    )

    assert result.pages == 1
    assert counter.read_text(encoding="utf-8") == "1"





def test_generation_rejects_compiler_command_with_extra_arguments(tmp_path: Path) -> None:
    profile_path = _write_profile(tmp_path / "profile.json")
    template_path = _write_template(tmp_path / "Resume.tex")
    compiler = _write_fake_compiler(tmp_path / "pdflatex")

    with pytest.raises(ValueError):
        generate_resume(
            _job(),
            profile_path=profile_path,
            template_path=template_path,
            output_root=tmp_path / "output",
            compiler=f"{compiler} --version",
        )


@pytest.mark.parametrize("target", ("profile", "template"))
def test_generation_rejects_insecure_input_modes(tmp_path: Path, target: str) -> None:
    profile_path = _write_profile(tmp_path / "profile.json")
    template_path = _write_template(tmp_path / "Resume.tex")
    compiler = _write_fake_compiler(tmp_path / "pdflatex")
    if target == "profile":
        profile_path.chmod(0o640)
    else:
        template_path.chmod(0o664)

    with pytest.raises(ValueError):
        generate_resume(
            _job(),
            profile_path=profile_path,
            template_path=template_path,
            output_root=tmp_path / "output",
            compiler=compiler,
        )


def test_generation_rejects_group_writable_compiler(tmp_path: Path) -> None:
    profile_path = _write_profile(tmp_path / "profile.json")
    template_path = _write_template(tmp_path / "Resume.tex")
    compiler = _write_fake_compiler(tmp_path / "pdflatex")
    compiler.chmod(0o775)

    with pytest.raises(RuntimeError, match="non-writable regular file"):
        generate_resume(
            _job(),
            profile_path=profile_path,
            template_path=template_path,
            output_root=tmp_path / "output",
            compiler=str(compiler),
        )


def test_default_discovery_rejects_unsafe_compiler_before_execution(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    profile_path = _write_profile(tmp_path / "profile.json")
    template_path = _write_template(tmp_path / "Resume.tex")
    marker = tmp_path / "executed"
    compiler = tmp_path / "tectonic"
    compiler.write_text('#!/bin/sh\nprintf ran > "$MARKER"\n', encoding="utf-8")
    compiler.chmod(0o777)
    monkeypatch.setenv("PATH", str(tmp_path))
    monkeypatch.setenv("MARKER", str(marker))

    with pytest.raises(RuntimeError, match="no safe, successful LaTeX compiler"):
        generate_resume(
            _job(),
            profile_path=profile_path,
            template_path=template_path,
            output_root=tmp_path / "output",
        )
    assert not marker.exists()


def test_default_discovery_skips_unsafe_compiler_for_safe_fallback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    profile_path = _write_profile(tmp_path / "profile.json")
    template_path = _write_template(tmp_path / "Resume.tex")
    marker = tmp_path / "unsafe-executed"
    unsafe = tmp_path / "tectonic"
    unsafe.write_text('#!/bin/sh\nprintf ran > "$MARKER"\n', encoding="utf-8")
    unsafe.chmod(0o777)
    _write_fake_compiler(tmp_path / "pdflatex")
    monkeypatch.setenv("PATH", str(tmp_path))
    monkeypatch.setenv("MARKER", str(marker))

    result = generate_resume(
        _job(),
        profile_path=profile_path,
        template_path=template_path,
        output_root=tmp_path / "output",
    )
    assert result.pages == 1
    assert not marker.exists()


def test_generation_rejects_compiler_replaced_after_fingerprinting(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    profile_path = _write_profile(tmp_path / "profile.json")
    template_path = _write_template(tmp_path / "Resume.tex")
    compiler = _write_fake_compiler(tmp_path / "pdflatex")
    original_optimize = resume_module.optimize_resume

    def replace_compiler(*args: Any, **kwargs: Any) -> Any:
        plan = original_optimize(*args, **kwargs)
        compiler.write_text(compiler.read_text(encoding="utf-8") + "\n# replaced\n", encoding="utf-8")
        compiler.chmod(0o755)
        return plan

    monkeypatch.setattr(resume_module, "optimize_resume", replace_compiler)
    with pytest.raises(RuntimeError, match="changed before invocation"):
        generate_resume(
            _job(),
            profile_path=profile_path,
            template_path=template_path,
            output_root=tmp_path / "output",
            compiler=str(compiler),
        )


def test_generation_caches_identical_input_without_recompiling(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    profile_path = _write_profile(tmp_path / "profile.json")
    template_path = _write_template(tmp_path / "Resume.tex")
    compiler = _write_fake_compiler(tmp_path / "pdflatex")
    counter = tmp_path / "compile-count"
    monkeypatch.setenv("FAKE_COUNTER", str(counter))

    first = generate_resume(
        _job(),
        profile_path=profile_path,
        template_path=template_path,
        output_root=tmp_path / "output",
        compiler=str(compiler),
    )
    second = generate_resume(
        _job(),
        profile_path=profile_path,
        template_path=template_path,
        output_root=tmp_path / "output",
        compiler=str(compiler),
    )

    assert first == second
    assert counter.read_text(encoding="utf-8") == "1"


def test_generation_compiler_identity_separates_cache_fingerprints(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    profile_path = _write_profile(tmp_path / "profile.json")
    template_path = _write_template(tmp_path / "Resume.tex")
    first_dir = tmp_path / "compiler-a"
    second_dir = tmp_path / "compiler-b"
    first_dir.mkdir(mode=0o700)
    second_dir.mkdir(mode=0o700)
    first_compiler = _write_fake_compiler(first_dir / "pdflatex")
    second_compiler = _write_fake_compiler(second_dir / "pdflatex")
    counter = tmp_path / "compile-count"
    monkeypatch.setenv("FAKE_COUNTER", str(counter))

    first = generate_resume(
        _job(),
        profile_path=profile_path,
        template_path=template_path,
        output_root=tmp_path / "output",
        compiler=str(first_compiler),
    )
    second = generate_resume(
        _job(),
        profile_path=profile_path,
        template_path=template_path,
        output_root=tmp_path / "output",
        compiler=str(second_compiler),
    )
    second_compiler.write_text(
        second_compiler.read_text(encoding="utf-8") + "\n# changed compiler binary\n",
        encoding="utf-8",
    )
    third = generate_resume(
        _job(),
        profile_path=profile_path,
        template_path=template_path,
        output_root=tmp_path / "output",
        compiler=str(second_compiler),
    )

    assert first.artifact_ref != second.artifact_ref
    assert second.artifact_ref != third.artifact_ref
    assert counter.read_text(encoding="utf-8") == "3"


@pytest.mark.parametrize("tamper", ("hash", "mode", "size"))
def test_generation_rejects_unsafe_cache_tampering(
    tmp_path: Path, tamper: str
) -> None:
    profile_path = _write_profile(tmp_path / "profile.json")
    template_path = _write_template(tmp_path / "Resume.tex")
    compiler = _write_fake_compiler(tmp_path / "pdflatex")
    result = generate_resume(
        _job(),
        profile_path=profile_path,
        template_path=template_path,
        output_root=tmp_path / "output",
        compiler=str(compiler),
    )

    if tamper == "hash":
        result.pdf_path.write_bytes(result.pdf_path.read_bytes() + b"tampered")
        expected = "digest mismatch"
    elif tamper == "mode":
        os.chmod(result.pdf_path, 0o644)
        expected = "not private"
    else:
        with result.pdf_path.open("r+b") as handle:
            handle.truncate(resume_module._MAX_RESUME_PDF_BYTES + 1)
        expected = "exceeds"

    with pytest.raises((RuntimeError, ValueError), match=expected):
        generate_resume(
            _job(),
            profile_path=profile_path,
            template_path=template_path,
            output_root=tmp_path / "output",
            compiler=str(compiler),
        )


def _trim_profile() -> dict[str, Any]:
    payload = _base_profile()
    payload["experience"] = [
        _experience(
            "exp-primary",
            "Primary Experience",
            start="2025-01",
            display="Jan 2025 - Present",
            bullets=[
                _bullet("primary-1", "Primary bullet 1 Python API", keywords=["Python", "API"]),
                _bullet("primary-2", "Primary bullet 2 Python API", keywords=["Python", "API"]),
                _bullet("primary-3", "Primary bullet 3 Python API", keywords=["Python", "API"]),
            ],
            keywords=["Python", "API"],
        ),
        _experience(
            "exp-optional",
            "Optional Experience",
            start="2024-01",
            end="2024-12",
            display="Jan 2024 - Dec 2024",
            bullets=[
                _bullet("optional-1", "Optional Experience bullet 1", keywords=["General"]),
                _bullet("optional-2", "Optional Experience bullet 2", keywords=["General"]),
                _bullet("optional-3", "Optional Experience bullet 3", keywords=["General"]),
            ],
            keywords=["General"],
        ),
    ]
    payload["leadership"] = [_leadership()]
    payload["projects"] = [
        {
            **_project(),
            "bullets": [
                _bullet("project-1", "Project bullet 1 Python", keywords=["Python"]),
                _bullet("project-2", "Project bullet 2 Python", keywords=["Python"]),
            ],
        }
    ]
    return payload


def _trim_summaries(log_path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in log_path.read_text(encoding="utf-8").splitlines()]


def test_trim_removes_globally_lowest_extra_bullets(tmp_path: Path) -> None:
    payload = _base_profile()
    payload["experience"] = [
        _experience(
            "exp-primary",
            "Primary Experience",
            bullets=[
                _bullet("primary-high-1", "Primary Python API work"),
                _bullet("primary-high-2", "More primary Python API work"),
            ],
        ),
        _experience(
            "exp-low-extra",
            "Older Python Experience",
            start="2024-01",
            end="2024-12",
            display="Jan 2024 - Dec 2024",
            bullets=[
                _bullet("experience-high", "Python API delivery"),
                _bullet("experience-low", "General documentation", keywords=["General"]),
            ],
        ),
        _experience(
            "exp-high-extra",
            "Oldest Python Experience",
            start="2023-01",
            end="2023-12",
            display="Jan 2023 - Dec 2023",
            bullets=[
                _bullet("experience-highest-1", "Python API systems"),
                _bullet("experience-highest-2", "Python API automation"),
            ],
        ),
    ]
    payload["projects"] = [
        {
            **_project("project-low-extra"),
            "name": "Python Alpha",
            "link": "https://example.test/python-alpha",
            "bullets": [
                _bullet("project-high", "Python API service"),
                _bullet("project-low", "General documentation", keywords=["General"]),
            ],
        },
        {
            **_project("project-high-extra"),
            "name": "Python Omega",
            "link": "https://example.test/python-omega",
            "bullets": [
                _bullet("project-highest-1", "Python API platform"),
                _bullet("project-highest-2", "Python API automation"),
            ],
        },
    ]
    profile = load_resume_profile(_write_profile(tmp_path / "profile.json", payload))
    job = _job()
    plan = optimize_resume(profile, job)
    assert plan.selection is not None
    terms = resume_module._job_term_weights(
        job.title,
        job.description,
        resume_module._infer_field(job.title, job.description),
    )

    projects = resume_module._remove_lowest_project_content(plan.selection, profile, terms)
    assert projects is not None
    project_bullets = dict(projects.projects)
    assert "project-low" not in project_bullets["project-low-extra"]
    assert "project-highest-2" in project_bullets["project-high-extra"]

    experience = resume_module._remove_extra_experience_bullet(plan.selection, profile, terms)
    assert experience is not None
    experience_bullets = dict(experience.experience)
    assert "experience-low" not in experience_bullets["exp-low-extra"]
    assert "experience-highest-2" in experience_bullets["exp-high-extra"]


def test_generation_trims_projects_leadership_then_optional_extra_bullets(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    profile_path = _write_profile(tmp_path / "profile.json", _trim_profile())
    template_path = _write_template(tmp_path / "Resume.tex")
    compiler = _write_fake_compiler(tmp_path / "pdflatex")
    log_path = tmp_path / "trim-log.jsonl"
    monkeypatch.setenv("FAKE_TRIM_MODE", "1")
    monkeypatch.setenv("FAKE_LOG", str(log_path))

    result = generate_resume(
        _job(),
        profile_path=profile_path,
        template_path=template_path,
        output_root=tmp_path / "output",
        compiler=str(compiler),
    )

    assert _trim_summaries(log_path) == [
        {"projects": 1, "leadership": True, "primary3": True, "optional": True, "optional_bullets": 3},
        {"projects": 1, "leadership": True, "primary3": True, "optional": True, "optional_bullets": 3},
        {"projects": 1, "leadership": True, "primary3": True, "optional": True, "optional_bullets": 3},
        {"projects": 0, "leadership": True, "primary3": True, "optional": True, "optional_bullets": 3},
        {"projects": 0, "leadership": False, "primary3": True, "optional": True, "optional_bullets": 3},
        {"projects": 0, "leadership": False, "primary3": True, "optional": True, "optional_bullets": 2},
        {"projects": 0, "leadership": False, "primary3": True, "optional": True, "optional_bullets": 1},
        {"projects": 0, "leadership": False, "primary3": True, "optional": False, "optional_bullets": 0},
        {"projects": 0, "leadership": False, "primary3": True, "optional": True, "optional_bullets": 1},
        {"projects": 0, "leadership": True, "primary3": True, "optional": False, "optional_bullets": 0},
        {"projects": 1, "leadership": True, "primary3": True, "optional": False, "optional_bullets": 0},
        {"projects": 1, "leadership": True, "primary3": True, "optional": False, "optional_bullets": 0},
    ]
    final_tex = result.tex_path.read_text(encoding="utf-8")
    assert "Primary bullet 1" in final_tex
    assert "Primary bullet 2" in final_tex
    assert "Primary bullet 3" in final_tex
    assert "Optional Experience" not in final_tex
    assert "\\section{Leadership}" in final_tex
    assert "\\resumeProjectHeading" in final_tex


def test_generation_fails_closed_when_controlled_fixture_never_fits(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    profile_path = _write_profile(tmp_path / "profile.json", _trim_profile())
    template_path = _write_template(tmp_path / "Resume.tex")
    compiler = _write_fake_compiler(tmp_path / "pdflatex")
    output_root = tmp_path / "output"
    monkeypatch.setenv("FAKE_ALWAYS_TWO", "1")

    with pytest.raises(RuntimeError, match="unable to fit resume to one page"):
        generate_resume(
            _job(),
            profile_path=profile_path,
            template_path=template_path,
            output_root=output_root,
            compiler=str(compiler),
        )

    job_dir = output_root / "job-7"
    assert job_dir.is_dir()
    assert list(job_dir.iterdir()) == []

def test_generation_rejects_one_page_pdf_without_extractable_text(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    profile_path = _write_profile(tmp_path / "profile.json")
    template_path = _write_template(tmp_path / "Resume.tex")
    compiler = _write_fake_compiler(tmp_path / "pdflatex")
    monkeypatch.setenv("FAKE_EMPTY_TEXT", "1")

    with pytest.raises(RuntimeError, match="generated PDF has no extractable text"):
        generate_resume(
            _job(title="Generalist", description="General responsibilities."),
            profile_path=profile_path,
            template_path=template_path,
            output_root=tmp_path / "output",
            compiler=str(compiler),
        )

def test_generation_rejects_repeated_matched_terms(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    profile_path = _write_profile(tmp_path / "profile.json")
    template_path = _write_template(tmp_path / "Resume.tex")
    compiler = _write_fake_compiler(tmp_path / "pdflatex")
    monkeypatch.setenv("FAKE_REPEAT_MATCHED_TERM", "1")

    with pytest.raises(RuntimeError, match="generated resume failed ATS evidence audit"):
        generate_resume(
            _job(description="Requirements:\n- Python"),
            profile_path=profile_path,
            template_path=template_path,
            output_root=tmp_path / "output",
            compiler=str(compiler),
        )


@pytest.mark.parametrize(
    "environment",
    (
        "FAKE_OVERWRITE_TEX",
        "FAKE_DELETE_TEX",
        "FAKE_DELETE_DESCRIPTION",
        "FAKE_SYMLINK_BYPRODUCT",
        "FAKE_DIRECTORY_BYPRODUCT",
        "FAKE_OVERSIZE_BYPRODUCT",
    ),
)
def test_generation_rejects_compiler_overwrite_delete_and_unsafe_byproducts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, environment: str
) -> None:
    profile_path = _write_profile(tmp_path / "profile.json")
    template_path = _write_template(tmp_path / "Resume.tex")
    compiler = _write_fake_compiler(tmp_path / "pdflatex")
    monkeypatch.setenv(environment, "1")
    if environment == "FAKE_OVERSIZE_BYPRODUCT":
        monkeypatch.setenv("FAKE_OVERSIZE_SIZE", str(resume_module._MAX_STAGE_BYTES + 1))

    with pytest.raises(RuntimeError):
        generate_resume(
            _job(),
            profile_path=profile_path,
            template_path=template_path,
            output_root=tmp_path / "output",
            compiler=str(compiler),
        )




def _job_snapshot(*, description: str = "Requirements:\n- Python\n- API") -> dict[str, Any]:
    return {
        "schema": "resume-job-v1",
        "id": 7,
        "title": "Backend Engineer",
        "company": "Acme Labs",
        "description": description,
        "location": "Remote",
        "posted_at": "2026-01-01T00:00:00Z",
    }


def _canonical_snapshot(payload: dict[str, Any]) -> bytes:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _command_argv(
    tmp_path: Path,
    *,
    profile_path: Path,
    template_path: Path,
    skill_path: Path | None = None,
    output_root: Path | None = None,
    compiler: Path | None = None,
    extra: list[str] | None = None,
) -> list[str]:
    arguments = [
        "--profile",
        str(profile_path),
        "--template",
        str(template_path),
        "--skill",
        str(skill_path if skill_path is not None else template_path.with_name("SKILL.md")),
        "--output-root",
        str(output_root if output_root is not None else tmp_path / "output"),
    ]
    if compiler is not None:
        arguments.extend(("--compiler", str(compiler)))
    if extra:
        arguments.extend(extra)
    return arguments


def _run_direct_command(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    arguments: list[str],
    stdin_bytes: bytes,
) -> tuple[int, str, str]:
    monkeypatch.setattr(sys, "stdin", io.TextIOWrapper(io.BytesIO(stdin_bytes), encoding="utf-8"))
    return_code = resume_command.main(arguments)
    captured = capsys.readouterr()
    return return_code, captured.out, captured.err


def _success_payload(stdout: str) -> dict[str, Any]:
    lines = stdout.strip().splitlines()
    assert len(lines) == 1
    payload = json.loads(lines[0])
    assert isinstance(payload, dict)
    return payload


def _assert_sanitized_error(stderr: str, code: str, tmp_path: Path) -> None:
    assert _failure_payload(stderr) == {
        "error": {"code": code, "message": {
            "invalid_input": "resume generation input was rejected",
            "generation_error": "resume generation failed",
        }[code]},
    }
    assert str(tmp_path) not in stderr
    assert "usage:" not in stderr.casefold()
    assert "traceback" not in stderr.casefold()


@pytest.mark.parametrize(
    "stdin_bytes",
    (
        b"{",
        b'{"company":"Acme Labs","description":"Requirements:\\n- Python\\n- API","id":7,"id":8,"location":"Remote","posted_at":"2026-01-01T00:00:00Z","schema":"resume-job-v1","title":"Backend Engineer"}',
        _canonical_snapshot({**_job_snapshot(), "unexpected": "rejected"}),
        _canonical_snapshot({key: value for key, value in _job_snapshot().items() if key != "location"}),
        json.dumps(_job_snapshot()).encode("utf-8"),
        b"\xff",
        b"x" * (64 * 1024 + 1),
    ),
    ids=("malformed", "duplicate-key", "unknown-key", "missing-key", "noncanonical", "invalid-utf8", "oversized"),
)
def test_command_rejects_invalid_canonical_stdin_with_sanitized_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    stdin_bytes: bytes,
) -> None:
    profile_path, template_path = _command_paths(tmp_path)
    return_code, stdout, stderr = _run_direct_command(
        monkeypatch,
        capsys,
        _command_argv(tmp_path, profile_path=profile_path, template_path=template_path),
        stdin_bytes,
    )
    assert return_code == 1
    assert stdout == ""
    _assert_sanitized_error(stderr, "invalid_input", tmp_path)


@pytest.mark.parametrize("target", ("profile", "template", "skill", "output"))
def test_command_rejects_unsafe_or_symlinked_paths_with_sanitized_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    target: str,
) -> None:
    profile_path, template_path = _command_paths(tmp_path)
    skill_path = template_path.with_name("SKILL.md")
    output_root = tmp_path / "output"
    output_root.mkdir()
    if target == "profile":
        unsafe_profile = tmp_path / "profile-link.json"
        unsafe_profile.symlink_to(profile_path)
        profile_path = unsafe_profile
    elif target == "template":
        unsafe_template = tmp_path / "Resume-link.tex"
        unsafe_template.symlink_to(template_path)
        template_path = unsafe_template
    elif target == "skill":
        unsafe_skill = tmp_path / "SKILL-link.md"
        unsafe_skill.symlink_to(skill_path)
        skill_path = unsafe_skill
    else:
        unsafe_output = tmp_path / "output-link"
        unsafe_output.symlink_to(output_root, target_is_directory=True)
        output_root = unsafe_output

    return_code, stdout, stderr = _run_direct_command(
        monkeypatch,
        capsys,
        _command_argv(
            tmp_path,
            profile_path=profile_path,
            template_path=template_path,
            skill_path=skill_path,
            output_root=output_root,
        ),
        _canonical_snapshot(_job_snapshot()),
    )
    assert return_code == 1
    assert stdout == ""
    _assert_sanitized_error(stderr, "invalid_input", tmp_path)


@pytest.mark.parametrize("target", ("profile", "template", "skill"))
def test_command_rejects_group_writable_inputs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    target: str,
) -> None:
    profile_path, template_path = _command_paths(tmp_path)
    skill_path = template_path.with_name("SKILL.md")
    paths = {"profile": profile_path, "template": template_path, "skill": skill_path}
    paths[target].chmod(stat.S_IMODE(paths[target].stat().st_mode) | 0o020)

    return_code, stdout, stderr = _run_direct_command(
        monkeypatch,
        capsys,
        _command_argv(
            tmp_path,
            profile_path=profile_path,
            template_path=template_path,
            skill_path=skill_path,
        ),
        _canonical_snapshot(_job_snapshot()),
    )
    assert return_code == 1
    assert stdout == ""
    _assert_sanitized_error(stderr, "invalid_input", tmp_path)


def test_command_rejects_group_writable_compiler(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    profile_path, template_path = _command_paths(tmp_path)
    compiler = _write_fake_compiler(tmp_path / "compiler")
    compiler.chmod(0o775)

    return_code, stdout, stderr = _run_direct_command(
        monkeypatch,
        capsys,
        _command_argv(
            tmp_path,
            profile_path=profile_path,
            template_path=template_path,
            compiler=compiler,
        ),
        _canonical_snapshot(_job_snapshot()),
    )
    assert return_code == 1
    assert stdout == ""
    _assert_sanitized_error(stderr, "invalid_input", tmp_path)


def test_command_cli_argument_errors_are_sanitized(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    profile_path, template_path = _command_paths(tmp_path)
    arguments = _command_argv(tmp_path, profile_path=profile_path, template_path=template_path)
    for malformed_arguments in (arguments[:-2], arguments + ["--unknown-option"]):
        return_code, stdout, stderr = _run_direct_command(
            monkeypatch,
            capsys,
            malformed_arguments,
            _canonical_snapshot(_job_snapshot()),
        )
        assert return_code == 1
        assert stdout == ""
        _assert_sanitized_error(stderr, "invalid_input", tmp_path)


def test_command_generation_failure_is_one_sanitized_json_object(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    profile_path, template_path = _command_paths(tmp_path)
    compiler = _write_fake_compiler(tmp_path / "pdflatex")
    monkeypatch.setenv("FAKE_FAIL", "1")
    return_code, stdout, stderr = _run_direct_command(
        monkeypatch,
        capsys,
        _command_argv(tmp_path, profile_path=profile_path, template_path=template_path, compiler=compiler),
        _canonical_snapshot(_job_snapshot()),
    )
    assert return_code == 1
    assert stdout == ""
    _assert_sanitized_error(stderr, "generation_error", tmp_path)


def test_command_identical_input_reuses_verified_artifact(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    profile_path, template_path = _command_paths(tmp_path)
    compiler = _write_fake_compiler(tmp_path / "pdflatex")
    counter = tmp_path / "compiler-counter"
    monkeypatch.setenv("FAKE_COUNTER", str(counter))
    arguments = _command_argv(tmp_path, profile_path=profile_path, template_path=template_path, compiler=compiler)
    snapshot = _canonical_snapshot(_job_snapshot())

    first_rc, first_out, first_err = _run_direct_command(monkeypatch, capsys, arguments, snapshot)
    second_rc, second_out, second_err = _run_direct_command(monkeypatch, capsys, arguments, snapshot)
    first = _success_payload(first_out)
    second = _success_payload(second_out)

    assert first_rc == second_rc == 0
    assert first_err == second_err == ""
    assert first == second
    assert set(first) == {
        "schema",
        "job_id",
        "artifact_ref",
        "tex_path",
        "pdf_path",
        "report_path",
        "manifest_path",
        "pages",
        "field",
        "graduation_date",
        "matched_keywords",
    }
    assert first["schema"] == "generated-resume-v1"
    assert first["job_id"] == 7
    assert first["pages"] == 1
    assert counter.read_text(encoding="utf-8") == "1"
    artifact_dir = Path(first["pdf_path"]).parent
    assert all(Path(first[key]).is_absolute() for key in ("tex_path", "pdf_path", "report_path", "manifest_path"))
    assert all(Path(first[key]).is_file() for key in ("tex_path", "pdf_path", "report_path", "manifest_path"))
    assert {child.name for child in artifact_dir.iterdir()} == {
        "resume.tex",
        "resume.pdf",
        "optimization.json",
        "job_description.txt",
        "manifest.json",
    }


def test_command_changed_description_changes_artifact_ref(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    profile_path, template_path = _command_paths(tmp_path)
    compiler = _write_fake_compiler(tmp_path / "pdflatex")
    arguments = _command_argv(tmp_path, profile_path=profile_path, template_path=template_path, compiler=compiler)
    first_job = _job_snapshot()
    second_job = _job_snapshot(description="Requirements:\n- Python\n- API\n- Docker")

    first_rc, first_out, first_err = _run_direct_command(
        monkeypatch, capsys, arguments, _canonical_snapshot(first_job)
    )
    second_rc, second_out, second_err = _run_direct_command(
        monkeypatch, capsys, arguments, _canonical_snapshot(second_job)
    )
    first = _success_payload(first_out)
    second = _success_payload(second_out)

    assert first_rc == second_rc == 0
    assert first_err == second_err == ""
    assert first["artifact_ref"] != second["artifact_ref"]
    assert Path(first["pdf_path"]).parent != Path(second["pdf_path"]).parent
    assert Path(first["pdf_path"]).parent.joinpath("job_description.txt").read_text(encoding="utf-8") == first_job["description"]
    assert Path(second["pdf_path"]).parent.joinpath("job_description.txt").read_text(encoding="utf-8") == second_job["description"]


def test_v2_profile_schema_parsing_line_filling(tmp_path: Path) -> None:
    repository = Path(__file__).resolve().parents[2]
    user_profile_path = repository / "private" / "resume" / "profile.json"
    profile = load_resume_profile(user_profile_path)
    assert profile.schema_version == 2
    assert len(profile.education) > 0
    assert len(profile.education[0].coursework) > 0
    assert "Data Structures" in profile.education[0].coursework
    assert profile.research is not None
    assert profile.career is not None

    job = _job(title="Software Engineer", description="Requirements:\n- Python\n- C++")
    plan = optimize_resume(profile, job)
    assert len(", ".join(plan.compressed_skills)) >= resume_module._MIN_SKILLS_LINE_CHARS
    assert "Education" in dict(plan.sections)


def test_coursework_optional_expansion(tmp_path: Path) -> None:
    payload = _base_profile()
    payload["schema_version"] = 2
    payload["education"][0]["coursework"] = ["Data Structures", "Algorithms", "Operating Systems"]
    profile_path = _write_profile(tmp_path / "profile.json", payload)
    template_path = _write_template(tmp_path / "Resume.tex")
    compiler = _write_fake_compiler(tmp_path / "pdflatex")
    profile = load_resume_profile(profile_path)

    job = _job(title="Software Engineer", description="Requirements:\n- Data Structures\n- Algorithms")
    result = generate_resume(
        job,
        profile_path=profile_path,
        template_path=template_path,
        output_root=tmp_path / "output",
        compiler=str(compiler),
    )
    assert result.pages == 1
    plan = optimize_resume(profile, job)
    assert plan.selection is not None
    assert plan.selection.selected_coursework == ()
    candidates = resume_module._expansion_candidates(plan, profile, job)
    assert any(candidate.selection and candidate.selection.selected_coursework for candidate in candidates)
    assert "Relevant Coursework" in result.tex_path.read_text(encoding="utf-8")
    assert "Data Structures" in result.tex_path.read_text(encoding="utf-8")
