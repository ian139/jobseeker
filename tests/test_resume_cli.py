"""Tests for resume-generate, resume-show, resume-list, and autofill generated resume CLI workflow."""

import json
import hashlib
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

from jobs_assistant.artifacts import ArtifactRoot
from jobs_assistant.cli import (
    build_parser,
    claim_application_job_with_generated_resume,
    main,
    resolve_generated_resume,
)
from jobs_assistant.db import (
    connect,
    get_generated_resume_private,
    initialize_database,
)


def _setup_db(tmp_path: Path) -> tuple[Path, Path, Path]:
    tmp_path.chmod(0o700)
    db_path = tmp_path / "jobs.db"
    artifacts_dir = tmp_path / "artifacts"
    artifacts_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    gen_dir = tmp_path / "generated-resumes"
    gen_dir.mkdir(mode=0o700, parents=True, exist_ok=True)

    root = ArtifactRoot.open(artifacts_dir, cwd=tmp_path)
    conn = connect(db_path)
    initialize_database(conn, migration_artifact_root=root)
    conn.close()
    root.close()

    return db_path, artifacts_dir, gen_dir


def _insert_test_job(
    db_path: Path,
    *,
    source_job_id: str = "test-job-1",
    title: str = "Senior Python Engineer",
    company: str = "Acme Corp",
    description: str = "Requirements:\n- Python 3.11\n- SQLite\n- Pytest",
    status: str = "queued",
) -> int:
    conn = connect(db_path)
    canonical_url = f"https://example.com/jobs/{source_job_id}"
    cur = conn.execute(
        """
        INSERT INTO jobs (source, source_job_id, canonical_url, title, company, description, status, posted_at, first_seen_at, discovered_at, last_seen_at)
        VALUES ('test', ?, ?, ?, ?, ?, ?, '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z')
        """,
        (source_job_id, canonical_url, title, company, description, status),
    )
    conn.commit()
    job_id = int(cur.lastrowid)
    conn.close()
    return job_id


def _create_sample_profile_file(tmp_path: Path) -> Path:
    profile_path = tmp_path / "profile.json"
    profile_data = {
        "facts": {
            "full_name": "Jane Doe",
            "email": "jane@example.com",
            "skills": ["Python 3.11", "SQLite", "Pytest"],
            "experience": [
                {
                    "title": "Senior Engineer",
                    "company": "Tech Corp",
                    "dates": "2022-2025",
                    "bullets": ["Built Python microservices using SQLite"],
                }
            ],
        },
        "description": "Experienced Python software engineer.",
    }
    profile_path.write_text(json.dumps(profile_data), encoding="utf-8")
    return profile_path


def _create_sample_resume_file(tmp_path: Path) -> Path:
    resume_path = tmp_path / "resume.txt"
    content = "Jane Doe\nEmail: jane@example.com\nSkills: Python 3.11, SQLite, Pytest\nExperience: Senior Engineer at Tech Corp (2022-2025)"
    resume_path.write_text(content, encoding="utf-8")
    return resume_path


def test_resume_cli_parser_and_help(capsys):
    parser = build_parser()

    # resume-generate help
    with pytest.raises(SystemExit) as exc_info:
        parser.parse_args(["resume-generate", "--help"])
    assert exc_info.value.code == 0
    captured = capsys.readouterr()
    assert "--job-id" in captured.out
    assert "--next" in captured.out
    assert "--profile-json" in captured.out
    assert "--source-resume" in captured.out
    assert "--application-artifact-root" in captured.out
    assert "--description-file" in captured.out
    assert "optional job description file" in captured.out

    # resume-show help
    with pytest.raises(SystemExit) as exc_info:
        parser.parse_args(["resume-show", "--help"])
    assert exc_info.value.code == 0
    captured = capsys.readouterr()
    assert "--resume-id" in captured.out
    assert "--application-artifact-root" in captured.out

    # resume-list help
    with pytest.raises(SystemExit) as exc_info:
        parser.parse_args(["resume-list", "--help"])
    assert exc_info.value.code == 0
    captured = capsys.readouterr()
    assert "--job-id" in captured.out
    assert "--limit" in captured.out
    assert "--application-artifact-root" in captured.out

    # autofill help contains generated options
    with pytest.raises(SystemExit) as exc_info:
        parser.parse_args(["autofill", "--help"])
    assert exc_info.value.code == 0
    captured = capsys.readouterr()
    assert "--generated-resume-id" in captured.out
    assert "--generated-resume-artifact-root" in captured.out

