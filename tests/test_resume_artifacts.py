from __future__ import annotations

import hashlib
import json
import os
import stat
import uuid
from pathlib import Path

import pytest
from pypdf import PdfReader

from jobs_assistant.resume_artifacts import (
    ARTIFACT_FILENAMES,
    DOCUMENT_FILENAMES,
    PersistedResumeRun,
    ResumeArtifactSecurityError,
    ResumeArtifactStore,
    ResumeRenderError,
    ResumeValidationError,
    _validate_resume,
    render_resume_pdf,
)


HASH = "0123456789abcdef" * 4


def resume_mapping(*, headline: bool = True, long: bool = False) -> dict[str, object]:
    facts = " ".join(["Built reliable systems"] * (700 if long else 1))
    value: dict[str, object] = {
        "schema_version": 1,
        "job_snapshot_sha256": HASH,
        "profile_sha256": HASH,
        "source_resume_sha256": HASH,
        "headline": {
            "text": "Ada Lovelace" if headline else "",
            "claim_ids": ["claim-name"] if headline else [],
        },
        "summary": {"text": "Engineer who builds reliable systems", "claim_ids": ["claim-summary"]},
        "skills": [
            {"name": "Python", "claim_ids": ["claim-python"]},
            {"name": "SQLite", "claim_ids": ["claim-sqlite"]},
        ],
        "experience": [
            {
                "source_entry_id": "exp-acme",
                "organization": "Acme Labs",
                "role": "Staff Engineer",
                "dates": "2020-2024",
                "bullets": [{"text": facts, "claim_ids": ["claim-acme"]}],
            }
        ],
        "education": [
            {
                "institution": "Example University",
                "degree": "B.S. Computer Science",
                "dates": "2016-2020",
                "bullets": [{"text": "Studied distributed systems", "claim_ids": ["claim-edu"]}],
            }
        ],
        "omitted_claim_ids": ["OMITTED_CLAIM_DO_NOT_RENDER"],
        "missing_fact_questions": ["MISSING_FACT_QUESTION_DO_NOT_RENDER"],
        "generation_notes": ["INTERNAL_GENERATION_NOTE_DO_NOT_RENDER"],
    }
    return value


def provenance() -> dict[str, str]:
    return {
        "job_snapshot_sha256": HASH,
        "profile_sha256": HASH,
        "source_resume_sha256": HASH,
        "generation_config_sha256": HASH,
    }


def documents() -> dict[str, object]:
    return {name: {"artifact": name, "version": 1} for name in DOCUMENT_FILENAMES}


def test_render_is_deterministic_and_reports_exact_pdf_hash() -> None:
    first = render_resume_pdf(resume_mapping())
    second = render_resume_pdf(dict(reversed(list(resume_mapping().items()))))

    assert first.bytes_data == second.bytes_data
    assert first.sha256 == second.sha256 == hashlib.sha256(first.bytes_data).hexdigest()
    assert first.pdf_sha256 == first.sha256
    assert first.page_count == 1
    assert PdfReader(__import__("io").BytesIO(first.bytes_data)).pages


def test_renderer_accepts_empty_headline_and_summary_without_claim_ids() -> None:
    value = resume_mapping()
    value["headline"] = {"text": "", "claim_ids": []}
    value["summary"] = {"text": "\n\t", "claim_ids": []}

    rendered = render_resume_pdf(value)

    assert rendered.page_count == 1
    assert "Ada Lovelace" not in rendered.extracted_text
    assert "Engineer who builds reliable systems" not in rendered.extracted_text


@pytest.mark.parametrize(
    ("field", "leaf"),
    (
        ("headline", {"text": " \n ", "claim_ids": ["claim-name"]}),
        ("summary", {"text": "Engineer", "claim_ids": []}),
    ),
)
def test_renderer_rejects_leaf_citation_cardinality_mismatches(
    field: str, leaf: dict[str, object]
) -> None:
    value = resume_mapping()
    value[field] = leaf

    with pytest.raises(ResumeValidationError):
        render_resume_pdf(value)


def test_rendered_text_is_pypdf_verified_and_contains_only_input_facts() -> None:
    rendered = render_resume_pdf(resume_mapping())
    extracted = rendered.extracted_text

    for expected in (
        "Ada Lovelace",
        "Engineer who builds reliable systems",
        "Python",
        "SQLite",
        "Acme Labs",
        "Staff Engineer",
        "2020-2024",
        "Built reliable systems",
        "Example University",
        "B.S. Computer Science",
        "Studied distributed systems",
    ):
        assert expected in extracted
    assert "keyword stuffing" not in extracted
    assert "SHOULD_NOT_APPEAR" not in extracted
    for hidden in (
        "OMITTED_CLAIM_DO_NOT_RENDER",
        "MISSING_FACT_QUESTION_DO_NOT_RENDER",
        "INTERNAL_GENERATION_NOTE_DO_NOT_RENDER",
        "exp-acme",
        "claim-acme",
    ):
        assert hidden not in extracted
    assert _collapsed(PdfReader(__import__("io").BytesIO(rendered.bytes_data)).pages[0].extract_text() or "") == _collapsed(extracted)


def test_render_rejects_unbounded_resume_and_invalid_shape() -> None:
    with pytest.raises(ResumeRenderError):
        render_resume_pdf(resume_mapping(long=True))
    with pytest.raises(ValueError):
        render_resume_pdf(resume_mapping(), max_pages=0)
    bad = resume_mapping()
    bad["untrusted_template"] = "<script>ignored</script>"
    with pytest.raises(ResumeValidationError):
        render_resume_pdf(bad)


