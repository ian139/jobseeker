"""Tests for backlog-to-resume database operations and generated_resumes schema."""

from pathlib import Path
import sqlite3
import pytest
from typing import Any

from jobs_assistant.artifacts import ArtifactRoot
from jobs_assistant.db import (
    initialize_database,
    read_resume_job,
    get_job_resume_snapshot,
    get_next_queued_job_resume_snapshot,
    claim_application_job,
    claim_application_job_with_generated_resume,
    get_ready_generated_resume,
    create_generated_resume,
    transition_generated_resume_state,
    get_generated_resume,
    get_generated_resume_private,
    list_generated_resumes,
    format_public_generated_resume,
    bind_generated_resume_to_application,
    GENERATED_RESUMES_STATES,
)
from jobs_assistant.resume import JobResumeSnapshot, GeneratedResumeArtifact


def _id(obj: Any) -> str:
    if hasattr(obj, "resume_id"):
        return str(obj.resume_id)
    return str(obj["resume_id"])


def _setup_db(tmp_path: Path) -> tuple[sqlite3.Connection, ArtifactRoot]:
    db_path = tmp_path / "jobs.db"
    artifacts_dir = tmp_path / "artifacts"
    root = ArtifactRoot.open(artifacts_dir, cwd=tmp_path)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON;")
    initialize_database(conn, migration_artifact_root=root)
    return conn, root