def test_resume_cli_selection_conflicts():
    parser = build_parser()

    # resume-generate requires mutually exclusive --job-id / --next
    with pytest.raises(SystemExit) as exc_info:
        parser.parse_args(["resume-generate", "--job-id", "1", "--next", "--profile-json", "p.json", "--source-resume", "r.txt"])
    assert exc_info.value.code == 2

    with pytest.raises(SystemExit) as exc_info:
        parser.parse_args(["resume-generate", "--profile-json", "p.json", "--source-resume", "r.txt"])
    assert exc_info.value.code == 2

    # autofill mutually exclusive --resume-file and --generated-resume-id
    args = parser.parse_args(["autofill", "--generated-resume-id", "RES1", "--resume-file", "custom.pdf"])
    with pytest.raises(SystemExit) as exc_info:
        from jobs_assistant.cli import _validate_autofill_args
        _validate_autofill_args(parser, args)
    assert exc_info.value.code == 2

    # autofill --generated-resume-id requires limit=1
    args2 = parser.parse_args(["autofill", "--generated-resume-id", "RES1", "--limit", "2"])
    with pytest.raises(SystemExit) as exc_info:
        from jobs_assistant.cli import _validate_autofill_args
        _validate_autofill_args(parser, args2)
    assert exc_info.value.code == 2


def test_resume_generate_command_success_and_public_output(tmp_path: Path, capsys):
    db_path, app_root, gen_root = _setup_db(tmp_path)
    job_id = _insert_test_job(db_path)
    profile_file = _create_sample_profile_file(tmp_path)
    resume_file = _create_sample_resume_file(tmp_path)

    ret = main([
        "--db", str(db_path),
        "resume-generate",
        "--job-id", str(job_id),
        "--profile-json", str(profile_file),
        "--source-resume", str(resume_file),
        "--artifact-root", str(gen_root),
        "--application-artifact-root", str(app_root),
    ])
    assert ret == 0

    captured = capsys.readouterr()
    out_json = json.loads(captured.out)
    assert isinstance(out_json, dict)
    assert out_json["job_id"] == job_id
    assert out_json["state"] == "ready"
    assert "resume_id" in out_json
    assert "job_snapshot_sha256" in out_json
    assert "profile_sha256" in out_json

    # Public output must NOT contain private paths or internal score json
    assert "private_pdf_path" not in out_json
    assert "artifact_dir" not in out_json
    assert "score_json" not in out_json
    assert "_description_override" not in out_json
    assert str(tmp_path) not in captured.out
    assert "jane@example.com" not in captured.out

    # Verify DB job status is unchanged (queued) and unmutated
    conn = connect(db_path)
    job_row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
    assert job_row["status"] == "queued"
    conn.close()


def test_resume_generate_replay_and_force(tmp_path: Path, capsys):
    db_path, app_root, gen_root = _setup_db(tmp_path)
    job_id = _insert_test_job(db_path)
    profile_file = _create_sample_profile_file(tmp_path)
    resume_file = _create_sample_resume_file(tmp_path)

    # First generation
    assert main([
        "--db", str(db_path),
        "resume-generate",
        "--job-id", str(job_id),
        "--profile-json", str(profile_file),
        "--source-resume", str(resume_file),
        "--artifact-root", str(gen_root),
        "--application-artifact-root", str(app_root),
    ]) == 0
    res1 = json.loads(capsys.readouterr().out)

    # Replay generation (no --force)
    assert main([
        "--db", str(db_path),
        "resume-generate",
        "--job-id", str(job_id),
        "--profile-json", str(profile_file),
        "--source-resume", str(resume_file),
        "--artifact-root", str(gen_root),
        "--application-artifact-root", str(app_root),
    ]) == 0
    res2 = json.loads(capsys.readouterr().out)

    assert res1["resume_id"] == res2["resume_id"]

    # Forced generation (--force)
    assert main([
        "--db", str(db_path),
        "resume-generate",
        "--job-id", str(job_id),
        "--profile-json", str(profile_file),
        "--source-resume", str(resume_file),
        "--artifact-root", str(gen_root),
        "--application-artifact-root", str(app_root),
        "--force",
    ]) == 0
    res3 = json.loads(capsys.readouterr().out)

    assert res3["resume_id"] != res1["resume_id"]