def test_render_rejects_legacy_and_wrong_nested_schema() -> None:
    legacy = resume_mapping()
    legacy["provenance"] = provenance()
    with pytest.raises(ResumeValidationError):
        render_resume_pdf(legacy)

    wrong_experience = resume_mapping()
    experience = dict(wrong_experience["experience"][0])  # type: ignore[index]
    del experience["organization"]
    experience["org"] = "Acme Labs"
    wrong_experience["experience"] = [experience]
    with pytest.raises(ResumeValidationError):
        render_resume_pdf(wrong_experience)

    missing_metadata = resume_mapping()
    del missing_metadata["generation_notes"]
    with pytest.raises(ResumeValidationError):
        render_resume_pdf(missing_metadata)


def test_schema_version_requires_exact_integer_one() -> None:
    normalized, _, _ = _validate_resume(resume_mapping())
    assert type(normalized["schema_version"]) is int
    assert normalized["schema_version"] == 1

    for invalid_schema_version in ("1", True, False, -1, 0, 2):
        invalid = resume_mapping()
        invalid["schema_version"] = invalid_schema_version
        with pytest.raises(ResumeValidationError, match="schema_version"):
            render_resume_pdf(invalid)


def test_owner_private_modes_and_complete_manifest(tmp_path: Path) -> None:
    root = tmp_path / "artifacts"
    store = ResumeArtifactStore(root)
    run_id = str(uuid.uuid4())
    rendered = render_resume_pdf(resume_mapping())
    result = store.persist_success(
        run_id,
        documents=documents(),
        validated_resume=resume_mapping(),
        rendered=rendered,
        provenance=provenance(),
    )

    assert isinstance(result, PersistedResumeRun)
    run_dir = result.private_pdf_path.parent
    assert stat.S_IMODE(root.stat().st_mode) == 0o700
    assert stat.S_IMODE(run_dir.stat().st_mode) == 0o700
    assert all(stat.S_IMODE(path.stat().st_mode) == 0o600 for path in run_dir.iterdir())
    assert {path.name for path in run_dir.iterdir()} == set(ARTIFACT_FILENAMES)
    manifest_path = run_dir / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    assert result.manifest_sha256 == hashlib.sha256(manifest_path.read_bytes()).hexdigest()
    assert manifest["content_sha256"] == result.content_sha256
    assert manifest["pdf_sha256"] == rendered.sha256
    assert manifest["provenance"] == provenance()
    assert json.loads((run_dir / "resume.json").read_text()) == resume_mapping()
    for name in ARTIFACT_FILENAMES[:-1]:
        payload = (run_dir / name).read_bytes()
        assert manifest["artifacts"][name] == {"bytes": len(payload), "sha256": hashlib.sha256(payload).hexdigest()}
    assert not list(run_dir.glob(".*.tmp"))


def test_store_rejects_traversal_symlinks_and_duplicate_runs(tmp_path: Path) -> None:
    with pytest.raises(ResumeArtifactSecurityError):
        ResumeArtifactStore(tmp_path / ".." / "escape")
    root = tmp_path / "artifacts"
    root.mkdir()
    os.chmod(root, 0o700)
    store = ResumeArtifactStore(root)
    with pytest.raises(ResumeArtifactSecurityError):
        store.persist_failure("../escape", reason_code="RENDERER_FAILURE")
    outside = tmp_path / "outside"
    outside.mkdir()
    os.symlink(outside, root / str(uuid.uuid4()))
    with pytest.raises(ResumeArtifactSecurityError):
        store.persist_failure(next(path.name for path in root.iterdir()), reason_code="RENDERER_FAILURE")
    valid = str(uuid.uuid4())
    store.persist_failure(valid, reason_code="RENDERER_FAILURE")
    with pytest.raises(ResumeArtifactSecurityError):
        store.persist_failure(valid, reason_code="RENDERER_FAILURE")


def test_renderer_failure_retains_redacted_evidence_without_success_manifest(tmp_path: Path) -> None:
    store = ResumeArtifactStore(tmp_path / "artifacts")
    run_id = str(uuid.uuid4())
    with pytest.raises(ResumeRenderError):
        store.render_and_persist(
            run_id,
            documents=documents(),
            validated_resume=resume_mapping(long=True),
            provenance=provenance(),
        )
    run_dir = (tmp_path / "artifacts" / run_id)
    failure = json.loads((run_dir / "failure.json").read_text())
    assert set(failure) == {"reason_code", "evidence_count", "evidence_sha256"}
    assert failure["reason_code"] == "RENDERER_FAILURE"
    assert "Built reliable systems" not in (run_dir / "failure.json").read_text()
    assert not (run_dir / "manifest.json").exists()
    assert set(path.name for path in run_dir.iterdir()) == {"failure.json"}


def test_persist_never_touches_source_resume(tmp_path: Path) -> None:
    source = tmp_path / "source-resume.pdf"
    original = b"source resume bytes that must never be replaced"
    source.write_bytes(original)
    store = ResumeArtifactStore(tmp_path / "artifacts")
    store.persist_success(
        str(uuid.uuid4()),
        documents=documents(),
        validated_resume=resume_mapping(),
        rendered=render_resume_pdf(resume_mapping()),
        provenance=provenance(),
    )
    assert source.read_bytes() == original


def _collapsed(value: str) -> str:
    return " ".join(value.split())
