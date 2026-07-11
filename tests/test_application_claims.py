from __future__ import annotations

import asyncio
import concurrent.futures
import json
import os
import sqlite3
import stat
import threading
from datetime import datetime, timezone
from pathlib import Path

import pytest

import jobs_assistant.application as application_module
import jobs_assistant.db as db_module

from jobs_assistant.artifacts import ArtifactRoot
from jobs_assistant.db import (
    PUBLIC_REASON_CODES,
    application_schema_fingerprint,
    claim_next_application_job,
    complete_review,
    connect,
    finish_application_run,
    initialize_database,
    list_application_reviews,
    mark_application_spawn_attempted,
    reconcile_open_session_failure,
    register_application_artifact,
    register_application_browser_process,
    register_application_owner_process,
    register_application_session,
    retry_review,
    review_window_state,
)



def _initialize(conn: sqlite3.Connection, tmp_path: Path) -> None:
    with ArtifactRoot.open(tmp_path / "artifacts", cwd=tmp_path) as artifact_root:
        initialize_database(conn, migration_artifact_root=artifact_root)


def _job(conn: sqlite3.Connection, *, url: str = "https://boards.greenhouse.io/acme/jobs/123?gh_src=abc&utm_source=x") -> int:
    conn.execute(
        """
        INSERT INTO jobs (
            source, source_job_id, canonical_url, title, company, discovered_at,
            raw_json, first_seen_at, last_seen_at
        ) VALUES ('fixture', ?, ?, 'Engineer', 'Acme', '2026-07-10T00:00:00+00:00', '{}',
                  '2026-07-10T00:00:00+00:00', '2026-07-10T00:00:00+00:00')
        """,
        (f"job-{url}", url),
    )
    conn.commit()
    return int(conn.execute("SELECT last_insert_rowid()").fetchone()[0])

def test_workflow_route_rejection_persists_private_failure_artifact(tmp_path: Path) -> None:
    conn = connect(tmp_path / "jobs.sqlite3")
    _initialize(conn, tmp_path)
    _job(conn, url="https://boards.greenhouse.io/acme/jobs")
    resume = tmp_path / "resume.txt"
    resume.write_text("Ada Example", encoding="utf-8")
    artifact_root = tmp_path / "artifacts"

    result = asyncio.run(
        application_module.run_application_workflow(
            conn,
            resume_file=resume,
            artifact_root=artifact_root,
            ats="greenhouse",
        )
    )

    assert result[0]["reason_code"] == "ats_mismatch"
    run = conn.execute(
        "SELECT status, reason_code, artifact_dir FROM application_runs"
    ).fetchone()
    assert run["status"] == "blocked"
    assert run["reason_code"] == "ats_mismatch"
    assert run["artifact_dir"] == "run-1"
    run_dir = artifact_root / run["artifact_dir"]
    assert (run_dir / "run.json").is_file()
    assert json.loads((run_dir / "run.json").read_text(encoding="utf-8"))["stage"] == "failed"

def test_pre_artifact_failure_does_not_persist_missing_artifact_ref(tmp_path: Path) -> None:
    """A run-dir collision must not bind a DB ref to a nonexistent directory."""
    conn = connect(tmp_path / "jobs.sqlite3")
    _initialize(conn, tmp_path)
    _job(conn, url="https://boards.greenhouse.io/acme/jobs/123")
    resume = tmp_path / "resume.txt"
    resume.write_text("Ada Example", encoding="utf-8")
    artifact_root = tmp_path / "artifacts"
    collision = artifact_root / "run-1"
    collision.write_text("not a run directory", encoding="utf-8")

    result = asyncio.run(
        application_module.run_application_workflow(
            conn,
            resume_file=resume,
            artifact_root=artifact_root,
            ats="greenhouse",
        )
    )

    assert result == [{
        "job_id": 1,
        "run_id": 1,
        "status": "failed",
        "reason_code": "browser_error",
        "ats": "greenhouse",
        "artifact_ref": None,
        "window_state": "closed",
    }]
    run = conn.execute(
        "SELECT status, reason_code, artifact_dir FROM application_runs"
    ).fetchone()
    assert run["status"] == "failed"
    assert run["reason_code"] == "browser_error"
    assert run["artifact_dir"] is None
    assert collision.is_file()


def test_failed_durable_finalization_is_not_reported_terminal(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    conn = connect(tmp_path / "jobs.sqlite3")
    _initialize(conn, tmp_path)
    _job(conn, url="https://boards.greenhouse.io/acme/jobs/123")
    resume = tmp_path / "resume.txt"
    resume.write_text("Ada Example", encoding="utf-8")

    class NeverStarts:
        @classmethod
        def start(cls, **kwargs):
            raise RuntimeError("browser startup")

    monkeypatch.setattr(application_module, "PuppeteerSession", NeverStarts)

    def fail_finish(*args, **kwargs):
        raise RuntimeError("database write failed")

    monkeypatch.setattr(application_module, "finish_application_run", fail_finish)

    with pytest.raises(RuntimeError, match="database_error"):
        asyncio.run(
            application_module.run_application_workflow(
                conn,
                resume_file=resume,
                artifact_root=tmp_path / "artifacts",
                ats="greenhouse",
            )
        )

    run = conn.execute(
        "SELECT status, artifact_dir FROM application_runs"
    ).fetchone()
    assert run["status"] == "running"
    assert run["artifact_dir"] == "run-1"
    assert (tmp_path / "artifacts" / "run-1").is_dir()


def _write_review_manifest(run: object, payload: dict[str, object]) -> None:
    """Write the current versioned review manifest and its run-token binding."""
    manifest = dict(payload)
    token = "a" * 64
    manifest.setdefault("version", 1)
    manifest["commit_token_sha256"] = token
    owner_pid = manifest.get("owner_pid")
    if owner_pid is not None:
        manifest["owner_identity"] = {
            "pid": owner_pid,
            "pgid": manifest.get("owner_pgid", owner_pid),
            "birth": manifest.get("owner_birth", "birth-owner"),
        }
    browser_pid = manifest.get("browser_pid")
    if browser_pid is not None:
        manifest["browser_identity"] = {
            "pid": browser_pid,
            "pgid": manifest.get("browser_pgid", browser_pid),
            "birth": manifest.get("browser_birth", "birth-browser"),
        }
    run_id = int(manifest["run_id"])
    job_id = int(manifest["job_id"])
    run.write_json(
        "run.json",
        {"run_id": run_id, "job_id": job_id, "commit_token_sha256": token},
    )
    run.write_json("review_session.json", manifest)

def _stale_spawn_fixture(
    conn: sqlite3.Connection,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    reason: str = "browser_error",
    state: str = "prepared",
    owner_birth: str = "birth-owner",
    browser_birth: str = "birth-browser",
) -> tuple[object, ArtifactRoot]:
    """Create a post-failure spawned session with identity-bound process evidence."""
    _job(conn, url=f"https://boards.greenhouse.io/acme/jobs/stale-{state}-{reason}")
    claim = claim_next_application_job(conn, owner="owner")
    assert claim is not None
    root = ArtifactRoot.open(tmp_path / "artifacts", cwd=tmp_path)
    monkeypatch.setattr(
        db_module,
        "_capture_process_identity",
        lambda pid: {
            "pid": pid,
            "pgid": pid,
            "birth": {12345: "birth-owner", 23456: "birth-browser"}.get(pid, "birth"),
        },
    )
    assert register_application_artifact(conn, run_id=claim.run_id, artifact_dir=f"run-{claim.run_id}")
    assert register_application_session(conn, run_id=claim.run_id, session_id="stale-session", session_state="starting")
    if state == "prepared":
        assert register_application_session(conn, run_id=claim.run_id, session_id="stale-session", session_state="prepared")
    assert mark_application_spawn_attempted(conn, run_id=claim.run_id, session_id="stale-session")
    assert register_application_owner_process(
        conn,
        run_id=claim.run_id,
        owner_pid=12345,
        process_identity={"pid": 12345, "pgid": 12345, "birth": "birth-owner"},
    )
    assert register_application_browser_process(
        conn,
        run_id=claim.run_id,
        browser_pid=23456,
        process_identity={"pid": 23456, "pgid": 23456, "birth": "birth-browser"},
    )
    with root.create_run_dir(claim.run_id) as run:
        run.write_json("browser-profile/state", {"private": "ephemeral"})
        run.write_json("input/staged.bin", {"private": "ephemeral"})
        _write_review_manifest(
            run,
            {
                "run_id": claim.run_id,
                "job_id": claim.job["id"],
                "session_id": "stale-session",
                "owner_pid": 12345,
                "browser_pid": 23456,
                "owner_pgid": 12345,
                "browser_pgid": 23456,
                "owner_birth": owner_birth,
                "browser_birth": browser_birth,
                "state": state,
                "spawn_attempted": True,
            },
        )
    finish_application_run(
        conn,
        run_id=claim.run_id,
        status="failed",
        reason_code=reason,
        artifact_dir=f"run-{claim.run_id}",
    )
    return claim, root

def _terminal_fixture(
    conn: sqlite3.Connection,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    reason: str,
    state: str = "failed",
) -> tuple[object, ArtifactRoot]:
    job_id = _job(conn)
    claim = claim_next_application_job(conn, owner="owner")
    assert claim is not None
    root = ArtifactRoot.open(tmp_path / "artifacts", cwd=tmp_path)
    monkeypatch.setattr(
        db_module,
        "_capture_process_identity",
        lambda pid: {
            "pid": pid,
            "pgid": pid,
            "birth": {12345: "birth-owner", 23456: "birth-browser"}.get(pid, "birth"),
        },
    )
    assert register_application_artifact(conn, run_id=claim.run_id, artifact_dir=f"run-{claim.run_id}")
    assert register_application_session(
        conn,
        run_id=claim.run_id,
        session_id="terminal-session",
        session_state="starting" if state == "failed" else "open",
    )
    assert mark_application_spawn_attempted(conn, run_id=claim.run_id, session_id="terminal-session")
    assert register_application_owner_process(
        conn,
        run_id=claim.run_id,
        owner_pid=12345,
        process_identity={"pid": 12345, "pgid": 12345, "birth": "birth-owner"},
    )
    assert register_application_browser_process(
        conn,
        run_id=claim.run_id,
        browser_pid=23456,
        process_identity={"pid": 23456, "pgid": 23456, "birth": "birth-browser"},
    )
    with root.create_run_dir(claim.run_id) as run:
        _write_review_manifest(
            run,
            {
                "run_id": claim.run_id,
                "job_id": job_id,
                "session_id": "terminal-session",
                "owner_pid": 12345,
                "browser_pid": 23456,
                "owner_pgid": 12345,
                "browser_pgid": 23456,
                "owner_birth": "birth-owner",
                "browser_birth": "birth-browser",
                "state": state,
                "cleanup": "complete",
                "terminal_reason": reason,
            },
        )
    finish_application_run(
        conn,
        run_id=claim.run_id,
        status="manual",
        reason_code="required_safe_fields_unresolved",
        artifact_dir=f"run-{claim.run_id}",
    )
    monkeypatch.setattr(db_module, "_process_group_state", lambda pid, expected=None: "absent")
    return claim, root

def test_sql_canonicalizer_repeated_representative_ddl_finishes() -> None:
    ddl = """
        CREATE TABLE IF NOT EXISTS "Quoted_Name" (
            "id" INTEGER PRIMARY KEY AUTOINCREMENT,
            "status" TEXT NOT NULL DEFAULT 'queued',
            CHECK ("status" <> 'bad' AND "id" >= 0 OR "status" = 'queued')
        );
    """
    cosmetic = """
        -- leading comment
        CREATE /* inline comment */ TABLE IF NOT EXISTS [quoted_name](
            [id] INTEGER PRIMARY KEY AUTOINCREMENT,
            [status] TEXT NOT NULL DEFAULT 'queued',
            CHECK([status] <> 'bad' AND [id] >= 0 OR [status] = 'queued')
        ); -- trailing comment
    """
    failures: list[BaseException] = []

    def run() -> None:
        try:
            for _ in range(256):
                assert db_module._canonicalize_sql(ddl) == db_module._canonicalize_sql(cosmetic)
        except BaseException as exc:  # report worker failures without losing timeout
            failures.append(exc)

    worker = threading.Thread(target=run, daemon=True)
    worker.start()
    worker.join(timeout=2.0)
    assert not worker.is_alive(), "SQL canonicalizer did not finish promptly"
    assert not failures, failures[0] if failures else None


def test_nested_redacted_summary_never_exposes_sentinel_host() -> None:
    summary = json.loads(
        db_module._redacted_summary(
            {"outer": {"url": "https://nested-sentinel.example.invalid/app"}}
        )
    )
    assert summary["count"] == 3
    assert summary["host_classes"] == ["unsupported_public"]
    assert "nested-sentinel.example.invalid" not in json.dumps(summary)


def _legacy_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE application_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            job_id INTEGER NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
            apply_url TEXT NOT NULL,
            status TEXT NOT NULL CHECK (status IN ('completed', 'manual', 'blocked', 'failed')),
            reason TEXT NOT NULL,
            started_at TEXT NOT NULL,
            finished_at TEXT NOT NULL,
            observation_json TEXT NOT NULL DEFAULT '{}',
            plan_json TEXT NOT NULL DEFAULT '{}'
        );
        CREATE INDEX idx_application_runs_job_id ON application_runs(job_id);
        CREATE INDEX idx_application_runs_status ON application_runs(status);
        """
    )

def _jobs_schema(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE jobs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source TEXT NOT NULL,
            source_job_id TEXT,
            canonical_url TEXT,
            title TEXT NOT NULL,
            company TEXT NOT NULL,
            location TEXT,
            remote INTEGER,
            posted_at TEXT,
            discovered_at TEXT NOT NULL,
            description TEXT,
            status TEXT NOT NULL DEFAULT 'queued',
            raw_json TEXT NOT NULL DEFAULT '{}',
            first_seen_at TEXT NOT NULL,
            last_seen_at TEXT NOT NULL
        )
        """
    )
    conn.commit()