def test_resume_generate_failure_leaves_job_unmutated(tmp_path: Path, capsys):
    db_path, app_root, gen_root = _setup_db(tmp_path)
    job_id = _insert_test_job(db_path)
    profile_file = _create_sample_profile_file(tmp_path)
    resume_file = _create_sample_resume_file(tmp_path)

    with patch("jobs_assistant.resume_service.render_resume_pdf", side_effect=ValueError("Simulated rendering failure")):
        ret = main([
            "--db", str(db_path),
            "resume-generate",
            "--job-id", str(job_id),
            "--profile-json", str(profile_file),
            "--source-resume", str(resume_file),
            "--artifact-root", str(gen_root),
            "--application-artifact-root", str(app_root),
        ])
        assert ret == 1

    captured = capsys.readouterr()
    err_json = json.loads(captured.err)
    assert "error" in err_json
    assert err_json["error"]["code"] == "invalid_input"
    assert str(tmp_path) not in captured.out
    assert str(tmp_path) not in captured.err

    # Confirm job is still queued and unclaimed in DB
    conn = connect(db_path)
    job_row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
    assert job_row["status"] == "queued"
    conn.close()

def test_resume_show_and_list_commands(tmp_path: Path, capsys):
    db_path, app_root, gen_root = _setup_db(tmp_path)
    job_id = _insert_test_job(db_path)
    profile_file = _create_sample_profile_file(tmp_path)
    resume_file = _create_sample_resume_file(tmp_path)

    # Generate a resume
    assert main([
        "--db", str(db_path),
        "resume-generate",
        "--job-id", str(job_id),
        "--profile-json", str(profile_file),
        "--source-resume", str(resume_file),
        "--artifact-root", str(gen_root),
        "--application-artifact-root", str(app_root),
    ]) == 0
    gen_res = json.loads(capsys.readouterr().out)
    resume_id = gen_res["resume_id"]

    # resume-show existing ID
    assert main([
        "--db", str(db_path),
        "resume-show",
        "--resume-id", resume_id,
        "--artifact-root", str(gen_root),
        "--application-artifact-root", str(app_root),
    ]) == 0
    show_res = json.loads(capsys.readouterr().out)
    assert show_res["resume_id"] == resume_id
    assert "private_pdf_path" not in show_res
    assert "_description_override" not in show_res

    # resume-show missing ID
    assert main([
        "--db", str(db_path),
        "resume-show",
        "--resume-id", "missing-id-123",
        "--artifact-root", str(gen_root),
        "--application-artifact-root", str(app_root),
    ]) == 1
    err_out = json.loads(capsys.readouterr().err)
    assert err_out["error"]["code"] == "invalid_input"

    # resume-list with job-id filter
    assert main([
        "--db", str(db_path),
        "resume-list",
        "--job-id", str(job_id),
        "--artifact-root", str(gen_root),
        "--application-artifact-root", str(app_root),
    ]) == 0
    list_res = json.loads(capsys.readouterr().out)
    assert "resumes" in list_res
    assert len(list_res["resumes"]) == 1
    assert list_res["resumes"][0]["resume_id"] == resume_id
    assert "private_pdf_path" not in list_res["resumes"][0]
    assert "_description_override" not in list_res["resumes"][0]

    # resume-list for job with no resumes
    assert main([
        "--db", str(db_path),
        "resume-list",
        "--job-id", "9999",
        "--artifact-root", str(gen_root),
        "--application-artifact-root", str(app_root),
    ]) == 0
    list_empty = json.loads(capsys.readouterr().out)
    assert list_empty["resumes"] == []