def _insert_test_job(
    conn: sqlite3.Connection,
    *,
    source_job_id: str = "test-job-1",
    title: str = "Senior Engineer",
    company: str = "Acme Corp",
    description: str = "Requirements:\n- Python 3.11\n- SQLite",
    status: str = "queued",
    posted_at: str | None = "2026-01-01T00:00:00Z",
    first_seen_at: str = "2026-01-01T00:00:00Z",
    canonical_url: str | None = None,
) -> int:
    if canonical_url is None:
        canonical_url = f"https://example.com/jobs/{source_job_id}"
    cur = conn.execute(
        """
        INSERT INTO jobs (source, source_job_id, canonical_url, title, company, description, status, posted_at, first_seen_at, discovered_at, last_seen_at)
        VALUES ('test', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (source_job_id, canonical_url, title, company, description, status, posted_at, first_seen_at, first_seen_at, first_seen_at),
    )
    conn.commit()
    return int(cur.lastrowid)


def test_read_resume_job_explicit_and_next(tmp_path: Path) -> None:
    conn, _ = _setup_db(tmp_path)
    job_id_1 = _insert_test_job(conn, source_job_id="job-1", title="DevOps Lead")
    _insert_test_job(conn, source_job_id="job-2", title="Frontend Architect")

    # Explicit query
    snapshot_1 = read_resume_job(conn, job_id=job_id_1)
    assert isinstance(snapshot_1, JobResumeSnapshot)
    assert snapshot_1.job_id == job_id_1
    assert snapshot_1.title == "DevOps Lead"
    assert snapshot_1.company == "Acme Corp"
    assert len(snapshot_1.job_snapshot_sha256) == 64

    # Wrapper explicit query
    snapshot_1_wrap = get_job_resume_snapshot(conn, job_id=job_id_1)
    assert snapshot_1_wrap == snapshot_1

    # Next queued query
    snapshot_next = get_next_queued_job_resume_snapshot(conn)
    assert isinstance(snapshot_next, JobResumeSnapshot)

    # Verify SELECT queries did NOT mutate jobs table
    rows = conn.execute("SELECT status FROM jobs ORDER BY id").fetchall()
    assert [r["status"] for r in rows] == ["queued", "queued"]


def test_next_queued_deterministic_order(tmp_path: Path) -> None:
    conn, _ = _setup_db(tmp_path)
    # Insert jobs with varied posted_at, first_seen_at, id
    _insert_test_job(conn, source_job_id="j1", title="Job 1", posted_at="2026-01-01T00:00:00Z", first_seen_at="2026-01-01T10:00:00Z")
    j2 = _insert_test_job(conn, source_job_id="j2", title="Job 2", posted_at="2026-01-02T00:00:00Z", first_seen_at="2026-01-01T05:00:00Z")
    _insert_test_job(conn, source_job_id="j3", title="Job 3", posted_at=None, first_seen_at="2026-01-01T01:00:00Z")
    # Ties are resolved by earliest first_seen_at, then lowest id.
    _insert_test_job(conn, source_job_id="j4", title="Job 4", posted_at="2026-01-02T00:00:00Z", first_seen_at="2026-01-01T06:00:00Z")
    _insert_test_job(conn, source_job_id="j5", title="Job 5", posted_at="2026-01-02T00:00:00Z", first_seen_at="2026-01-01T05:00:00Z")

    # Order should be posted_at DESC NULLS LAST, first_seen_at ASC, id ASC
    # j2 has latest posted_at ("2026-01-02"), so j2 comes first.
    snapshot = read_resume_job(conn, next_queued=True)
    assert isinstance(snapshot, JobResumeSnapshot)
    assert snapshot.job_id == j2
    # Repeated reads remain deterministic and do not claim the selected job.
    assert read_resume_job(conn, next_queued=True).job_id == j2
    assert conn.execute("SELECT status FROM jobs WHERE id=?", (j2,)).fetchone()["status"] == "queued"


def test_missing_rows_and_invalid_args(tmp_path: Path) -> None:
    conn, _ = _setup_db(tmp_path)
    # Missing explicit job
    assert read_resume_job(conn, job_id=99999) is None
    assert get_job_resume_snapshot(conn, 99999) is None

    # Empty database next queued
    assert read_resume_job(conn, next_queued=True) is None
    assert get_next_queued_job_resume_snapshot(conn) is None

    # Invalid job_id type
    with pytest.raises(TypeError):
        read_resume_job(conn, job_id="invalid-id")


def test_claim_application_job_success_and_non_queued(tmp_path: Path) -> None:
    conn, _ = _setup_db(tmp_path)
    job_id = _insert_test_job(conn, source_job_id="claim-job-1", status="queued")

    # Claiming non-existent job does not mutate state
    assert claim_application_job(conn, owner="test-owner", job_id=9999) is None
    assert conn.execute("SELECT status FROM jobs WHERE id=?", (job_id,)).fetchone()["status"] == "queued"
    assert conn.execute("SELECT COUNT(*) FROM application_runs").fetchone()[0] == 0

    # Claim exact queued job (positional and keyword args support)
    claim = claim_application_job(conn, owner="test-owner", job_id=job_id)
    assert claim is not None
    assert claim.job["id"] == job_id
    assert conn.execute("SELECT status FROM jobs WHERE id=?", (job_id,)).fetchone()["status"] == "in_progress"
    assert conn.execute("SELECT COUNT(*) FROM application_runs WHERE job_id=?", (job_id,)).fetchone()[0] == 1

    # Claiming already in_progress job returns None and does not mutate further
    assert claim_application_job(conn, owner="test-owner-2", job_id=job_id) is None
    assert conn.execute("SELECT COUNT(*) FROM application_runs WHERE job_id=?", (job_id,)).fetchone()[0] == 1


def test_atomic_generated_resume_claim_validates_and_binds(tmp_path: Path) -> None:
    conn, _ = _setup_db(tmp_path)

    def ready_resume(job_id: int, resume_id: str) -> str:
        snapshot = read_resume_job(conn, job_id=job_id)
        assert isinstance(snapshot, JobResumeSnapshot)
        resume = create_generated_resume(
            conn,
            job_id=job_id,
            resume_id=resume_id,
            job_snapshot_sha256=snapshot.job_snapshot_sha256,
            profile_sha256="1" * 64,
            source_resume_sha256="2" * 64,
            generation_config_sha256="3" * 64,
            state="ready",
            artifact_dir=f"/artifacts/{resume_id}",
            content_sha256="4" * 64,
            pdf_sha256="5" * 64,
            private_pdf_path=f"/private/{resume_id}.pdf",
        )
        return _id(resume)

    j1 = _insert_test_job(conn, source_job_id="atomic-j1")
    j2 = _insert_test_job(conn, source_job_id="atomic-j2")
    j3 = _insert_test_job(conn, source_job_id="atomic-j3")
    j4 = _insert_test_job(conn, source_job_id="atomic-j4", status="in_progress")
    j5 = _insert_test_job(conn, source_job_id="atomic-j5")
    r1 = ready_resume(j1, "atomic-r1")
    r2 = ready_resume(j2, "atomic-r2")
    r3 = ready_resume(j3, "atomic-r3")
    transition_generated_resume_state(conn, resume_id=r3, to_state="superseded")
    r4 = ready_resume(j4, "atomic-r4")
    r5 = create_generated_resume(
        conn,
        job_id=j5,
        resume_id="atomic-r5",
        job_snapshot_sha256=read_resume_job(conn, job_id=j5).job_snapshot_sha256,
        profile_sha256="1" * 64,
        source_resume_sha256="2" * 64,
        generation_config_sha256="3" * 64,
        state="generating",
    )
    r5_id = _id(r5)

    def snapshot(job_id: int) -> tuple[str, int, int]:
        return (
            str(conn.execute("SELECT status FROM jobs WHERE id=?", (job_id,)).fetchone()["status"]),
            int(conn.execute("SELECT COUNT(*) FROM application_runs").fetchone()[0]),
            int(conn.execute("SELECT COUNT(*) FROM application_resume_bindings").fetchone()[0]),
        )

    with pytest.raises(TypeError, match="expected_job_snapshot_sha256"):
        claim_application_job_with_generated_resume(
            conn,
            owner="atomic-owner",
            job_id=j1,
            resume_id=r1,
            expected_job_snapshot_sha256="a" * 63,
        )
    assert snapshot(j1) == ("queued", 0, 0)

    # Every mismatch is rejected without changing the target job or either side table.
    for target_job, target_resume in (
        (j1, r2),  # wrong job
        (j3, r3),  # superseded/stale resume
        (j5, r5_id),  # non-ready resume
        (j1, "missing-resume"),  # missing resume
        (j4, r4),  # no queued job
    ):
        before = snapshot(target_job)
        assert claim_application_job_with_generated_resume(
            conn,
            owner="atomic-owner",
            job_id=target_job,
            resume_id=target_resume,
            expected_job_snapshot_sha256=read_resume_job(conn, job_id=target_job).job_snapshot_sha256,
        ) is None
        assert snapshot(target_job) == before

    claim = claim_application_job_with_generated_resume(
        conn,
        owner="atomic-owner",
        job_id=j1,
        resume_id=r1,
        expected_job_snapshot_sha256=read_resume_job(conn, job_id=j1).job_snapshot_sha256,
    )
    assert claim is not None
    assert claim.job["id"] == j1
    assert conn.execute("SELECT status FROM jobs WHERE id=?", (j1,)).fetchone()["status"] == "in_progress"
    run = conn.execute(
        "SELECT job_id, status, owner FROM application_runs WHERE id=?",
        (claim.run_id,),
    ).fetchone()
    assert tuple(run) == (j1, "running", "atomic-owner")
    binding = conn.execute(
        "SELECT resume_id, run_id, bound_at FROM application_resume_bindings WHERE run_id=?",
        (claim.run_id,),
    ).fetchone()
    assert tuple(binding[:2]) == (r1, claim.run_id)
    assert binding["bound_at"]


@pytest.mark.parametrize(
    ("column", "value"),
    (
        ("description", "Changed requirements:\n- Python 3.12"),
        ("canonical_url", "https://example.com/jobs/atomic-stale-updated"),
    ),
)
def test_atomic_generated_resume_claim_rejects_stale_job_snapshot(
    tmp_path: Path,
    column: str,
    value: str,
) -> None:
    conn, _ = _setup_db(tmp_path)
    job_id = _insert_test_job(conn, source_job_id="atomic-stale")
    snapshot = read_resume_job(conn, job_id=job_id)
    assert isinstance(snapshot, JobResumeSnapshot)
    resume = create_generated_resume(
        conn,
        job_id=job_id,
        resume_id="atomic-stale-resume",
        job_snapshot_sha256=snapshot.job_snapshot_sha256,
        profile_sha256="b" * 64,
        source_resume_sha256="c" * 64,
        generation_config_sha256="d" * 64,
        state="ready",
        artifact_dir="/artifacts/atomic-stale-resume",
        content_sha256="e" * 64,
        pdf_sha256="f" * 64,
        private_pdf_path="/private/atomic-stale-resume.pdf",
    )
    conn.execute(f"UPDATE jobs SET {column}=? WHERE id=?", (value, job_id))
    conn.commit()

    assert claim_application_job_with_generated_resume(
        conn,
        owner="atomic-owner",
        job_id=job_id,
        resume_id=_id(resume),
        expected_job_snapshot_sha256=snapshot.job_snapshot_sha256,
    ) is None
    assert conn.execute("SELECT status FROM jobs WHERE id=?", (job_id,)).fetchone()["status"] == "queued"
    assert conn.execute("SELECT COUNT(*) FROM application_runs").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM application_resume_bindings").fetchone()[0] == 0


def test_blank_description_override_generates_and_claims(tmp_path: Path) -> None:
    conn, _ = _setup_db(tmp_path)
    job_id = _insert_test_job(
        conn,
        source_job_id="atomic-override",
        description="",
    )
    override = "Override requirements:\n- Python 3.12\n- SQLite"

    with pytest.raises(ValueError, match="Job description cannot be blank"):
        read_resume_job(conn, job_id=job_id)
    snapshot = read_resume_job(
        conn,
        job_id=job_id,
        description_override=override,
    )
    assert isinstance(snapshot, JobResumeSnapshot)
    assert snapshot.description == "Override requirements:\n- Python 3.12\n- SQLite"
    assert snapshot.requirements == ("Override requirements:", "Python 3.12", "SQLite")
    assert get_job_resume_snapshot(
        conn,
        job_id,
        description_override=override,
    ) == snapshot

    with pytest.raises(ValueError, match="description_override must be non-blank"):
        read_resume_job(conn, job_id=job_id, description_override=" \n ")

    resume = create_generated_resume(
        conn,
        job_id=job_id,
        resume_id="atomic-override-resume",
        job_snapshot_sha256=snapshot.job_snapshot_sha256,
        profile_sha256="b" * 64,
        source_resume_sha256="c" * 64,
        generation_config_sha256="d" * 64,
        state="ready",
        artifact_dir="/artifacts/atomic-override-resume",
        content_sha256="e" * 64,
        pdf_sha256="f" * 64,
        private_pdf_path="/private/atomic-override-resume.pdf",
    )
    with pytest.raises(TypeError, match="description_override"):
        claim_application_job_with_generated_resume(
            conn,
            owner="atomic-owner",
            job_id=job_id,
            resume_id=_id(resume),
            expected_job_snapshot_sha256=snapshot.job_snapshot_sha256,
            description_override=123,  # type: ignore[arg-type]
        )
    assert conn.execute("SELECT status FROM jobs WHERE id=?", (job_id,)).fetchone()["status"] == "queued"
    assert conn.execute("SELECT COUNT(*) FROM application_runs").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM application_resume_bindings").fetchone()[0] == 0

    claim = claim_application_job_with_generated_resume(
        conn,
        owner="atomic-owner",
        job_id=job_id,
        resume_id=_id(resume),
        expected_job_snapshot_sha256=snapshot.job_snapshot_sha256,
        description_override=override,
    )
    assert claim is not None
    assert conn.execute("SELECT status FROM jobs WHERE id=?", (job_id,)).fetchone()["status"] == "in_progress"


def test_blank_description_override_rejects_other_row_mutation(tmp_path: Path) -> None:
    conn, _ = _setup_db(tmp_path)
    job_id = _insert_test_job(
        conn,
        source_job_id="atomic-override-stale",
        description="",
    )
    override = "Override requirements:\n- Python 3.12"
    snapshot = read_resume_job(
        conn,
        job_id=job_id,
        description_override=override,
    )
    assert isinstance(snapshot, JobResumeSnapshot)
    resume = create_generated_resume(
        conn,
        job_id=job_id,
        resume_id="atomic-override-stale-resume",
        job_snapshot_sha256=snapshot.job_snapshot_sha256,
        profile_sha256="b" * 64,
        source_resume_sha256="c" * 64,
        generation_config_sha256="d" * 64,
        state="ready",
        artifact_dir="/artifacts/atomic-override-stale-resume",
        content_sha256="e" * 64,
        pdf_sha256="f" * 64,
        private_pdf_path="/private/atomic-override-stale-resume.pdf",
    )
    conn.execute(
        "UPDATE jobs SET canonical_url=? WHERE id=?",
        ("https://example.com/jobs/atomic-override-stale-updated", job_id),
    )
    conn.commit()

    assert claim_application_job_with_generated_resume(
        conn,
        owner="atomic-owner",
        job_id=job_id,
        resume_id=_id(resume),
        expected_job_snapshot_sha256=snapshot.job_snapshot_sha256,
        description_override=override,
    ) is None
    assert conn.execute("SELECT status FROM jobs WHERE id=?", (job_id,)).fetchone()["status"] == "queued"
    assert conn.execute("SELECT COUNT(*) FROM application_runs").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM application_resume_bindings").fetchone()[0] == 0


def test_atomic_generated_resume_claim_rolls_back_on_binding_failure(tmp_path: Path) -> None:
    conn, _ = _setup_db(tmp_path)
    job_id = _insert_test_job(conn, source_job_id="atomic-failure")
    snapshot = read_resume_job(conn, job_id=job_id)
    assert isinstance(snapshot, JobResumeSnapshot)
    resume = create_generated_resume(
        conn,
        job_id=job_id,
        resume_id="atomic-failure-resume",
        job_snapshot_sha256=snapshot.job_snapshot_sha256,
        profile_sha256="b" * 64,
        source_resume_sha256="c" * 64,
        generation_config_sha256="d" * 64,
        state="ready",
        artifact_dir="/artifacts/atomic-failure-resume",
        content_sha256="e" * 64,
        pdf_sha256="f" * 64,
        private_pdf_path="/private/atomic-failure-resume.pdf",
    )
    resume_id = _id(resume)
    conn.execute(
        """
        CREATE TRIGGER injected_binding_failure
        BEFORE INSERT ON application_resume_bindings
        BEGIN
            SELECT RAISE(ABORT, 'injected binding failure');
        END
        """
    )
    conn.commit()

    with pytest.raises(sqlite3.IntegrityError, match="injected binding failure"):
        claim_application_job_with_generated_resume(
            conn,
            owner="atomic-owner",
            job_id=job_id,
            resume_id=resume_id,
            expected_job_snapshot_sha256=snapshot.job_snapshot_sha256,
        )
    assert conn.execute("SELECT status FROM jobs WHERE id=?", (job_id,)).fetchone()["status"] == "queued"
    assert conn.execute("SELECT COUNT(*) FROM application_runs").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM application_resume_bindings").fetchone()[0] == 0


def test_generated_resumes_schema_safety(tmp_path: Path) -> None:
    conn, root = _setup_db(tmp_path)

    # Re-running initialize_database is idempotent
    initialize_database(conn, migration_artifact_root=root)

    # Altering schema causes fingerprint mismatch error
    conn.execute("ALTER TABLE generated_resumes ADD COLUMN dummy_col TEXT")
    conn.commit()

    with pytest.raises(RuntimeError, match="generated_resumes adjunct schema fingerprint mismatch"):
        initialize_database(conn, migration_artifact_root=root)


def test_all_states_and_legal_transitions(tmp_path: Path) -> None:
    conn, _ = _setup_db(tmp_path)
    job_id = _insert_test_job(conn, source_job_id="resume-job-states")

    assert GENERATED_RESUMES_STATES == ("pending", "generating", "validating", "rendering", "ready", "failed", "superseded")

    h_job = "a" * 64
    h_prof = "b" * 64
    h_src = "c" * 64
    h_cfg = "d" * 64
    h_cnt = "e" * 64
    h_pdf = "f" * 64

    # Create record in pending state
    rec = create_generated_resume(
        conn,
        job_id=job_id,
        job_snapshot_sha256=h_job,
        profile_sha256=h_prof,
        source_resume_sha256=h_src,
        generation_config_sha256=h_cfg,
        state="pending",
    )
    resume_id = rec["resume_id"]
    assert rec["state"] == "pending"

    # Step-by-step legal non-terminal transitions
    step1 = transition_generated_resume_state(conn, resume_id=resume_id, from_state="pending", to_state="generating")
    assert step1["state"] == "generating"

    step2 = transition_generated_resume_state(conn, resume_id=resume_id, from_state="generating", to_state="validating")
    assert step2["state"] == "validating"

    step3 = transition_generated_resume_state(conn, resume_id=resume_id, from_state="validating", to_state="rendering")
    assert step3["state"] == "rendering"

    # Transition from rendering to ready
    ready = transition_generated_resume_state(
        conn,
        resume_id=resume_id,
        from_state="rendering",
        to_state="ready",
        artifact_dir="/artifacts/run-1",
        content_sha256=h_cnt,
        pdf_sha256=h_pdf,
        private_pdf_path="/private/pdf/1.pdf",
    )
    assert ready["state"] == "ready"
    assert ready["content_sha256"] == h_cnt

    # Transition from ready to superseded
    sup = transition_generated_resume_state(
        conn,
        resume_id=resume_id,
        from_state="ready",
        to_state="superseded",
    )
    assert sup["state"] == "superseded"

    # Invalid state transition from superseded to ready raises ValueError
    with pytest.raises(ValueError):
        transition_generated_resume_state(conn, resume_id=resume_id, from_state="superseded", to_state="ready")

    # Failed state transition test: retains artifact_dir and reason_code but clears/omits PDF hashes
    rec_fail = create_generated_resume(
        conn,
        job_id=job_id,
        job_snapshot_sha256=h_job,
        profile_sha256=h_prof,
        source_resume_sha256=h_src,
        generation_config_sha256=h_cfg,
        state="generating",
        artifact_dir="/artifacts/run-failed",
    )
    fail_id = rec_fail["resume_id"]

    failed = transition_generated_resume_state(
        conn,
        resume_id=fail_id,
        to_state="failed",
        reason_code="render_timeout",
    )
    assert failed["state"] == "failed"
    assert failed["reason_code"] == "render_timeout"
    assert failed["completed_at"] is not None

    # Verify in DB that failed attempt has artifact_dir but NO PDF hashes
    raw_failed = get_generated_resume_private(conn, fail_id)
    assert raw_failed["artifact_dir"] == "/artifacts/run-failed"
    assert raw_failed["content_sha256"] is None
    assert raw_failed["pdf_sha256"] is None
    assert raw_failed["private_pdf_path"] is None

    # Failed terminal status cannot be transitioned
    with pytest.raises(ValueError):
        transition_generated_resume_state(conn, resume_id=fail_id, to_state="ready")


def test_exact_immutable_input_replay(tmp_path: Path) -> None:
    conn, _ = _setup_db(tmp_path)
    job_id = _insert_test_job(conn, source_job_id="replay-job")

    h_job = "1" * 64
    h_prof = "2" * 64
    h_src = "3" * 64
    h_cfg = "4" * 64
    h_cnt = "5" * 64
    h_pdf = "6" * 64

    # Initially no ready resume
    assert get_ready_generated_resume(conn, job_id=job_id) is None

    # Create ready resume
    created = create_generated_resume(
        conn,
        job_id=job_id,
        job_snapshot_sha256=h_job,
        profile_sha256=h_prof,
        source_resume_sha256=h_src,
        generation_config_sha256=h_cfg,
        state="ready",
        artifact_dir="/artifacts/r1",
        content_sha256=h_cnt,
        pdf_sha256=h_pdf,
        private_pdf_path="/private/pdf/replay.pdf",
    )

    # Re-creating ready resume with exact immutable inputs returns existing ready resume
    replayed = create_generated_resume(
        conn,
        job_id=job_id,
        job_snapshot_sha256=h_job,
        profile_sha256=h_prof,
        source_resume_sha256=h_src,
        generation_config_sha256=h_cfg,
        state="ready",
        artifact_dir="/artifacts/r1",
        content_sha256=h_cnt,
        pdf_sha256=h_pdf,
        private_pdf_path="/private/pdf/replay.pdf",
        force=False,
    )
    assert _id(replayed) == _id(created)

    # Ready lookup with exact immutable inputs returns existing ready resume
    ready = get_ready_generated_resume(
        conn,
        job_id=job_id,
        job_snapshot_sha256=h_job,
        profile_sha256=h_prof,
        source_resume_sha256=h_src,
        generation_config_sha256=h_cfg,
    )
    assert ready is not None
    assert _id(ready) == _id(created)


def test_force_creates_distinct_row_and_supersedes_only_matching_inputs(tmp_path: Path) -> None:
    conn, _ = _setup_db(tmp_path)
    job_id = _insert_test_job(conn, source_job_id="force-job")

    h_job = "0" * 64
    h_prof = "1" * 64
    h_src = "2" * 64
    h_cfg = "3" * 64
    h_cnt = "4" * 64
    h_pdf = "5" * 64

    # Create initial ready resume
    r1 = create_generated_resume(
        conn,
        job_id=job_id,
        job_snapshot_sha256=h_job,
        profile_sha256=h_prof,
        source_resume_sha256=h_src,
        generation_config_sha256=h_cfg,
        state="ready",
        artifact_dir="/artifacts/r1",
        content_sha256=h_cnt,
        pdf_sha256=h_pdf,
        private_pdf_path="/private/pdf/r1.pdf",
    )
    r1_id = _id(r1)

    # Force create a new resume for the SAME matching input configuration
    r2 = create_generated_resume(
        conn,
        job_id=job_id,
        job_snapshot_sha256=h_job,
        profile_sha256=h_prof,
        source_resume_sha256=h_src,
        generation_config_sha256=h_cfg,
        state="ready",
        artifact_dir="/artifacts/r2",
        content_sha256=h_cnt,
        pdf_sha256=h_pdf,
        private_pdf_path="/private/pdf/r2.pdf",
        force=True,
    )
    r2_id = _id(r2)

    assert r1_id != r2_id

    # Old resume r1 is now superseded
    old_row = get_generated_resume_private(conn, r1_id)
    assert old_row["state"] == "superseded"
    assert old_row["private_pdf_path"] == "/private/pdf/r1.pdf"

    # New resume r2 is the current ready resume for matching input
    ready = get_ready_generated_resume(conn, job_id=job_id, job_snapshot_sha256=h_job, profile_sha256=h_prof, source_resume_sha256=h_src, generation_config_sha256=h_cfg)
    assert _id(ready) == r2_id


def test_force_pending_failure_preserves_ready_until_replacement_ready(tmp_path: Path) -> None:
    conn, _ = _setup_db(tmp_path)
    job_id = _insert_test_job(conn, source_job_id="force-lifecycle-job")

    inputs = {
        "job_snapshot_sha256": "a" * 64,
        "profile_sha256": "b" * 64,
        "source_resume_sha256": "c" * 64,
        "generation_config_sha256": "d" * 64,
    }
    prior = create_generated_resume(
        conn,
        job_id=job_id,
        state="ready",
        artifact_dir="/artifacts/prior",
        content_sha256="e" * 64,
        pdf_sha256="f" * 64,
        private_pdf_path="/private/pdf/prior.pdf",
        **inputs,
    )
    prior_id = _id(prior)

    failed_attempt = create_generated_resume(
        conn,
        job_id=job_id,
        state="pending",
        force=True,
        **inputs,
    )
    failed_id = _id(failed_attempt)
    assert failed_id != prior_id
    assert get_generated_resume_private(conn, prior_id)["state"] == "ready"
    assert _id(get_ready_generated_resume(conn, job_id=job_id, **inputs)) == prior_id

    failed = transition_generated_resume_state(
        conn,
        resume_id=failed_id,
        from_state="pending",
        to_state="failed",
        reason_code="render_failed",
        artifact_dir="/artifacts/failed",
    )
    assert failed["state"] == "failed"
    assert get_generated_resume_private(conn, prior_id)["state"] == "ready"
    assert _id(get_ready_generated_resume(conn, job_id=job_id, **inputs)) == prior_id

    replacement = create_generated_resume(
        conn,
        job_id=job_id,
        state="pending",
        force=True,
        **inputs,
    )
    replacement_id = _id(replacement)
    assert replacement_id not in {prior_id, failed_id}
    assert get_generated_resume_private(conn, prior_id)["state"] == "ready"

    ready = transition_generated_resume_state(
        conn,
        resume_id=replacement_id,
        from_state="pending",
        to_state="ready",
        artifact_dir="/artifacts/replacement",
        content_sha256="1" * 64,
        pdf_sha256="2" * 64,
        private_pdf_path="/private/pdf/replacement.pdf",
    )
    assert ready["state"] == "ready"
    assert get_generated_resume_private(conn, prior_id)["state"] == "superseded"
    assert _id(get_ready_generated_resume(conn, job_id=job_id, **inputs)) == replacement_id


def test_multiple_configs_coexist_ready(tmp_path: Path) -> None:
    conn, _ = _setup_db(tmp_path)
    job_id = _insert_test_job(conn, source_job_id="coexist-job")

    h_job = "0" * 64
    h_src = "2" * 64
    h_cnt = "4" * 64
    h_pdf = "5" * 64

    # Config 1
    c1_prof = "1" * 64
    c1_cfg = "a" * 64

    # Config 2 (different generation_config_sha256)
    c2_prof = "1" * 64
    c2_cfg = "b" * 64

    # Create ready resume for Config 1
    r1 = create_generated_resume(
        conn,
        job_id=job_id,
        job_snapshot_sha256=h_job,
        profile_sha256=c1_prof,
        source_resume_sha256=h_src,
        generation_config_sha256=c1_cfg,
        state="ready",
        artifact_dir="/artifacts/c1",
        content_sha256=h_cnt,
        pdf_sha256=h_pdf,
        private_pdf_path="/private/pdf/c1.pdf",
    )
    r1_id = _id(r1)

    # Create ready resume for Config 2
    r2 = create_generated_resume(
        conn,
        job_id=job_id,
        job_snapshot_sha256=h_job,
        profile_sha256=c2_prof,
        source_resume_sha256=h_src,
        generation_config_sha256=c2_cfg,
        state="ready",
        artifact_dir="/artifacts/c2",
        content_sha256=h_cnt,
        pdf_sha256=h_pdf,
        private_pdf_path="/private/pdf/c2.pdf",
    )
    r2_id = _id(r2)

    assert r1_id != r2_id

    # BOTH Config 1 and Config 2 remain ready (multiple configs coexist ready)
    r1_state = get_generated_resume_private(conn, r1_id)["state"]
    r2_state = get_generated_resume_private(conn, r2_id)["state"]
    assert r1_state == "ready"
    assert r2_state == "ready"

    # Forcing a new ready resume for Config 1 supersedes ONLY Config 1
    r1_new = create_generated_resume(
        conn,
        job_id=job_id,
        job_snapshot_sha256=h_job,
        profile_sha256=c1_prof,
        source_resume_sha256=h_src,
        generation_config_sha256=c1_cfg,
        state="ready",
        artifact_dir="/artifacts/c1_v2",
        content_sha256=h_cnt,
        pdf_sha256=h_pdf,
        private_pdf_path="/private/pdf/c1_v2.pdf",
        force=True,
    )
    assert get_generated_resume_private(conn, r1_id)["state"] == "superseded"
    assert get_generated_resume_private(conn, r2_id)["state"] == "ready"  # Config 2 unaffected!
    assert get_generated_resume_private(conn, _id(r1_new))["state"] == "ready"


def test_safe_public_default_and_explicit_private_resolver(tmp_path: Path) -> None:
    conn, _ = _setup_db(tmp_path)
    job_id = _insert_test_job(conn, source_job_id="public-job")

    rec = create_generated_resume(
        conn,
        job_id=job_id,
        job_snapshot_sha256="a" * 64,
        profile_sha256="b" * 64,
        source_resume_sha256="c" * 64,
        generation_config_sha256="d" * 64,
        state="ready",
        artifact_dir="/artifacts/dir-secret",
        content_sha256="e" * 64,
        pdf_sha256="f" * 64,
        private_pdf_path="/private/pdf/secret.pdf",
        score_json={"score": 98, "matches": ["Python"]},
    )
    resume_id = _id(rec)

    # Safe public default for get_generated_resume redacts private_pdf_path, artifact_dir, score_json
    public_show = get_generated_resume(conn, resume_id)
    assert "private_pdf_path" not in public_show
    assert "artifact_dir" not in public_show
    assert "score_json" not in public_show

    # format_public_generated_resume redacts private fields
    raw_show = get_generated_resume(conn, resume_id, public_shaping=False)
    formatted = format_public_generated_resume(raw_show)
    assert "private_pdf_path" not in formatted
    assert "artifact_dir" not in formatted
    assert "score_json" not in formatted

    # List with safe public default redacts private fields
    listed = list_generated_resumes(conn, job_id=job_id)
    assert len(listed) == 1
    assert "private_pdf_path" not in listed[0]
    assert "artifact_dir" not in listed[0]
    assert "score_json" not in listed[0]

    # Explicit private resolver returns all private fields for internal service
    private_show = get_generated_resume_private(conn, resume_id)
    assert private_show is not None
    assert private_show["private_pdf_path"] == "/private/pdf/secret.pdf"
    assert private_show["artifact_dir"] == "/artifacts/dir-secret"
    assert "score_json" in private_show


def test_bind_generated_resume_to_application(tmp_path: Path) -> None:
    conn, _ = _setup_db(tmp_path)
    j1 = _insert_test_job(conn, source_job_id="bind-job-1")
    j2 = _insert_test_job(conn, source_job_id="bind-job-2")

    # Ready resume for j1
    r1 = create_generated_resume(
        conn,
        job_id=j1,
        job_snapshot_sha256="1" * 64,
        profile_sha256="2" * 64,
        source_resume_sha256="3" * 64,
        generation_config_sha256="4" * 64,
        state="ready",
        artifact_dir="/artifacts/r1",
        content_sha256="5" * 64,
        pdf_sha256="6" * 64,
        private_pdf_path="/private/pdf/r1.pdf",
    )
    r1_id = _id(r1)

    # Generating resume for j1 (non-ready)
    r_gen = create_generated_resume(
        conn,
        job_id=j1,
        job_snapshot_sha256="1" * 64,
        profile_sha256="2" * 64,
        source_resume_sha256="3" * 64,
        generation_config_sha256="7" * 64,
        state="generating",
    )
    r_gen_id = _id(r_gen)

    # Ready resume for j2
    r2 = create_generated_resume(
        conn,
        job_id=j2,
        job_snapshot_sha256="a" * 64,
        profile_sha256="b" * 64,
        source_resume_sha256="c" * 64,
        generation_config_sha256="d" * 64,
        state="ready",
        artifact_dir="/artifacts/r2",
        content_sha256="e" * 64,
        pdf_sha256="f" * 64,
        private_pdf_path="/private/pdf/r2.pdf",
    )
    r2_id = _id(r2)

    # Claim application run for j1
    claim1 = claim_application_job(conn, owner="runner-1", job_id=j1)
    assert claim1 is not None
    run_id_1 = claim1.run_id

    # Valid binding: ready resume r1 and run_id_1 both for job j1
    binding = bind_generated_resume_to_application(conn, resume_id=r1_id, run_id=run_id_1)
    assert binding["resume_id"] == r1_id
    assert binding["run_id"] == run_id_1
    assert "bound_at" in binding

    # Rejection: Non-ready resume state
    with pytest.raises(ValueError, match="must be 'ready'"):
        bind_generated_resume_to_application(conn, resume_id=r_gen_id, run_id=run_id_1)

    # Rejection: Mismatched job_id (r2 is for j2, run_id_1 is for j1)
    with pytest.raises(ValueError, match="Binding mismatch"):
        bind_generated_resume_to_application(conn, resume_id=r2_id, run_id=run_id_1)

    # Rejection: Missing resume or run
    with pytest.raises(KeyError):
        bind_generated_resume_to_application(conn, resume_id="non-existent-resume", run_id=run_id_1)
    with pytest.raises(KeyError):
        bind_generated_resume_to_application(conn, resume_id=r1_id, run_id=99999)


def test_generated_resume_artifact_dataclass_conversion(tmp_path: Path) -> None:
    conn, _ = _setup_db(tmp_path)
    job_id = _insert_test_job(conn, source_job_id="dataclass-job")

    rec = create_generated_resume(
        conn,
        job_id=job_id,
        job_snapshot_sha256="1" * 64,
        profile_sha256="2" * 64,
        source_resume_sha256="3" * 64,
        generation_config_sha256="4" * 64,
        state="ready",
        artifact_dir="/artifacts/r1",
        content_sha256="5" * 64,
        pdf_sha256="6" * 64,
        private_pdf_path="/private/pdf/r1.pdf",
        raw_object=True,
    )
    assert isinstance(rec, GeneratedResumeArtifact)
    assert rec.job_id == job_id
    assert isinstance(rec.job_id, int)
    assert isinstance(rec.private_pdf_path, Path)
    assert str(rec.private_pdf_path) == "/private/pdf/r1.pdf"

    lookup = get_ready_generated_resume(conn, job_id=job_id, raw_object=True)
    assert isinstance(lookup, GeneratedResumeArtifact)
    assert lookup.job_id == job_id
    assert isinstance(lookup.job_id, int)
    assert isinstance(lookup.private_pdf_path, Path)