def test_initialize_database_creates_and_noops_exact_application_schema(tmp_path: Path) -> None:
    conn = connect(tmp_path / "jobs.sqlite3")
    _initialize(conn, tmp_path)
    before = application_schema_fingerprint(conn)
    assert before["columns"][-1] == "reviewed_at"
    _initialize(conn, tmp_path)
    assert application_schema_fingerprint(conn) == before


def test_application_init_creates_exact_schema_and_reason_code_check(tmp_path: Path) -> None:
    conn = connect(tmp_path / "jobs.sqlite3")
    _initialize(conn, tmp_path)

    fp = application_schema_fingerprint(conn)
    assert fp["columns"] == [
        "id",
        "job_id",
        "apply_url",
        "status",
        "reason_code",
        "owner",
        "started_at",
        "finished_at",
        "observation_json",
        "plan_json",
        "artifact_dir",
        "session_id",
        "owner_pid",
        "browser_pid",
        "outcome",
        "reviewed_at",
    ]
    assert "idx_application_runs_running_job" in fp["indexes"]

    job_id = _job(conn)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO application_runs (job_id, apply_url, status, owner, started_at) VALUES (?, 'x', 'manual', 'o', 't')",
            (job_id,),
        )
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            """
            INSERT INTO application_runs (job_id, apply_url, status, owner, started_at, finished_at, reason_code)
            VALUES (?, 'x', 'failed', 'o', 't', 't', 'not_public')
            """,
            (job_id,),
        )
    assert "browser_error" in PUBLIC_REASON_CODES



def test_application_init_rejects_unknown_partial_schema_without_mutation(tmp_path: Path) -> None:
    conn = connect(tmp_path / "jobs.sqlite3")
    conn.execute("CREATE TABLE application_runs (id INTEGER PRIMARY KEY, arbitrary TEXT)")
    conn.execute("CREATE TABLE sentinel (value TEXT)")
    conn.execute("INSERT INTO sentinel VALUES ('untouched')")
    conn.commit()

    with pytest.raises(RuntimeError, match="unknown application_runs schema"):
        _initialize(conn, tmp_path)

    assert conn.execute("SELECT value FROM sentinel").fetchone()[0] == "untouched"
    assert conn.execute("PRAGMA table_info(application_runs)").fetchall()[1]["name"] == "arbitrary"
    assert not (tmp_path / "artifacts" / ".database-identity").exists()


def test_exact_legacy_migration_redacts_db_and_preserves_private_artifacts(tmp_path: Path) -> None:
    conn = connect(tmp_path / "jobs.sqlite3")
    _jobs_schema(conn)
    job_id = _job(conn, url="https://boards.greenhouse.io/acme/jobs/123?token=secret&utm_source=x")
    _legacy_schema(conn)
    conn.execute(
        """
        INSERT INTO application_runs (
            id, job_id, apply_url, status, reason, started_at, finished_at, observation_json, plan_json
        ) VALUES (7, ?, ?, 'completed', ?, '2026-07-10T00:00:00+00:00',
                  '2026-07-10T00:01:00+00:00', ?, ?)
        """,
        (
            job_id,
            "https://boards.greenhouse.io/acme/jobs/123?token=secret&utm_source=x",
            "raw error token=secret",
            json.dumps({"field": "secret@example.com", "url": "https://example.test/private?token=secret"}),
            json.dumps({"answer": "secret"}),
        ),
    )
    conn.commit()

    _initialize(conn, tmp_path)
    row = conn.execute("SELECT * FROM application_runs WHERE id=7").fetchone()
    assert row["status"] == "review_ready"
    assert row["reason_code"] == "legacy_run"
    assert row["owner"] == "legacy:migrated"
    public_blob = json.dumps(dict(row), default=str)
    assert "secret" not in public_blob
    assert row["apply_url"].startswith("gh_hash:")
    artifact_dir = tmp_path / "artifacts" / "legacy-run-7"
    assert artifact_dir.is_dir()
    assert (artifact_dir / "legacy" / "reason.txt").read_text() == "raw error token=secret"
    assert stat.S_IMODE((artifact_dir / "legacy" / "reason.txt").stat().st_mode) == 0o600
    with ArtifactRoot.open(tmp_path / "artifacts", cwd=tmp_path) as root:
        reviews = list_application_reviews(conn, limit=10, artifact_root=root)
        migrated = next(item for item in reviews if item["run_id"] == 7)
        assert migrated["artifact_ref"] == "legacy-run-7"
        with root.open_artifact_ref(migrated["artifact_ref"], run_id=7) as legacy_run:
            assert legacy_run.read_bytes("legacy/reason.txt") == b"raw error token=secret"
    assert conn.execute("SELECT seq FROM sqlite_sequence WHERE name='application_runs'").fetchone()["seq"] >= 7
    assert conn.execute("SELECT status FROM jobs WHERE id=?", (job_id,)).fetchone()["status"] == "in_progress"


def test_claims_are_atomic_disjoint_and_full_row_frozen(tmp_path: Path) -> None:
    db = tmp_path / "jobs.sqlite3"
    conn1 = connect(db)
    _initialize(conn1, tmp_path)
    first = _job(conn1, url="https://boards.greenhouse.io/acme/jobs/1")
    second = _job(conn1, url="https://boards.greenhouse.io/acme/jobs/2")
    conn2 = connect(db)

    claim1 = claim_next_application_job(conn1, owner="owner-a")
    claim2 = claim_next_application_job(conn2, owner="owner-b")

    assert claim1 is not None and claim2 is not None

    assert {claim1.job["id"], claim2.job["id"]} == {first, second}
    assert claim_next_application_job(conn1, owner="owner-c") is None
    assert conn1.execute("SELECT COUNT(*) FROM application_runs WHERE status='running'").fetchone()[0] == 2
    assert claim1.job["title"] == "Engineer"
    with pytest.raises(TypeError):
        claim1.job["title"] = "Changed"  # type: ignore[index]