def test_autofill_generated_resume_mode_success(tmp_path: Path, capsys, monkeypatch):
    db_path, app_root, gen_root = _setup_db(tmp_path)
    job_id = _insert_test_job(db_path)
    profile_file = _create_sample_profile_file(tmp_path)
    resume_file = _create_sample_resume_file(tmp_path)

    # Generate a ready resume
    assert main([
        "--db", str(db_path),
        "resume-generate",
        "--job-id", str(job_id),
        "--profile-json", str(profile_file),
        "--source-resume", str(resume_file),
        "--artifact-root", str(gen_root),
        "--application-artifact-root", str(app_root),
    ]) == 0
    gen_res = json.loads(capsys.readouterr().out)
    resume_id = gen_res["resume_id"]

    monkeypatch.setattr("jobs_assistant.cli.PuppeteerSession.preflight", lambda headed=False: None)

    workflow_calls: list[dict[str, object]] = []

    async def fake_workflow(conn, **kwargs):
        workflow_calls.append(kwargs)
        # Execute the claim_provider provided by generated mode
        claim_provider = kwargs.get("claim_provider")
        assert claim_provider is not None
        claim = claim_provider(conn, owner="test-owner")
        assert claim is not None
        return [{
            "job_id": job_id,
            "run_id": claim.run_id,
            "status": "review_ready",
            "reason_code": "draft_ready",
            "ats": "greenhouse",
            "artifact_ref": f"run-{claim.run_id}",
            "window_state": "closed",
        }]

    monkeypatch.setattr("jobs_assistant.cli.run_application_workflow", fake_workflow)

    ret = main([
        "--db", str(db_path),
        "autofill",
        "--generated-resume-id", resume_id,
        "--profile-json", str(profile_file),
        "--artifact-root", str(app_root),
        "--generated-resume-artifact-root", str(gen_root),
    ])
    assert ret == 0

    captured = capsys.readouterr()
    autofill_out = json.loads(captured.out)
    assert len(autofill_out["results"]) == 1
    assert autofill_out["results"][0]["job_id"] == job_id

    assert len(workflow_calls) == 1
    call_kwargs = workflow_calls[0]
    assert call_kwargs["limit"] == 1
    selected_resume = Path(str(call_kwargs["resume_file"]))
    assert selected_resume != resume_file
    assert selected_resume.name == "resume.pdf"
    assert selected_resume.is_file()
    assert selected_resume.read_bytes()
    assert call_kwargs["expected_resume_sha256"] == gen_res["pdf_sha256"]
    assert hashlib.sha256(selected_resume.read_bytes()).hexdigest() == call_kwargs["expected_resume_sha256"]
    assert call_kwargs["expected_profile_sha256"] == gen_res["profile_sha256"]

    # Verify atomic binding was created in DB
    conn = connect(db_path)
    binding = conn.execute("SELECT * FROM application_resume_bindings WHERE resume_id = ?", (resume_id,)).fetchone()
    assert binding is not None
    assert binding["run_id"] == autofill_out["results"][0]["run_id"]
    conn.close()


def test_autofill_generated_resume_rejections_before_claim(tmp_path: Path, capsys, monkeypatch):
    db_path, app_root, gen_root = _setup_db(tmp_path)
    job_id1 = _insert_test_job(db_path, source_job_id="job-1")
    profile_file = _create_sample_profile_file(tmp_path)
    resume_file = _create_sample_resume_file(tmp_path)

    # Generate resume for job 1
    assert main([
        "--db", str(db_path),
        "resume-generate",
        "--job-id", str(job_id1),
        "--profile-json", str(profile_file),
        "--source-resume", str(resume_file),
        "--artifact-root", str(gen_root),
        "--application-artifact-root", str(app_root),
    ]) == 0
    gen_res = json.loads(capsys.readouterr().out)
    resume_id = gen_res["resume_id"]

    monkeypatch.setattr("jobs_assistant.cli.PuppeteerSession.preflight", lambda headed=False: None)

    # Rejection case 1: Stale profile (profile JSON changed since generation)
    stale_profile = tmp_path / "stale_profile.json"
    stale_profile.write_text(json.dumps({"facts": {"full_name": "Different Name"}}), encoding="utf-8")

    ret_stale = main([
        "--db", str(db_path),
        "autofill",
        "--generated-resume-id", resume_id,
        "--profile-json", str(stale_profile),
        "--artifact-root", str(app_root),
        "--generated-resume-artifact-root", str(gen_root),
    ])
    assert ret_stale == 1
    captured = capsys.readouterr()
    err_stale = json.loads(captured.err)
    assert err_stale["error"]["code"] == "invalid_input"
    assert str(tmp_path) not in json.dumps(err_stale)

    # Rejection case 2: PDF deleted from artifact root
    conn = connect(db_path)
    rec = get_generated_resume_private(conn, resume_id)
    pdf_path = Path(rec["private_pdf_path"])
    pdf_path.unlink()
    conn.close()

    ret_missing_pdf = main([
        "--db", str(db_path),
        "autofill",
        "--generated-resume-id", resume_id,
        "--profile-json", str(profile_file),
        "--artifact-root", str(app_root),
        "--generated-resume-artifact-root", str(gen_root),
    ])
    assert ret_missing_pdf == 1
    captured = capsys.readouterr()
    err_pdf = json.loads(captured.err)
    assert err_pdf["error"]["code"] == "invalid_input"
    assert str(tmp_path) not in json.dumps(err_pdf)
    conn = connect(db_path)
    job_row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id1,)).fetchone()
    assert job_row["status"] == "queued"
    conn.close()

def test_autofill_default_and_explicit_resume_file_compatibility(tmp_path: Path, capsys, monkeypatch):
    db_path, app_root, gen_root = _setup_db(tmp_path)
    profile_file = _create_sample_profile_file(tmp_path)
    explicit_resume = _create_sample_resume_file(tmp_path)

    monkeypatch.setattr("jobs_assistant.cli.PuppeteerSession.preflight", lambda headed=False: None)
    monkeypatch.setattr("jobs_assistant.cli.load_resume_context", lambda path: MagicMock(__enter__=lambda s: None, __exit__=lambda s, *a: None))

    captured_kwargs: list[dict[str, object]] = []

    async def fake_workflow(conn, **kwargs):
        captured_kwargs.append(kwargs)
        return []

    monkeypatch.setattr("jobs_assistant.cli.run_application_workflow", fake_workflow)

    # 1. Default resume file compatibility
    assert main([
        "--db", str(db_path),
        "autofill",
        "--profile-json", str(profile_file),
        "--artifact-root", str(app_root),
    ]) == 0
    assert len(captured_kwargs) == 1
    assert captured_kwargs[0]["resume_file"] == "resume/Main_Resume.pdf"
    assert "claim_provider" not in captured_kwargs[0]

    # 2. Explicit resume file compatibility
    captured_kwargs.clear()
    assert main([
        "--db", str(db_path),
        "autofill",
        "--resume-file", str(explicit_resume),
        "--profile-json", str(profile_file),
        "--artifact-root", str(app_root),
    ]) == 0
    assert len(captured_kwargs) == 1
    assert captured_kwargs[0]["resume_file"] == str(explicit_resume)
    assert "claim_provider" not in captured_kwargs[0]
def test_resume_generate_with_default_application_artifact_root_and_bound_db(tmp_path: Path, capsys, monkeypatch):
    """Simulate DB bound to application root then resume root generation succeeds with distinct roots."""
    monkeypatch.chdir(tmp_path)
    db_path = tmp_path / "jobs.db"
    app_root = tmp_path / "data" / "application-runs"
    app_root.mkdir(mode=0o700, parents=True, exist_ok=True)
    gen_root = tmp_path / "data" / "generated-resumes"
    gen_root.mkdir(mode=0o700, parents=True, exist_ok=True)

    # Bind DB to data/application-runs (the DEFAULT_ARTIFACT_ROOT)
    root = ArtifactRoot.open(app_root, cwd=tmp_path)
    conn = connect(db_path)
    initialize_database(conn, migration_artifact_root=root)
    conn.close()
    root.close()

    job_id = _insert_test_job(db_path)
    profile_file = _create_sample_profile_file(tmp_path)
    resume_file = _create_sample_resume_file(tmp_path)

    # Default application-artifact-root is DEFAULT_ARTIFACT_ROOT ("data/application-runs")
    ret = main([
        "--db", str(db_path),
        "resume-generate",
        "--job-id", str(job_id),
        "--profile-json", str(profile_file),
        "--source-resume", str(resume_file),
        "--artifact-root", str(gen_root),
    ])
    assert ret == 0
    res = json.loads(capsys.readouterr().out)
    assert res["state"] == "ready"
    assert res["job_id"] == job_id

    # Also test resume-show and resume-list with default application-artifact-root
    assert main([
        "--db", str(db_path),
        "resume-show",
        "--resume-id", res["resume_id"],
        "--artifact-root", str(gen_root),
    ]) == 0

    assert main([
        "--db", str(db_path),
        "resume-list",
        "--job-id", str(job_id),
        "--artifact-root", str(gen_root),
    ]) == 0