def test_finish_redacts_and_cas_accepts_only_public_codes(tmp_path: Path) -> None:
    conn = connect(tmp_path / "jobs.sqlite3")
    _initialize(conn, tmp_path)
    _job(conn)
    claim = claim_next_application_job(conn, owner="owner")
    assert claim is not None

    with pytest.raises(ValueError, match="exact public"):
        finish_application_run(conn, run_id=claim.run_id, status="failed", reason_code="raw token=secret")

    finish_application_run(
        conn,
        run_id=claim.run_id,
        status="failed",
        reason_code="browser_error",
        observation_summary={"url": "https://example.test/apply?token=secret", "field": "secret@example.com"},
        plan_summary={"selector": "#secret", "error": "token=secret"},
        artifact_dir=f"run-{claim.run_id}",
    )
    row = conn.execute("SELECT * FROM application_runs WHERE id=?", (claim.run_id,)).fetchone()
    assert row["finished_at"] is not None
    assert "secret" not in row["observation_json"]
    assert "secret" not in row["plan_json"]
    with pytest.raises(RuntimeError, match="running"):
        finish_application_run(conn, run_id=claim.run_id, status="failed", reason_code="database_error")


def test_latest_review_cas_complete_and_retry(tmp_path: Path) -> None:
    conn = connect(tmp_path / "jobs.sqlite3")
    _initialize(conn, tmp_path)
    job_id = _job(conn)
    claim = claim_next_application_job(conn, owner="owner")
    assert claim is not None
    finish_application_run(conn, run_id=claim.run_id, status="manual", reason_code="required_safe_fields_unresolved")

    conn.execute(
        """
        INSERT INTO application_runs (job_id, apply_url, status, reason_code, owner, started_at, finished_at)
        VALUES (?, 'gh_hash:new host=boards.greenhouse.io', 'manual', 'required_safe_fields_unresolved', 'owner',
                '2026-07-10T00:02:00+00:00', '2026-07-10T00:03:00+00:00')
        """,
        (job_id,),
    )
    latest_run = int(conn.execute("SELECT max(id) FROM application_runs WHERE job_id=?", (job_id,)).fetchone()[0])
    conn.commit()
    with pytest.raises(RuntimeError, match="latest"):
        complete_review(conn, run_id=claim.run_id, outcome="skipped", artifact_root=ArtifactRoot.open(tmp_path / "artifacts", cwd=tmp_path))

    result = retry_review(conn, run_id=latest_run, artifact_root=ArtifactRoot.open(tmp_path / "artifacts", cwd=tmp_path))
    assert result["outcome"] == "retry"
    assert conn.execute("SELECT status FROM jobs WHERE id=?", (job_id,)).fetchone()["status"] == "queued"
    with pytest.raises(RuntimeError, match="reviewed"):
        complete_review(conn, run_id=latest_run, outcome="submitted", artifact_root=ArtifactRoot.open(tmp_path / "artifacts", cwd=tmp_path))


def test_submitted_rejects_failed_runs_even_after_open_guarded_browser_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    conn = connect(tmp_path / "jobs.sqlite3")
    _initialize(conn, tmp_path)
    _job(conn)
    failed = claim_next_application_job(conn, owner="owner")
    assert failed is not None
    finish_application_run(conn, run_id=failed.run_id, status="failed", reason_code="handoff_failed")
    with pytest.raises(RuntimeError, match="failed"):
        complete_review(conn, run_id=failed.run_id, outcome="submitted", artifact_root=ArtifactRoot.open(tmp_path / "artifacts", cwd=tmp_path))

    conn.execute("UPDATE jobs SET status='queued' WHERE id=?", (failed.job["id"],))
    conn.commit()
    opened = claim_next_application_job(conn, owner="owner")
    assert opened is not None
    root = ArtifactRoot.open(tmp_path / "artifacts", cwd=tmp_path)
    monkeypatch.setattr(
        db_module,
        "_capture_process_identity",
        lambda pid: {"pid": pid, "pgid": pid, "birth": {12345: "birth-owner", 23456: "birth-browser"}.get(pid, "birth")},
    )
    assert register_application_artifact(conn, run_id=opened.run_id, artifact_dir=f"run-{opened.run_id}") is True
    assert register_application_session(conn, run_id=opened.run_id, session_id="session-open", session_state="open") is True
    assert mark_application_spawn_attempted(conn, run_id=opened.run_id, session_id="session-open")
    assert register_application_owner_process(
        conn, run_id=opened.run_id, owner_pid=12345,
        process_identity={"pid": 12345, "pgid": 12345, "birth": "birth-owner"},
    )
    assert register_application_browser_process(
        conn, run_id=opened.run_id, browser_pid=23456,
        process_identity={"pid": 23456, "pgid": 23456, "birth": "birth-browser"},
    )
    with root.create_run_dir(opened.run_id) as run:
        _write_review_manifest(
            run,
            {
                "run_id": opened.run_id,
                "job_id": opened.job["id"],
                "session_id": "session-open",
                "owner_pid": 12345,
                "browser_pid": 23456,
                "owner_pgid": 12345,
                "browser_pgid": 23456,
                "owner_birth": "birth-owner",
                "browser_birth": "birth-browser",
                "state": "closed",
                "cleanup": "complete",
            },
        )
    finish_application_run(
        conn,
        run_id=opened.run_id,
        status="failed",
        reason_code="browser_error",
        artifact_dir=f"run-{opened.run_id}",
    )
    monkeypatch.setattr(db_module, "_process_group_state", lambda pid, expected=None: "absent")
    with pytest.raises(RuntimeError, match="failed"):
        complete_review(
            conn,
            run_id=opened.run_id,
            outcome="submitted",
            artifact_root=ArtifactRoot.open(tmp_path / "artifacts", cwd=tmp_path),
        )