def test_autofill_generated_resume_description_override_and_gen_root_cleanup(tmp_path: Path, capsys, monkeypatch):
    """Test generated autofill passes description override to atomic claim and closes gen root deterministically."""
    db_path, app_root, gen_root = _setup_db(tmp_path)
    job_id = _insert_test_job(db_path)
    profile_file = _create_sample_profile_file(tmp_path)
    resume_file = _create_sample_resume_file(tmp_path)
    desc_file = tmp_path / "job_desc.txt"
    desc_file.write_text("Override requirement: Senior Python Engineer with 5 years experience.", encoding="utf-8")

    # Generate resume with explicit description file
    assert main([
        "--db", str(db_path),
        "resume-generate",
        "--job-id", str(job_id),
        "--profile-json", str(profile_file),
        "--source-resume", str(resume_file),
        "--artifact-root", str(gen_root),
        "--application-artifact-root", str(app_root),
        "--description-file", str(desc_file),
    ]) == 0
    gen_res = json.loads(capsys.readouterr().out)
    resume_id = gen_res["resume_id"]

    monkeypatch.setattr("jobs_assistant.cli.PuppeteerSession.preflight", lambda headed=False: None)

    original_resolve = resolve_generated_resume

    def spy_resolve(conn, resume_id, artifact_root, expected_profile_sha256=None):
        rec = original_resolve(conn, resume_id, artifact_root, expected_profile_sha256=expected_profile_sha256)
        rec["_description_override"] = "Override requirement: Senior Python Engineer with 5 years experience."
        return rec

    monkeypatch.setattr("jobs_assistant.cli.resolve_generated_resume", spy_resolve)

    claim_args: list[dict[str, object]] = []

    original_claim = claim_application_job_with_generated_resume

    def spy_claim(conn, *, owner, job_id, resume_id, expected_job_snapshot_sha256, description_override=None):
        claim_args.append({
            "owner": owner,
            "job_id": job_id,
            "resume_id": resume_id,
            "expected_job_snapshot_sha256": expected_job_snapshot_sha256,
            "description_override": description_override,
        })
        return original_claim(
            conn,
            owner=owner,
            job_id=job_id,
            resume_id=resume_id,
            expected_job_snapshot_sha256=expected_job_snapshot_sha256,
            description_override=description_override,
        )

    monkeypatch.setattr("jobs_assistant.cli.claim_application_job_with_generated_resume", spy_claim)


    async def fake_workflow(conn, **kwargs):
        claim_provider = kwargs.get("claim_provider")
        assert claim_provider is not None

        # Resolve/open check: during workflow execution, check if gen_root in CLI is open
        claim = claim_provider(conn, owner="test-owner")
        assert claim is not None

        return [{
            "job_id": job_id,
            "run_id": claim.run_id,
            "status": "review_ready",
            "reason_code": "draft_ready",
            "ats": "greenhouse",
            "artifact_ref": f"run-{claim.run_id}",
            "window_state": "closed",
        }]

    monkeypatch.setattr("jobs_assistant.cli.run_application_workflow", fake_workflow)

    ret = main([
        "--db", str(db_path),
        "autofill",
        "--generated-resume-id", resume_id,
        "--profile-json", str(profile_file),
        "--artifact-root", str(app_root),
        "--generated-resume-artifact-root", str(gen_root),
    ])
    assert ret == 0

    captured = capsys.readouterr()
    autofill_out = json.loads(captured.out)
    assert len(autofill_out["results"]) == 1
    assert "_description_override" not in captured.out
    assert str(desc_file) not in captured.out

    # Verify claim received the description override from _description_override
    assert len(claim_args) == 1
    assert claim_args[0]["description_override"] == "Override requirement: Senior Python Engineer with 5 years experience."

    # Test failure case cleanup: verify gen_root is closed when workflow fails
    async def failing_workflow(conn, **kwargs):
        raise ValueError("Simulated workflow failure")

    monkeypatch.setattr("jobs_assistant.cli.run_application_workflow", failing_workflow)

    ret_fail = main([
        "--db", str(db_path),
        "autofill",
        "--generated-resume-id", resume_id,
        "--profile-json", str(profile_file),
        "--artifact-root", str(app_root),
        "--generated-resume-artifact-root", str(gen_root),
    ])
    assert ret_fail == 1
    captured_fail = capsys.readouterr()
    assert "_description_override" not in captured_fail.err