def test_reconcile_only_latest_unreviewed_matching_session(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    conn = connect(tmp_path / "jobs.sqlite3")
    _initialize(conn, tmp_path)
    _job(conn)
    old = claim_next_application_job(conn, owner="owner")
    assert old is not None
    finish_application_run(conn, run_id=old.run_id, status="manual", reason_code="required_safe_fields_unresolved")
    conn.execute("UPDATE jobs SET status='queued' WHERE id=?", (old.job["id"],))
    conn.commit()
    latest = claim_next_application_job(conn, owner="owner")
    assert latest is not None
    root = ArtifactRoot.open(tmp_path / "artifacts", cwd=tmp_path)
    monkeypatch.setattr(
        db_module,
        "_capture_process_identity",
        lambda pid: {"pid": pid, "pgid": pid, "birth": {12345: "birth-owner", 23456: "birth-browser"}.get(pid, "birth")},
    )
    assert register_application_artifact(conn, run_id=latest.run_id, artifact_dir=f"run-{latest.run_id}")
    assert register_application_session(conn, run_id=latest.run_id, session_id="sess-new", session_state="open")
    assert mark_application_spawn_attempted(conn, run_id=latest.run_id, session_id="sess-new")
    assert register_application_owner_process(conn, run_id=latest.run_id, owner_pid=12345, process_identity={"pid": 12345, "pgid": 12345, "birth": "birth-owner"})
    assert register_application_browser_process(conn, run_id=latest.run_id, browser_pid=23456, process_identity={"pid": 23456, "pgid": 23456, "birth": "birth-browser"})
    with root.create_run_dir(latest.run_id) as run:
        _write_review_manifest(
            run,
            {
                "run_id": latest.run_id,
                "job_id": latest.job["id"],
                "session_id": "sess-new",
                "owner_pid": 12345,
                "browser_pid": 23456,
                "owner_pgid": 12345,
                "browser_pgid": 23456,
                "owner_birth": "birth-owner",
                "browser_birth": "birth-browser",
                "state": "open_guarded",
                "cleanup": "complete",
                "terminal_reason": "browser_error",
            },
        )
    monkeypatch.setattr(db_module, "_process_group_state", lambda pid, expected=None: "absent")
    assert reconcile_open_session_failure(conn, run_id=old.run_id, session_id=None, artifact_root=root) is False
    assert reconcile_open_session_failure(conn, run_id=latest.run_id, session_id="other", artifact_root=root) is False
    assert reconcile_open_session_failure(conn, run_id=latest.run_id, session_id="sess-new", artifact_root=root) is True
    row = conn.execute("SELECT status, reason_code FROM application_runs WHERE id=?", (latest.run_id,)).fetchone()
    assert (row["status"], row["reason_code"]) == ("failed", "browser_error")


@pytest.mark.parametrize("mode", [0o644, 0o755])
def test_secure_connect_rejects_non_private_existing_db_modes(tmp_path: Path, mode: int) -> None:
    parent = tmp_path / "private"
    parent.mkdir(mode=0o700)
    db = parent / "jobs.sqlite3"
    db.write_bytes(b"")
    os.chmod(db, mode)
    with pytest.raises(PermissionError, match="owner-private"):
        connect(db)


def test_connect_closes_opened_connection_when_security_revalidation_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = tmp_path / "jobs.sqlite3"
    real_prepare = db_module._secure_prepare_sqlite_path
    prepare_calls: list[Path] = []
    opened: list[sqlite3.Connection] = []
    real_connect = db_module.sqlite3.connect

    def flaky_prepare(path: Path) -> None:
        prepare_calls.append(path)
        if len(prepare_calls) == 2:
            raise PermissionError("database path changed")
        real_prepare(path)

    def tracking_connect(*args, **kwargs):
        connection = real_connect(*args, **kwargs)
        opened.append(connection)
        return connection

    monkeypatch.setattr(db_module, "_secure_prepare_sqlite_path", flaky_prepare)
    monkeypatch.setattr(db_module.sqlite3, "connect", tracking_connect)

    with pytest.raises(PermissionError, match="database path changed"):
        connect(db)

    assert len(opened) == 1
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        opened[0].execute("SELECT 1")

@pytest.mark.parametrize("suffix", ["-wal", "-shm", "-journal"])
def test_secure_connect_rejects_non_private_existing_sidecar_modes(tmp_path: Path, suffix: str) -> None:
    parent = tmp_path / "private"
    parent.mkdir(mode=0o700)
    db = parent / "jobs.sqlite3"
    sidecar = parent / f"jobs.sqlite3{suffix}"
    db.write_bytes(b"")
    sidecar.write_bytes(b"")
    os.chmod(db, 0o600)
    os.chmod(sidecar, 0o644)
    with pytest.raises(PermissionError, match="owner-private"):
        connect(db)


def test_secure_connect_accepts_private_db_sidecar_and_parent_modes(tmp_path: Path) -> None:
    parent = tmp_path / "private"
    parent.mkdir(mode=0o700)
    db = parent / "jobs.sqlite3"
    sidecar = parent / "jobs.sqlite3-wal"
    db.write_bytes(b"")
    sidecar.write_bytes(b"")
    os.chmod(db, 0o600)
    os.chmod(sidecar, 0o600)
    conn = connect(db)
    conn.close()
    assert stat.S_IMODE(parent.stat().st_mode) == 0o700
    assert stat.S_IMODE(db.stat().st_mode) == 0o600
    assert stat.S_IMODE(sidecar.stat().st_mode) == 0o600


def test_secure_connect_rejects_non_private_user_parent_modes(tmp_path: Path) -> None:
    parent = tmp_path / "public"
    parent.mkdir(mode=0o755)
    os.chmod(parent, 0o755)
    db = parent / "jobs.sqlite3"
    db.write_bytes(b"")
    os.chmod(db, 0o600)
    with pytest.raises(PermissionError, match="parent directory"):
        connect(db)


def test_process_group_survivors_and_pid_reuse_refuse_review_transition(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    conn = connect(tmp_path / "jobs.sqlite3")
    _initialize(conn, tmp_path)
    _job(conn)
    claim = claim_next_application_job(conn, owner="owner")
    assert claim is not None
    finish_application_run(conn, run_id=claim.run_id, status="manual", reason_code="required_safe_fields_unresolved")
    conn.execute(
        "UPDATE application_runs SET owner_pid=12345, browser_pid=23456, observation_json=? WHERE id=?",
        (json.dumps({"_process": {
            "owner": {"pid": 12345, "pgid": 12345, "birth": "birth-owner"},
            "browser": {"pid": 23456, "pgid": 23456, "birth": "birth-browser"},
        }}), claim.run_id),
    )
    conn.commit()

    monkeypatch.setattr("jobs_assistant.db._process_group_state", lambda pid, expected=None: "live" if pid == 12345 else "absent")
    with pytest.raises(RuntimeError, match="window_live"):
        complete_review(conn, run_id=claim.run_id, outcome="skipped", artifact_root=ArtifactRoot.open(tmp_path / "artifacts", cwd=tmp_path))

    monkeypatch.setattr("jobs_assistant.db._process_group_state", lambda pid, expected=None: "unknown")
    with pytest.raises(RuntimeError, match="window_state_unknown"):
        retry_review(conn, run_id=claim.run_id, artifact_root=ArtifactRoot.open(tmp_path / "artifacts", cwd=tmp_path))

def test_live_open_guarded_manifest_token_mismatch_is_unknown_and_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    conn = connect(tmp_path / "jobs.sqlite3")
    _initialize(conn, tmp_path)
    job_id = _job(conn)
    claim = claim_next_application_job(conn, owner="owner")
    assert claim is not None
    root = ArtifactRoot.open(tmp_path / "artifacts", cwd=tmp_path)
    monkeypatch.setattr(
        db_module,
        "_capture_process_identity",
        lambda pid: {
            "pid": pid,
            "pgid": pid,
            "birth": {12345: "birth-owner", 23456: "birth-browser"}[pid],
        },
    )
    assert register_application_artifact(conn, run_id=claim.run_id, artifact_dir=f"run-{claim.run_id}")
    assert register_application_session(
        conn, run_id=claim.run_id, session_id="live-session", session_state="starting"
    )
    assert mark_application_spawn_attempted(conn, run_id=claim.run_id, session_id="live-session")
    assert register_application_owner_process(
        conn,
        run_id=claim.run_id,
        owner_pid=12345,
        process_identity={"pid": 12345, "pgid": 12345, "birth": "birth-owner"},
    )
    assert register_application_browser_process(
        conn,
        run_id=claim.run_id,
        browser_pid=23456,
        process_identity={"pid": 23456, "pgid": 23456, "birth": "birth-browser"},
    )
    assert register_application_session(
        conn, run_id=claim.run_id, session_id="live-session", session_state="prepared"
    )
    assert register_application_session(
        conn, run_id=claim.run_id, session_id="live-session", session_state="open_guarded"
    )
    with root.create_run_dir(claim.run_id) as run:
        _write_review_manifest(
            run,
            {
                "run_id": claim.run_id,
                "job_id": job_id,
                "session_id": "live-session",
                "owner_pid": 12345,
                "browser_pid": 23456,
                "owner_pgid": 12345,
                "browser_pgid": 23456,
                "owner_birth": "birth-owner",
                "browser_birth": "birth-browser",
                "state": "open_guarded",
                "cleanup": "complete",
                "heartbeat": datetime.now(timezone.utc).isoformat(),
            },
        )
        run.write_json(
            "review_session.json",
            {
                **json.loads(run.read_bytes("review_session.json").decode("utf-8")),
                "commit_token_sha256": "b" * 64,
            },
        )
    finish_application_run(
        conn,
        run_id=claim.run_id,
        status="manual",
        reason_code="required_safe_fields_unresolved",
        artifact_dir=f"run-{claim.run_id}",
    )
    monkeypatch.setattr(db_module, "_process_group_state", lambda pid, expected=None: "live")

    assert review_window_state(conn, run_id=claim.run_id, artifact_root=root) == "unknown"
    rows = list_application_reviews(conn, limit=10, artifact_root=root)
    assert rows[0]["window_state"] == "unknown"
    with pytest.raises(RuntimeError, match="window_live"):
        complete_review(
            conn,
            run_id=claim.run_id,
            outcome="skipped",
            artifact_root=root,
        )


def test_fingerprint_rejects_trigger_without_unrelated_mutation(tmp_path: Path) -> None:
    conn = connect(tmp_path / "jobs.sqlite3")
    _initialize(conn, tmp_path)
    conn.execute("CREATE TRIGGER application_runs_probe AFTER INSERT ON application_runs BEGIN SELECT 1; END")
    conn.execute("CREATE TABLE sentinel (value TEXT)")
    conn.execute("INSERT INTO sentinel VALUES ('untouched')")
    conn.commit()
    with pytest.raises(RuntimeError, match="unknown application_runs schema"):
        _initialize(conn, tmp_path)
    assert conn.execute("SELECT value FROM sentinel").fetchone()[0] == "untouched"
    assert conn.execute("SELECT name FROM sqlite_schema WHERE type='trigger' AND name='application_runs_probe'").fetchone() is not None


def test_concurrent_initializers_serialize_and_converge(tmp_path: Path) -> None:
    db = tmp_path / "jobs.sqlite3"

    def initialize_in_thread() -> dict[str, object]:
        conn = connect(db)
        try:
            _initialize(conn, tmp_path)
            return application_schema_fingerprint(conn)
        finally:
            conn.close()

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
        fingerprints = list(executor.map(lambda _: initialize_in_thread(), range(2)))
    assert fingerprints[0] == fingerprints[1]


def test_registration_helpers_are_idempotent_cas_and_persist_identity(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    conn = connect(tmp_path / "jobs.sqlite3")
    _initialize(conn, tmp_path)
    job_id = _job(conn)
    claim = claim_next_application_job(conn, owner="owner")
    assert claim is not None
    assert register_application_artifact(conn, run_id=claim.run_id, artifact_dir=f"run-{claim.run_id}") is True
    assert register_application_artifact(conn, run_id=claim.run_id, artifact_dir=f"run-{claim.run_id}") is True
    with pytest.raises(ValueError, match="must match"):
        register_application_artifact(conn, run_id=claim.run_id, artifact_dir="run-other")
    assert register_application_session(conn, run_id=claim.run_id, session_id="session") is True
    assert register_application_session(conn, run_id=claim.run_id, session_id="session") is True
    assert register_application_session(conn, run_id=claim.run_id, session_id="other") is False
    assert mark_application_spawn_attempted(conn, run_id=claim.run_id, session_id="session")
    root = ArtifactRoot.open(tmp_path / "artifacts", cwd=tmp_path)
    capture_calls: list[int] = []
    def capture(pid: int) -> dict[str, object]:
        capture_calls.append(pid)
        return {"pid": pid, "pgid": pid, "birth": {12345: "birth-a", 23456: "birth-b"}.get(pid, "birth")}
    monkeypatch.setattr(db_module, "_capture_process_identity", capture)
    assert register_application_owner_process(conn, run_id=claim.run_id, owner_pid=12345, process_identity={"pid": 12345, "pgid": 12345, "birth": "birth-a"}, artifact_root=root) is True
    assert register_application_owner_process(conn, run_id=claim.run_id, owner_pid=12345) is True
    assert capture_calls.count(12345) == 2
    assert register_application_owner_process(conn, run_id=claim.run_id, owner_pid=12346) is False
    assert register_application_browser_process(conn, run_id=claim.run_id, browser_pid=23456, process_identity={"pid": 23456, "pgid": 23456, "birth": "birth-b"}, artifact_root=root) is True
    assert register_application_browser_process(conn, run_id=claim.run_id, browser_pid=23457) is False
    row = conn.execute("SELECT job_id, artifact_dir, session_id, owner_pid, browser_pid, observation_json FROM application_runs WHERE id=?", (claim.run_id,)).fetchone()
    assert row["job_id"] == job_id
    assert (row["artifact_dir"], row["session_id"], row["owner_pid"], row["browser_pid"]) == (f"run-{claim.run_id}", "session", 12345, 23456)
    assert json.loads(row["observation_json"])["_process"]["owner"]["birth"] == "birth-a"
    finish_application_run(conn, run_id=claim.run_id, status="failed", reason_code="browser_error", observation_summary={"selector": "#secret"})
    finished = conn.execute("SELECT observation_json FROM application_runs WHERE id=?", (claim.run_id,)).fetchone()
    assert json.loads(finished["observation_json"])["_process"]["browser"]["pgid"] == 23456
def test_process_probe_detects_surviving_group_and_pid_reuse(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(db_module, "_capture_process_identity", lambda pid: None)
    monkeypatch.setattr(db_module, "_group_members", lambda pgid: {999})
    assert db_module._process_group_state(123, expected={"pgid": 44, "birth": "birth-a"}) == "live"
    monkeypatch.setattr(db_module, "_capture_process_identity", lambda pid: {"pid": pid, "pgid": 44, "birth": "birth-new"})
    assert db_module._process_group_state(123, expected={"pgid": 44, "birth": "birth-a"}) == "unknown"


def test_legacy_binary_payloads_are_preserved_and_high_sequence_survives(tmp_path: Path) -> None:
    conn = connect(tmp_path / "jobs.sqlite3")
    _jobs_schema(conn)
    job_id = _job(conn)
    _legacy_schema(conn)
    raw_url = b"https://boards.greenhouse.io/acme/jobs/7?token=\xff"
    raw_reason = b"reason=\xff secret"
    conn.execute(
        "INSERT INTO application_runs (id, job_id, apply_url, status, reason, started_at, finished_at, observation_json, plan_json) VALUES (7, ?, ?, 'manual', ?, 't', 't', ?, ?)",
        (job_id, sqlite3.Binary(raw_url), sqlite3.Binary(raw_reason), sqlite3.Binary(b"{\xff}"), sqlite3.Binary(b"{secret}")),
    )
    conn.execute("INSERT OR REPLACE INTO sqlite_sequence(name, seq) VALUES ('application_runs', 900)")
    conn.commit()
    _initialize(conn, tmp_path)
    row = conn.execute("SELECT apply_url, reason_code, artifact_dir FROM application_runs WHERE id=7").fetchone()
    assert row["reason_code"] == "legacy_run"
    assert row["apply_url"].startswith("gh_hash:")
    assert conn.execute("SELECT seq FROM sqlite_sequence WHERE name='application_runs'").fetchone()["seq"] >= 900
    legacy_dir = tmp_path / "artifacts" / "legacy-run-7"
    assert (legacy_dir / "legacy" / "reason.txt").read_bytes() == raw_reason
    assert not (legacy_dir / "legacy" / "apply_url.txt").exists()
def test_review_state_conflict_rolls_back_run_and_job(tmp_path: Path) -> None:
    conn = connect(tmp_path / "jobs.sqlite3")
    _initialize(conn, tmp_path)
    _job(conn)
    claim = claim_next_application_job(conn, owner="owner")
    assert claim is not None
    finish_application_run(conn, run_id=claim.run_id, status="manual", reason_code="required_safe_fields_unresolved")
    conn.execute("UPDATE jobs SET status='queued' WHERE id=?", (claim.job["id"],))
    conn.commit()
    before_run = tuple(conn.execute("SELECT status, outcome, reviewed_at FROM application_runs WHERE id=?", (claim.run_id,)).fetchone())
    with pytest.raises(RuntimeError, match="state_conflict"):
        complete_review(conn, run_id=claim.run_id, outcome="skipped", artifact_root=ArtifactRoot.open(tmp_path / "artifacts", cwd=tmp_path))
    assert tuple(conn.execute("SELECT status, outcome, reviewed_at FROM application_runs WHERE id=?", (claim.run_id,)).fetchone()) == before_run
def test_reconcile_requires_terminal_manifest_and_closed_is_noop(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    conn = connect(tmp_path / "jobs.sqlite3")
    _initialize(conn, tmp_path)
    job_id = _job(conn, url="https://boards.greenhouse.io/acme/jobs/450")
    claim = claim_next_application_job(conn, owner="owner")
    assert claim is not None
    root = ArtifactRoot.open(tmp_path / "artifacts", cwd=tmp_path)
    monkeypatch.setattr(
        db_module,
        "_capture_process_identity",
        lambda pid: {
            "pid": pid, "pgid": pid,
            "birth": {12345: "birth-owner", 23456: "birth-browser", 12346: "birth-owner-2", 23457: "birth-browser-2"}.get(pid, "birth"),
        },
    )
    assert register_application_artifact(conn, run_id=claim.run_id, artifact_dir=f"run-{claim.run_id}")
    assert register_application_session(conn, run_id=claim.run_id, session_id="session-start", session_state="starting")
    assert mark_application_spawn_attempted(conn, run_id=claim.run_id, session_id="session-start")
    assert register_application_owner_process(conn, run_id=claim.run_id, owner_pid=12345, process_identity={"pid": 12345, "pgid": 12345, "birth": "birth-owner"})
    assert register_application_browser_process(conn, run_id=claim.run_id, browser_pid=23456, process_identity={"pid": 23456, "pgid": 23456, "birth": "birth-browser"})
    with root.create_run_dir(claim.run_id) as run:
        _write_review_manifest(
            run,
            {
                "run_id": claim.run_id,
                "job_id": job_id,
                "session_id": "session-start",
                "owner_pid": 12345,
                "browser_pid": 23456,
                "owner_pgid": 12345,
                "browser_pgid": 23456,
                "owner_birth": "birth-owner",
                "browser_birth": "birth-browser",
                "state": "starting",
                "cleanup": "complete",
                "terminal_reason": "handoff_failed",
            },
        )
    monkeypatch.setattr(db_module, "_process_group_state", lambda pid, expected=None: "absent")
    assert reconcile_open_session_failure(conn, run_id=claim.run_id, session_id="session-start", artifact_root=root) is True
    row = conn.execute("SELECT status, reason_code FROM application_runs WHERE id=?", (claim.run_id,)).fetchone()
    assert (row["status"], row["reason_code"]) == ("failed", "handoff_failed")

    job_id = _job(conn, url="https://boards.greenhouse.io/acme/jobs/451")
    closed = claim_next_application_job(conn, owner="owner")
    assert closed is not None
    assert register_application_artifact(conn, run_id=closed.run_id, artifact_dir=f"run-{closed.run_id}")
    assert register_application_session(conn, run_id=closed.run_id, session_id="session-closed", session_state="open")
    assert mark_application_spawn_attempted(conn, run_id=closed.run_id, session_id="session-closed")
    assert register_application_owner_process(conn, run_id=closed.run_id, owner_pid=12346, process_identity={"pid": 12346, "pgid": 12346, "birth": "birth-owner-2"})
    assert register_application_browser_process(conn, run_id=closed.run_id, browser_pid=23457, process_identity={"pid": 23457, "pgid": 23457, "birth": "birth-browser-2"})
    with root.create_run_dir(closed.run_id) as run:
        _write_review_manifest(
            run,
            {
                "run_id": closed.run_id,
                "job_id": job_id,
                "session_id": "session-closed",
                "owner_pid": 12346,
                "browser_pid": 23457,
                "owner_pgid": 12346,
                "browser_pgid": 23457,
                "owner_birth": "birth-owner-2",
                "browser_birth": "birth-browser-2",
                "state": "closed",
                "cleanup": "complete",
            },
        )
    before = tuple(conn.execute("SELECT status, reason_code FROM application_runs WHERE id=?", (closed.run_id,)).fetchone())
    assert reconcile_open_session_failure(conn, run_id=closed.run_id, session_id="session-closed", artifact_root=root) is False
    assert tuple(conn.execute("SELECT status, reason_code FROM application_runs WHERE id=?", (closed.run_id,)).fetchone()) == before


def test_artifact_root_binds_one_database_identity_without_ddl_on_mismatch(tmp_path: Path) -> None:
    root = ArtifactRoot.open(tmp_path / "artifacts", cwd=tmp_path)
    first = connect(tmp_path / "first.sqlite3")
    _initialize(first, tmp_path)
    second = connect(tmp_path / "second.sqlite3")
    with pytest.raises(RuntimeError, match="another database"):
        initialize_database(second, migration_artifact_root=root)
    assert second.execute("SELECT 1 FROM sqlite_schema WHERE name='application_runs'").fetchone() is None
    same = ArtifactRoot.open(tmp_path / "artifacts", cwd=tmp_path)
    initialize_database(first, migration_artifact_root=same)


def test_redacted_db_summaries_never_store_sentinel_hostname(tmp_path: Path) -> None:
    db = tmp_path / "jobs.sqlite3"
    conn = connect(db)
    _initialize(conn, tmp_path)
    _job(conn, url="https://boards.greenhouse.io/acme/jobs/303")
    claim = claim_next_application_job(conn, owner="owner")
    assert claim is not None
    finish_application_run(
        conn,
        run_id=claim.run_id,
        status="failed",
        reason_code="browser_error",
        observation_summary={
            "url": "https://sentinel.attacker.example/apply",
            "nested": ["https://sentinel.attacker.example/private"],
        },
    )
    blob = b"".join(bytes(row[0] or b"", "utf-8") for row in conn.execute("SELECT apply_url, observation_json, plan_json FROM application_runs"))
    conn.close()
    blob += db.read_bytes()
    assert b"sentinel.attacker.example" not in blob


def test_secure_connect_rejects_traversal_and_world_writable_file(tmp_path: Path) -> None:
    with pytest.raises(PermissionError, match="parent traversal"):
        connect(tmp_path / ".." / "escape.sqlite3")
    unsafe = tmp_path / "unsafe.sqlite3"
    unsafe.write_text("")
    os.chmod(unsafe, 0o666)
    with pytest.raises(PermissionError, match="owner-private"):
        connect(unsafe)


def test_barrier_claims_are_disjoint_across_connections(tmp_path: Path) -> None:
    db = tmp_path / "jobs.sqlite3"
    setup = connect(db)
    _initialize(setup, tmp_path)
    first = _job(setup, url="https://boards.greenhouse.io/acme/jobs/301")
    second = _job(setup, url="https://boards.greenhouse.io/acme/jobs/302")
    setup.close()
    barrier = threading.Barrier(2)

    def claim_once(owner: str) -> int:
        conn = connect(db)
        barrier.wait()
        claim = claim_next_application_job(conn, owner=owner)
        conn.close()
        assert claim is not None
        return claim.job["id"]

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        claimed = list(pool.map(claim_once, ("a", "b")))
    assert set(claimed) == {first, second}


def test_concurrent_complete_retry_has_one_cas_winner(tmp_path: Path) -> None:
    db = tmp_path / "jobs.sqlite3"
    conn = connect(db)
    _initialize(conn, tmp_path)
    _job(conn)
    claim = claim_next_application_job(conn, owner="owner")
    assert claim is not None
    finish_application_run(conn, run_id=claim.run_id, status="manual", reason_code="required_safe_fields_unresolved")
    conn.close()
    barrier = threading.Barrier(2)

    def review(kind: str) -> str:
        local = connect(db)
        barrier.wait()
        try:
            if kind == "complete":
                complete_review(local, run_id=claim.run_id, outcome="skipped", artifact_root=ArtifactRoot.open(tmp_path / "artifacts", cwd=tmp_path))
            else:
                retry_review(local, run_id=claim.run_id, artifact_root=ArtifactRoot.open(tmp_path / "artifacts", cwd=tmp_path))
            return "won"
        except RuntimeError:
            return "lost"
        finally:
            local.close()

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(review, ("complete", "retry")))
    assert sorted(outcomes) == ["lost", "won"]


def test_manifest_confirmed_stale_cleanup_requires_confirmation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    conn = connect(tmp_path / "jobs.sqlite3")
    _initialize(conn, tmp_path)
    _job(conn)
    claim = claim_next_application_job(conn, owner="owner")
    assert claim is not None
    root = ArtifactRoot.open(tmp_path / "artifacts", cwd=tmp_path)
    monkeypatch.setattr(
        db_module,
        "_capture_process_identity",
        lambda pid: {"pid": pid, "pgid": pid, "birth": {12345: "birth-owner", 23456: "birth-browser"}.get(pid, "birth")},
    )
    assert register_application_artifact(conn, run_id=claim.run_id, artifact_dir=f"run-{claim.run_id}")
    assert register_application_session(conn, run_id=claim.run_id, session_id="stale", session_state="open")
    assert mark_application_spawn_attempted(conn, run_id=claim.run_id, session_id="stale")
    assert register_application_owner_process(
        conn, run_id=claim.run_id, owner_pid=12345,
        process_identity={"pid": 12345, "pgid": 12345, "birth": "birth-owner"},
    )
    assert register_application_browser_process(
        conn, run_id=claim.run_id, browser_pid=23456,
        process_identity={"pid": 23456, "pgid": 23456, "birth": "birth-browser"},
    )
    with root.create_run_dir(claim.run_id) as run:
        run.write_json("browser-profile/state", {"secret": "private"})
        _write_review_manifest(
            run,
            {
                "run_id": claim.run_id,
                "job_id": claim.job["id"],
                "session_id": "stale",
                "owner_pid": 12345,
                "browser_pid": 23456,
                "owner_pgid": 12345,
                "browser_pgid": 23456,
                "owner_birth": "birth-owner",
                "browser_birth": "birth-browser",
                "state": "open",
            },
        )
    finish_application_run(conn, run_id=claim.run_id, status="manual", reason_code="required_safe_fields_unresolved", artifact_dir=f"run-{claim.run_id}")
    monkeypatch.setattr(db_module, "_process_group_state", lambda pid, expected=None: "absent")
    with pytest.raises(RuntimeError, match="window_state_unknown"):
        complete_review(conn, run_id=claim.run_id, outcome="skipped", artifact_root=root)
    complete_review(conn, run_id=claim.run_id, outcome="skipped", artifact_root=root, confirm_window_closed=True)
    assert not (tmp_path / "artifacts" / f"run-{claim.run_id}" / "browser-profile").exists()


def test_secure_connect_rejects_symlink_paths_and_creates_private_db(tmp_path: Path) -> None:
    target = tmp_path / "target.sqlite3"
    target.write_text("")
    symlink = tmp_path / "link.sqlite3"
    symlink.symlink_to(target)
    with pytest.raises(OSError):
        connect(symlink)
    real_parent = tmp_path / "real"
    real_parent.mkdir(mode=0o700)
    component = tmp_path / "component"
    component.symlink_to(real_parent, target_is_directory=True)
    with pytest.raises(OSError):
        connect(component / "nested.sqlite3")
    fresh = tmp_path / "fresh.sqlite3"
    conn = connect(fresh)
    conn.close()
    assert stat.S_IMODE(fresh.stat().st_mode) == 0o600


def test_application_reason_status_check_rejects_cross_products(tmp_path: Path) -> None:
    conn = connect(tmp_path / "jobs.sqlite3")
    _initialize(conn, tmp_path)
    job_id = _job(conn)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO application_runs (job_id, apply_url, status, reason_code, owner, started_at, finished_at) "
            "VALUES (?, 'gh_hash:x class=approved_greenhouse', 'review_ready', 'browser_error', 'o', 't', 't')",
            (job_id,),
        )
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO application_runs (job_id, apply_url, status, reason_code, owner, started_at, finished_at) "
            "VALUES (?, 'gh_hash:x class=approved_greenhouse', 'failed', 'draft_ready', 'o', 't', 't')",
            (job_id,),
        )


def test_barrier_single_queued_job_has_one_claim_winner(tmp_path: Path) -> None:
    db = tmp_path / "jobs.sqlite3"
    setup = connect(db)
    _initialize(setup, tmp_path)
    job_id = _job(setup, url="https://boards.greenhouse.io/acme/jobs/399")
    setup.close()
    barrier = threading.Barrier(2)

    def claim_once(owner: str) -> int | None:
        conn = connect(db)
        barrier.wait()
        claim = claim_next_application_job(conn, owner=owner)
        conn.close()
        return None if claim is None else int(claim.job["id"])

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(claim_once, ("a", "b")))
    assert outcomes.count(job_id) == 1
    assert outcomes.count(None) == 1


def test_legacy_migration_checkpoints_wal_and_erases_raw_payload(tmp_path: Path) -> None:
    db = tmp_path / "jobs.sqlite3"
    conn = connect(db)
    _jobs_schema(conn)
    job_id = _job(conn, url="https://boards.greenhouse.io/acme/jobs/902")
    _legacy_schema(conn)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute(
        "INSERT INTO application_runs (id, job_id, apply_url, status, reason, started_at, finished_at, observation_json, plan_json) "
        "VALUES (11, ?, ?, 'manual', ?, 't', 't', ?, ?)",
        (
            job_id,
            "https://boards.greenhouse.io/acme/jobs/902",
            "WAL_SENTINEL_REASON",
            json.dumps({"raw": "WAL_SENTINEL_OBSERVATION"}),
            json.dumps({"raw": "WAL_SENTINEL_PLAN"}),
        ),
    )
    conn.commit()
    _initialize(conn, tmp_path)
    conn.close()
    for path in (db, Path(f"{db}-wal"), Path(f"{db}-shm"), Path(f"{db}-journal")):
        if path.exists():
            assert b"WAL_SENTINEL" not in path.read_bytes()

def test_identity_payload_requires_exact_live_capture(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[int] = []
    def capture(pid: int) -> dict[str, object]:
        calls.append(pid)
        return {"pid": pid, "pgid": pid, "birth": "live-birth"}
    monkeypatch.setattr(db_module, "_capture_process_identity", capture)
    assert db_module._identity_payload(
        {"pid": 701, "pgid": 701, "birth": "live-birth"}, 701
    ) == {"pid": 701, "pgid": 701, "birth": "live-birth"}
    assert calls == [701]

    for observed, supplied in (
        (None, {"pid": 702, "pgid": 702, "birth": "x"}),
        ({"pid": 703, "pgid": 703, "birth": "x"}, {"pid": 704, "pgid": 704, "birth": "x"}),
        ({"pid": 705, "pgid": 706, "birth": "x"}, {"pid": 705, "pgid": 705, "birth": "x"}),
        ({"pid": 707, "pgid": 707, "birth": "live"}, {"pid": 707, "pgid": 707, "birth": "stale"}),
        ({"pid": 708, "pgid": 708, "probe_error": True}, {"pid": 708, "pgid": 708, "birth": "x"}),
    ):
        monkeypatch.setattr(db_module, "_capture_process_identity", lambda _pid, value=observed: value)
        with pytest.raises((RuntimeError, ValueError)):
            db_module._identity_payload(supplied, int(supplied["pid"]))


def test_process_registration_probe_failure_leaves_database_unchanged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    conn = connect(tmp_path / "jobs.sqlite3")
    _initialize(conn, tmp_path)
    _job(conn)
    claim = claim_next_application_job(conn, owner="owner")
    assert claim is not None
    assert register_application_artifact(conn, run_id=claim.run_id, artifact_dir=f"run-{claim.run_id}")
    assert register_application_session(conn, run_id=claim.run_id, session_id="session")
    assert mark_application_spawn_attempted(conn, run_id=claim.run_id, session_id="session")
    before = tuple(conn.execute(
        "SELECT owner_pid, browser_pid, observation_json FROM application_runs WHERE id=?",
        (claim.run_id,),
    ).fetchone())
    monkeypatch.setattr(db_module, "_capture_process_identity", lambda _pid: None)
    with pytest.raises(RuntimeError, match="unavailable"):
        register_application_owner_process(
            conn, run_id=claim.run_id, owner_pid=709,
            process_identity={"pid": 709, "pgid": 709, "birth": "fake"},
        )
    after = tuple(conn.execute(
        "SELECT owner_pid, browser_pid, observation_json FROM application_runs WHERE id=?",
        (claim.run_id,),
    ).fetchone())
    assert after == before


def test_malformed_registered_identity_refuses_group_probe_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    conn = connect(tmp_path / "jobs.sqlite3")
    _initialize(conn, tmp_path)
    _job(conn)
    claim = claim_next_application_job(conn, owner="owner")
    assert claim is not None
    finish_application_run(conn, run_id=claim.run_id, status="manual", reason_code="required_safe_fields_unresolved")
    conn.execute(
        "UPDATE application_runs SET owner_pid=710, observation_json=? WHERE id=?",
        (json.dumps({"_process": {"owner": {"pid": 710, "pgid": "malformed", "birth": "b"}}}), claim.run_id),
    )
    conn.commit()
    monkeypatch.setattr(db_module, "_process_group_state", lambda *_args, **_kwargs: pytest.fail("unsafe fallback probe"))
    with pytest.raises(RuntimeError, match="window_state_unknown"):
        retry_review(conn, run_id=claim.run_id, artifact_root=ArtifactRoot.open(tmp_path / "artifacts", cwd=tmp_path))

def test_reconcile_refuses_a_root_bound_to_another_database(tmp_path: Path) -> None:
    conn = connect(tmp_path / "jobs.sqlite3")
    _initialize(conn, tmp_path)
    _job(conn)
    claim = claim_next_application_job(conn, owner="owner")
    assert claim is not None
    other_conn = connect(tmp_path / "other.sqlite3")
    wrong_root = ArtifactRoot.open(tmp_path / "wrong-artifacts", cwd=tmp_path)
    initialize_database(other_conn, migration_artifact_root=wrong_root)
    before = tuple(
        conn.execute(
            "SELECT status, reason_code, reviewed_at FROM application_runs WHERE id=?",
            (claim.run_id,),
        ).fetchone()
    )
    with pytest.raises(RuntimeError, match="another database"):
        reconcile_open_session_failure(
            conn, run_id=claim.run_id, session_id=None, artifact_root=wrong_root
        )
    assert tuple(
        conn.execute(
            "SELECT status, reason_code, reviewed_at FROM application_runs WHERE id=?",
            (claim.run_id,),
        ).fetchone()
    ) == before


def test_failed_terminal_manifest_requires_reconciliation_before_review(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    conn = connect(tmp_path / "jobs.sqlite3")
    _initialize(conn, tmp_path)
    claim, root = _terminal_fixture(conn, tmp_path, monkeypatch, reason="artifact_error")
    with pytest.raises(RuntimeError, match="terminal manifest"):
        complete_review(conn, run_id=claim.run_id, outcome="skipped", artifact_root=root)
    with pytest.raises(RuntimeError, match="terminal manifest"):
        retry_review(conn, run_id=claim.run_id, artifact_root=root)
    assert reconcile_open_session_failure(
        conn, run_id=claim.run_id, session_id="terminal-session", artifact_root=root
    )
    result = retry_review(conn, run_id=claim.run_id, artifact_root=root)
    assert (result["status"], result["reason_code"], result["outcome"]) == (
        "failed",
        "artifact_error",
        "retry",
    )


@pytest.mark.parametrize(
    ("terminal_reason", "expected_status"),
    (("artifact_error", "failed"), ("unsafe_network_attempt", "blocked")),
)
def test_terminal_manifest_failure_codes_map_to_exact_status(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    terminal_reason: str,
    expected_status: str,
) -> None:
    conn = connect(tmp_path / "jobs.sqlite3")
    _initialize(conn, tmp_path)
    claim, root = _terminal_fixture(conn, tmp_path, monkeypatch, reason=terminal_reason)
    assert reconcile_open_session_failure(
        conn, run_id=claim.run_id, session_id="terminal-session", artifact_root=root
    )
    row = conn.execute(
        "SELECT status, reason_code FROM application_runs WHERE id=?", (claim.run_id,)
    ).fetchone()
    assert (row["status"], row["reason_code"]) == (expected_status, terminal_reason)
    if terminal_reason == "unsafe_network_attempt":
        with pytest.raises(RuntimeError, match="failed"):
            complete_review(
                conn,
                run_id=claim.run_id,
                outcome="submitted",
                artifact_root=root,
            )


def test_legacy_checkpoint_failure_commits_and_retries_from_durable_marker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = tmp_path / "jobs.sqlite3"
    conn = connect(db)
    _jobs_schema(conn)
    job_id = _job(conn, url="https://boards.greenhouse.io/acme/jobs/903")
    _legacy_schema(conn)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute(
        "INSERT INTO application_runs (id, job_id, apply_url, status, reason, started_at, finished_at, observation_json, plan_json) "
        "VALUES (12, ?, ?, 'manual', 'legacy-reason', 't', 't', '{}', '{}')",
        (job_id, "https://boards.greenhouse.io/acme/jobs/903"),
    )
    conn.commit()
    calls = 0
    original_checkpoint = db_module._checkpoint_wal

    def fail_once(connection: sqlite3.Connection) -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("injected checkpoint failure")
        original_checkpoint(connection)

    monkeypatch.setattr(db_module, "_checkpoint_wal", fail_once)
    with ArtifactRoot.open(tmp_path / "artifacts", cwd=tmp_path) as root:
        with pytest.raises(RuntimeError, match="migration committed; WAL checkpoint pending"):
            initialize_database(conn, migration_artifact_root=root)
        assert db_module._is_target_application_schema(conn)
        pending = conn.execute(
            "SELECT value FROM application_migration_state WHERE key=?",
            ("legacy_wal_checkpoint",),
        ).fetchone()
        assert pending["value"] == "pending"
        initialize_database(conn, migration_artifact_root=root)
    assert calls == 2
    assert conn.execute(
        "SELECT 1 FROM application_migration_state WHERE key=?",
        ("legacy_wal_checkpoint",),
    ).fetchone() is None

def test_review_manifest_rejects_version_identity_projection_and_token_forgery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    conn = connect(tmp_path / "jobs.sqlite3")
    _initialize(conn, tmp_path)
    claim, root = _terminal_fixture(conn, tmp_path, monkeypatch, reason="artifact_error")
    manifest_path = tmp_path / "artifacts" / f"run-{claim.run_id}" / "review_session.json"
    valid = json.loads(manifest_path.read_text())
    malformed = (
        {**valid, "version": True},
        {**valid, "run_id": True},
        {**valid, "job_id": True},
        {**valid, "cleanup": 1, "terminal_reason": None, "state": "open"},
        {
            **valid,
            "owner_identity": {**valid["owner_identity"], "birth": 123},
        },
        {**valid, "owner_pid": valid["owner_pid"] + 1},
        {**valid, "commit_token_sha256": "b" * 64},
    )
    for candidate in malformed:
        with root.create_run_dir(claim.run_id) as run:
            run.write_json("review_session.json", candidate)
        with pytest.raises(RuntimeError, match="window_state_unknown"):
            retry_review(conn, run_id=claim.run_id, artifact_root=root)
    with root.create_run_dir(claim.run_id) as run:
        run.write_json("review_session.json", valid)


def test_review_list_malformed_manifest_isolated_to_unknown_row(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    conn = connect(tmp_path / "jobs.sqlite3")
    _initialize(conn, tmp_path)
    first_job = _job(conn, url="https://boards.greenhouse.io/acme/jobs/701")
    first = claim_next_application_job(conn, owner="owner")
    assert first is not None and first.job["id"] == first_job
    root = ArtifactRoot.open(tmp_path / "artifacts", cwd=tmp_path)
    assert register_application_artifact(conn, run_id=first.run_id, artifact_dir=f"run-{first.run_id}")
    assert register_application_session(conn, run_id=first.run_id, session_id="list-session", session_state="starting")
    with root.create_run_dir(first.run_id) as run:
        _write_review_manifest(
            run,
            {
                "run_id": first.run_id,
                "job_id": first_job,
                "session_id": "list-session",
                "state": "starting",
                "spawn_attempted": False,
                "cleanup": "complete",
            },
        )
    finish_application_run(
        conn,
        run_id=first.run_id,
        status="manual",
        reason_code="required_safe_fields_unresolved",
        artifact_dir=f"run-{first.run_id}",
    )
    second_job = _job(conn, url="https://boards.greenhouse.io/acme/jobs/702")
    second = claim_next_application_job(conn, owner="owner")
    assert second is not None and second.job["id"] == second_job
    finish_application_run(conn, run_id=second.run_id, status="manual", reason_code="required_safe_fields_unresolved")
    with root.create_run_dir(first.run_id) as run:
        run.write_json(
            "review_session.json",
            {
                "version": 1,
                "run_id": first.run_id,
                "job_id": first_job,
                "session_id": "list-session",
                "state": "starting",
                "spawn_attempted": False,
                "owner_identity": {"pid": 12345, "pgid": 12345, "birth": 123},
                "commit_token_sha256": "a" * 64,
            },
        )
    rows = list_application_reviews(conn, limit=10, artifact_root=root)
    assert {row["run_id"] for row in rows} == {first.run_id, second.run_id}
    assert next(row for row in rows if row["run_id"] == first.run_id)["window_state"] == "unknown"


def test_retry_running_claim_marks_abandoned_and_requeues(tmp_path: Path) -> None:
    conn = connect(tmp_path / "jobs.sqlite3")
    _initialize(conn, tmp_path)
    job_id = _job(conn)
    claim = claim_next_application_job(conn, owner="owner")
    assert claim is not None
    with ArtifactRoot.open(tmp_path / "artifacts", cwd=tmp_path) as root:
        result = retry_review(
            conn,
            run_id=claim.run_id,
            artifact_root=root,
        )
    assert result["status"] == "failed"
    assert result["reason_code"] == "abandoned_running_attempt"
    assert result["outcome"] == "retry"
    row = conn.execute(
        "SELECT status, reason_code, outcome, reviewed_at FROM application_runs WHERE id=?",
        (claim.run_id,),
    ).fetchone()
    assert (row["status"], row["reason_code"], row["outcome"]) == (
        "failed",
        "abandoned_running_attempt",
        "retry",
    )
    assert row["reviewed_at"] is not None
    assert conn.execute("SELECT status FROM jobs WHERE id=?", (job_id,)).fetchone()["status"] == "queued"


def test_reconcile_confirmed_stale_spawned_session_marks_terminal_browser_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    conn = connect(tmp_path / "jobs.sqlite3")
    _initialize(conn, tmp_path)
    claim, root = _stale_spawn_fixture(conn, tmp_path, monkeypatch)
    run_dir = tmp_path / "artifacts" / f"run-{claim.run_id}"
    before_manifest = json.loads((run_dir / "review_session.json").read_text())
    probes: list[tuple[int, dict[str, object] | None]] = []

    def absent(pid: int, *, expected: dict[str, object] | None = None) -> str:
        probes.append((pid, expected))
        return "absent"

    monkeypatch.setattr(db_module, "_process_group_state", absent)
    assert reconcile_open_session_failure(
        conn,
        run_id=claim.run_id,
        session_id="stale-session",
        artifact_root=root,
    )
    updated_manifest = json.loads((run_dir / "review_session.json").read_text())
    expected_manifest = {
        **before_manifest,
        "state": "failed",
        "terminal_reason": "browser_error",
        "cleanup": "confirmed_stale",
    }
    assert updated_manifest == expected_manifest
    assert probes == [
        (12345, {"pid": 12345, "pgid": 12345, "birth": "birth-owner"}),
        (23456, {"pid": 23456, "pgid": 23456, "birth": "birth-browser"}),
    ]
    assert not (run_dir / "browser-profile").exists()
    assert not (run_dir / "input").exists()
    row = conn.execute(
        "SELECT status, reason_code, observation_json FROM application_runs WHERE id=?",
        (claim.run_id,),
    ).fetchone()
    assert (row["status"], row["reason_code"]) == ("failed", "browser_error")
    observation = json.loads(row["observation_json"])
    assert observation["_terminal_reconciled"] == {
        "session_id": "stale-session",
        "reason_code": "browser_error",
    }


@pytest.mark.parametrize("guard", ["live", "unknown", "birth_mismatch", "non_latest", "reviewed", "non_browser_error"])
def test_reconcile_stale_spawn_guards_leave_database_and_manifest_unchanged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, guard: str
) -> None:
    conn = connect(tmp_path / "jobs.sqlite3")
    _initialize(conn, tmp_path)
    owner_birth = "birth-owner" if guard != "birth_mismatch" else "different-owner"
    reason = "browser_error" if guard != "non_browser_error" else "artifact_error"
    claim, root = _stale_spawn_fixture(
        conn,
        tmp_path,
        monkeypatch,
        owner_birth=owner_birth,
        reason=reason,
    )
    run_dir = tmp_path / "artifacts" / f"run-{claim.run_id}"
    before_manifest = (run_dir / "review_session.json").read_bytes()
    before_run = (run_dir / "run.json").read_bytes()
    before_row = tuple(
        conn.execute(
            "SELECT status, reason_code, observation_json, reviewed_at FROM application_runs WHERE id=?",
            (claim.run_id,),
        ).fetchone()
    )
    if guard == "live":
        monkeypatch.setattr(db_module, "_process_group_state", lambda _pid, expected=None: "live")
    elif guard == "unknown":
        monkeypatch.setattr(db_module, "_process_group_state", lambda _pid, expected=None: "unknown")
    elif guard == "non_latest":
        _job(conn, url="https://boards.greenhouse.io/acme/jobs/newer")
        assert claim_next_application_job(conn, owner="owner") is not None
    elif guard == "reviewed":
        conn.execute(
            "UPDATE application_runs SET outcome=?, reviewed_at=? WHERE id=?",
            ("skipped", "2026-07-11T00:00:00+00:00", claim.run_id),
        )
        conn.commit()
    assert not reconcile_open_session_failure(
        conn,
        run_id=claim.run_id,
        session_id="stale-session",
        artifact_root=root,
    )
    after_row = tuple(
        conn.execute(
            "SELECT status, reason_code, observation_json, reviewed_at FROM application_runs WHERE id=?",
            (claim.run_id,),
        ).fetchone()
    )
    assert after_row == before_row if guard != "reviewed" else after_row[0:3] == before_row[0:3]
    assert (run_dir / "review_session.json").read_bytes() == before_manifest
    assert (run_dir / "run.json").read_bytes() == before_run
    assert (run_dir / "browser-profile").exists()
    assert (run_dir / "input").exists()


def test_spawn_attempted_false_reconciles_pre_spawn_handoff_failure(tmp_path: Path) -> None:
    conn = connect(tmp_path / "jobs.sqlite3")
    _initialize(conn, tmp_path)
    root = ArtifactRoot.open(tmp_path / "artifacts", cwd=tmp_path)

    _job(conn, url="https://boards.greenhouse.io/acme/jobs/pre-spawn-false")
    claim = claim_next_application_job(conn, owner="owner")
    assert claim is not None
    assert register_application_artifact(conn, run_id=claim.run_id, artifact_dir=f"run-{claim.run_id}")
    assert register_application_session(
        conn,
        run_id=claim.run_id,
        session_id="pre-spawn-false",
        session_state="starting",
    )
    with root.create_run_dir(claim.run_id) as run:
        _write_review_manifest(run, {
            "run_id": claim.run_id,
            "job_id": claim.job["id"],
            "session_id": "pre-spawn-false",
            "state": "starting",
            "spawn_attempted": False,
            "process": {},
        })
    assert reconcile_open_session_failure(
        conn,
        run_id=claim.run_id,
        session_id="pre-spawn-false",
        artifact_root=root,
    )
    row = conn.execute(
        "SELECT status, reason_code FROM application_runs WHERE id=?",
        (claim.run_id,),
    ).fetchone()
    assert (row["status"], row["reason_code"]) == ("failed", "handoff_failed")

    _job(conn, url="https://boards.greenhouse.io/acme/jobs/pre-spawn-true")
    refused = claim_next_application_job(conn, owner="owner")
    assert refused is not None
    assert register_application_artifact(
        conn,
        run_id=refused.run_id,
        artifact_dir=f"run-{refused.run_id}",
    )
    assert register_application_session(
        conn,
        run_id=refused.run_id,
        session_id="pre-spawn-true",
        session_state="starting",
    )
    assert mark_application_spawn_attempted(
        conn,
        run_id=refused.run_id,
        session_id="pre-spawn-true",
    )
    assert not mark_application_spawn_attempted(
        conn,
        run_id=refused.run_id,
        session_id="pre-spawn-true",
    )
    with root.create_run_dir(refused.run_id) as run:
        _write_review_manifest(run, {
            "run_id": refused.run_id,
            "job_id": refused.job["id"],
            "session_id": "pre-spawn-true",
            "state": "starting",
            "spawn_attempted": True,
            "process": {},
        })
    before_run = tuple(conn.execute(
        "SELECT status, reason_code FROM application_runs WHERE id=?",
        (refused.run_id,),
    ).fetchone())
    before_job = conn.execute(
        "SELECT status FROM jobs WHERE id=?",
        (refused.job["id"],),
    ).fetchone()["status"]
    assert not reconcile_open_session_failure(
        conn,
        run_id=refused.run_id,
        session_id="pre-spawn-true",
        artifact_root=root,
    )
    assert tuple(conn.execute(
        "SELECT status, reason_code FROM application_runs WHERE id=?",
        (refused.run_id,),
    ).fetchone()) == before_run
    assert conn.execute(
        "SELECT status FROM jobs WHERE id=?",
        (refused.job["id"],),
    ).fetchone()["status"] == before_job
    with root.open_run_dir(refused.run_id) as run:
        manifest = run.read_json("review_session.json")
        del manifest["spawn_attempted"]
        run.replace_json("review_session.json", manifest)
    assert not reconcile_open_session_failure(
        conn,
        run_id=refused.run_id,
        session_id="pre-spawn-true",
        artifact_root=root,
    )
    assert tuple(conn.execute(
        "SELECT status, reason_code FROM application_runs WHERE id=?",
        (refused.run_id,),
    ).fetchone()) == before_run
    assert conn.execute(
        "SELECT status FROM jobs WHERE id=?",
        (refused.job["id"],),
    ).fetchone()["status"] == before_job
    root.close()
