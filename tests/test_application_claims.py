from __future__ import annotations

import asyncio
import concurrent.futures
import hashlib
import json
import os
import signal
import sqlite3
import stat
import threading
from datetime import datetime, timezone
from pathlib import Path
from uuid import UUID, uuid5
import pytest

import jobs_assistant.application as application_module
import jobs_assistant.db as db_module
from jobs_assistant.application_rpc_contracts import (
    APPLICATION_RPC_PROTOCOL_VERSION,
    ApplicationRpcRequest,
    build_application_response,
    parse_application_request,
)

from jobs_assistant.artifacts import ArtifactRoot
from jobs_assistant.db import (
    PUBLIC_REASON_CODES,
    RpcClaimOutcome,
    RpcRequestInfo,
    RpcRunTransition,
    append_rpc_event,
    application_schema_fingerprint,
    claim_application_job_for_rpc,
    claim_next_application_job,
    bind_rpc_handoff_intent,
    commit_rpc_proposal_failure,
    commit_rpc_proposal_result,
    commit_rpc_failure,
    commit_rpc_run_transition,
    complete_review,
    complete_rpc_request,
    connect,
    finish_application_run,
    get_application_review_details,
    get_rpc_request,
    get_rpc_run_status,
    initialize_database,
    latest_rpc_event,
    list_application_reviews,
    mark_application_spawn_attempted,
    recover_rpc_handoffs,
    reconcile_abandoned_rpc_runs,
    reconcile_open_session_failure,
    register_application_artifact,
    register_application_browser_process,
    register_application_owner_process,
    register_application_session,
    replay_rpc_events,
    request_rpc_cancellation,
    reserve_rpc_request,
    retry_review,
    review_window_state,
    rpc_schema_fingerprint,
    update_rpc_run_artifact_manifest,
    update_rpc_run_observation,
    update_rpc_run_process,
    update_rpc_run_state,
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


def _write_review_manifest(
    run: object, payload: dict[str, object], *, token: str | None = "a" * 64
) -> None:
    """Write the current versioned review manifest and its run-token binding."""
    manifest = dict(payload)
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


_ARTIFACT_KEY_PATHS = {
    "observation": "observation.json",
    "plan": "plan.json",
    "actions": "actions.json",
    "browser_failure": "browser_failure.json",
}


def _valid_observation(blocker_codes: list[str] | None = None) -> dict[str, object]:
    return {
        "field_count": 5,
        "button_count": 1,
        "required_count": 2,
        "final_marker_count": 1,
        "error_count": 0,
        "blocker_codes": list(blocker_codes or []),
    }


def _valid_plan() -> dict[str, object]:
    return {
        "status": "ready",
        "reason_code": "draft_ready",
        "answer_count": 3,
        "skipped_target_count": 0,
        "resume_upload": False,
        "safe_click": False,
    }


def _valid_actions() -> dict[str, object]:
    return {
        "mutation_count": 0,
        "actions": [],
        "final_submit_calls": 0,
    }


def _valid_browser_failure(*, ats_policy: str = "greenhouse") -> dict[str, object]:
    return {
        "version": 1,
        "stage": "observation",
        "operation": "observe",
        "code": "browser_command_failed",
        "iteration": 1,
        "ats_policy": ats_policy,
        "no_final_submit": True,
        "protocol": "length-prefixed-json-v1",
    }


def _review_details_run(
    conn: sqlite3.Connection,
    tmp_path: Path,
    *,
    status: str = "review_ready",
    reason_code: str | None = "draft_ready",
    outcome: str | None = None,
    reviewed_at: str | None = None,
    finished_at: str | None = "2026-07-10T00:01:00Z",
    manifest_stage: str = "finished",
    manifest: dict[str, object] | None = None,
    artifacts: dict[str, object] | None = None,
) -> tuple[ArtifactRoot, int, int]:
    job_id = _job(conn)
    conn.execute(
        """
        INSERT INTO application_runs (
            job_id, apply_url, status, owner, started_at, finished_at,
            reason_code, artifact_dir, outcome, reviewed_at
        ) VALUES (?, ?, ?, 'owner', '2026-07-10T00:00:00Z',
                  ?, ?, 'run-1', ?, ?)
        """,
        (
            job_id,
            "https://greenhouse.example.test/jobs/1",
            status,
            finished_at,
            reason_code,
            outcome,
            reviewed_at,
        ),
    )
    conn.commit()
    run_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    root = ArtifactRoot.open(tmp_path / "artifacts", cwd=tmp_path)
    run = root.create_run_dir(run_id)
    artifact_descriptors: dict[str, object] = {}
    for key, payload in (artifacts or {}).items():
        rel_path = _ARTIFACT_KEY_PATHS[key]
        result = run.write_json(rel_path, payload)
        artifact_descriptors[key] = {
            "path": rel_path,
            "sha256": result.sha256,
            "iteration": 1,
            "stage": manifest_stage,
        }
    final_manifest = dict(manifest or {})
    final_manifest.setdefault("run_id", run_id)
    final_manifest.setdefault("job_id", job_id)
    final_manifest.setdefault("ats_policy", "greenhouse")
    final_manifest.setdefault("no_final_submit", True)
    final_manifest.setdefault("stage", manifest_stage)
    final_manifest.setdefault("latest_iteration", 1)
    final_manifest.setdefault("latest_stage", manifest_stage)
    final_manifest.setdefault("latest", {"iteration": 1, "stage": manifest_stage})
    final_manifest.setdefault("commit_token_sha256", None)
    final_manifest["artifacts"] = artifact_descriptors
    run.write_json("run.json", final_manifest)
    run.close()
    conn.execute(
        "UPDATE application_runs SET artifact_dir=? WHERE id=?",
        (f"run-{run_id}", run_id),
    )
    conn.commit()
    return root, run_id, job_id


def _minimal_review_db(tmp_path: Path) -> tuple[sqlite3.Connection, ArtifactRoot]:
    """Return an in-memory DB and bound artifact root with relaxed constraints.

    This lets us exercise the public-output validators for values that the
    real schema's CHECK constraints would otherwise reject.
    """
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        """
        CREATE TABLE jobs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source TEXT NOT NULL,
            source_job_id TEXT,
            canonical_url TEXT,
            title TEXT NOT NULL,
            company TEXT NOT NULL,
            description TEXT,
            discovered_at TEXT NOT NULL,
            raw_json TEXT NOT NULL DEFAULT '{}',
            first_seen_at TEXT NOT NULL,
            last_seen_at TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'queued'
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE application_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            job_id INTEGER NOT NULL,
            apply_url TEXT NOT NULL,
            status TEXT NOT NULL,
            reason_code TEXT,
            owner TEXT NOT NULL,
            started_at TEXT NOT NULL,
            finished_at TEXT,
            outcome TEXT,
            reviewed_at TEXT,
            observation_json TEXT,
            artifact_dir TEXT,
            session_id TEXT,
            owner_pid INTEGER,
            browser_pid INTEGER
        )
        """
    )
    root = ArtifactRoot.open(tmp_path / "artifacts", cwd=tmp_path)
    db_module._bind_artifact_root(conn, root, create=True)
    return conn, root


def _insert_minimal_review_run(
    conn: sqlite3.Connection,
    root: ArtifactRoot,
    tmp_path: Path,
    *,
    status: str = "review_ready",
    reason_code: str | None = "draft_ready",
    job_status: str = "in_progress",
    title: str = "Engineer",
    company: str = "Acme",
    started_at: str = "2026-07-10T00:00:00Z",
    finished_at: str | None = "2026-07-10T00:01:00Z",
    manifest_stage: str = "finished",
) -> tuple[int, int]:
    conn.execute(
        """
        INSERT INTO jobs (
            source, source_job_id, canonical_url, title, company, discovered_at,
            raw_json, first_seen_at, last_seen_at, status
        ) VALUES (
            'fixture', '1', 'https://greenhouse.example.test/jobs/1', ?, ?, '2026-07-10T00:00:00Z',
            '{}', '2026-07-10T00:00:00Z', '2026-07-10T00:00:00Z', ?
        )
        """,
        (title, company, job_status),
    )
    conn.commit()
    job_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    conn.execute(
        """
        INSERT INTO application_runs (
            job_id, apply_url, status, owner, started_at, finished_at,
            reason_code, artifact_dir
        ) VALUES (?, ?, ?, 'owner', ?, ?, ?, 'run-1')
        """,
        (job_id, "https://greenhouse.example.test/jobs/1", status, started_at, finished_at, reason_code),
    )
    conn.commit()
    run_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    run = root.create_run_dir(run_id)
    manifest = {
        "run_id": run_id,
        "job_id": job_id,
        "ats_policy": "greenhouse",
        "no_final_submit": True,
        "stage": manifest_stage,
        "latest": {"iteration": 1, "stage": manifest_stage},
        "artifacts": {},
    }
    run.write_json("run.json", manifest)
    run.close()
    return run_id, job_id


def test_review_details_finished_run_returns_all_summaries(tmp_path: Path) -> None:
    conn = connect(tmp_path / "jobs.sqlite3")
    _initialize(conn, tmp_path)
    root, run_id, job_id = _review_details_run(
        conn,
        tmp_path,
        status="review_ready",
        reason_code="draft_ready",
        manifest_stage="finished",
        artifacts={
            "observation": _valid_observation(),
            "plan": _valid_plan(),
            "actions": _valid_actions(),
        },
    )
    result = get_application_review_details(conn, run_id=run_id, artifact_root=root)
    assert result["run_id"] == run_id
    assert result["job_id"] == job_id
    assert result["status"] == "review_ready"
    assert result["reason_code"] == "draft_ready"
    assert result["ats"] == "greenhouse"
    assert result["evidence"]["stage"] == "finished"
    assert result["observation"] is not None
    assert result["plan"] is not None
    assert result["actions"] is not None
    assert result["browser_failure"] is None
    root.close()


def test_review_details_early_failed_run_with_only_browser_failure(tmp_path: Path) -> None:
    conn = connect(tmp_path / "jobs.sqlite3")
    _initialize(conn, tmp_path)
    root, run_id, job_id = _review_details_run(
        conn,
        tmp_path,
        status="failed",
        reason_code="browser_error",
        manifest_stage="failed",
        artifacts={"browser_failure": _valid_browser_failure()},
    )
    result = get_application_review_details(conn, run_id=run_id, artifact_root=root)
    assert result["run_id"] == run_id
    assert result["job_id"] == job_id
    assert result["status"] == "failed"
    assert result["reason_code"] == "browser_error"
    assert result["observation"] is None
    assert result["plan"] is None
    assert result["actions"] is None
    assert result["browser_failure"] == {
        "stage": "observation",
        "operation": "observe",
        "code": "browser_command_failed",
        "iteration": 1,
        "ats": "greenhouse",
        "no_final_submit": True,
    }
    root.close()


def test_review_details_failed_run_without_failure_artifact(tmp_path: Path) -> None:
    conn = connect(tmp_path / "jobs.sqlite3")
    _initialize(conn, tmp_path)
    root, run_id, _ = _review_details_run(
        conn,
        tmp_path,
        status="failed",
        reason_code="browser_error",
        manifest_stage="failed",
        artifacts={},
    )
    result = get_application_review_details(conn, run_id=run_id, artifact_root=root)
    assert result["status"] == "failed"
    assert result["reason_code"] == "browser_error"
    assert result["observation"] is None
    assert result["plan"] is None
    assert result["actions"] is None
    assert result["browser_failure"] is None
    root.close()


def test_review_details_claimed_and_running_runs_are_publicly_queryable(tmp_path: Path) -> None:
    conn = connect(tmp_path / "jobs.sqlite3")
    _initialize(conn, tmp_path)
    root, run_id, _ = _review_details_run(
        conn,
        tmp_path,
        status="running",
        reason_code=None,
        manifest_stage="claimed",
        artifacts={},
        finished_at=None,
    )
    result = get_application_review_details(conn, run_id=run_id, artifact_root=root)
    assert result["status"] == "running"
    assert result["reason_code"] is None
    assert result["observation"] is None
    assert result["plan"] is None
    assert result["actions"] is None
    assert result["browser_failure"] is None
    root.close()


def test_review_details_finished_run_missing_required_artifact_rejected(tmp_path: Path) -> None:
    conn = connect(tmp_path / "jobs.sqlite3")
    _initialize(conn, tmp_path)
    root, run_id, _ = _review_details_run(
        conn,
        tmp_path,
        status="review_ready",
        reason_code="draft_ready",
        manifest_stage="finished",
        artifacts={
            "observation": _valid_observation(),
            "plan": _valid_plan(),
            # actions intentionally omitted
        },
    )
    with pytest.raises(RuntimeError, match="manifest_error"):
        get_application_review_details(conn, run_id=run_id, artifact_root=root)
    root.close()


def test_review_details_invalid_status_rejected(tmp_path: Path) -> None:
    conn, root = _minimal_review_db(tmp_path)
    run_id, _ = _insert_minimal_review_run(
        conn, root, tmp_path, status="bogus"
    )
    with pytest.raises(RuntimeError, match="manifest_error"):
        get_application_review_details(conn, run_id=run_id, artifact_root=root)
    root.close()


def test_review_details_invalid_job_status_rejected(tmp_path: Path) -> None:
    conn, root = _minimal_review_db(tmp_path)
    run_id, _ = _insert_minimal_review_run(
        conn, root, tmp_path, job_status="bogus"
    )
    with pytest.raises(RuntimeError, match="manifest_error"):
        get_application_review_details(conn, run_id=run_id, artifact_root=root)
    root.close()


def test_review_details_reason_code_mismatch_rejected(tmp_path: Path) -> None:
    conn, root = _minimal_review_db(tmp_path)
    run_id, _ = _insert_minimal_review_run(
        conn, root, tmp_path, status="failed", reason_code="draft_ready"
    )
    with pytest.raises(RuntimeError, match="manifest_error"):
        get_application_review_details(conn, run_id=run_id, artifact_root=root)
    root.close()


@pytest.mark.parametrize(
    ("column", "table", "value"),
    [
        ("title", "jobs", ""),
        ("company", "jobs", ""),
        ("started_at", "application_runs", ""),
    ],
)
def test_review_details_empty_db_text_rejected(
    tmp_path: Path, column: str, table: str, value: str
) -> None:
    conn = connect(tmp_path / "jobs.sqlite3")
    _initialize(conn, tmp_path)
    root, run_id, _ = _review_details_run(
        conn,
        tmp_path,
        status="review_ready",
        reason_code="draft_ready",
        manifest_stage="finished",
        artifacts={
            "observation": _valid_observation(),
            "plan": _valid_plan(),
            "actions": _valid_actions(),
        },
    )
    conn.execute(f"UPDATE {table} SET {column}=? WHERE id=(SELECT job_id FROM application_runs WHERE id=?)" if table == "jobs" else f"UPDATE {table} SET {column}=? WHERE id=?", (value, run_id))
    conn.commit()
    with pytest.raises(RuntimeError, match="manifest_error"):
        get_application_review_details(conn, run_id=run_id, artifact_root=root)
    root.close()


def test_review_details_oversized_title_rejected(tmp_path: Path) -> None:
    conn = connect(tmp_path / "jobs.sqlite3")
    _initialize(conn, tmp_path)
    root, run_id, _ = _review_details_run(
        conn,
        tmp_path,
        status="review_ready",
        reason_code="draft_ready",
        manifest_stage="finished",
        artifacts={
            "observation": _valid_observation(),
            "plan": _valid_plan(),
            "actions": _valid_actions(),
        },
    )
    conn.execute("UPDATE jobs SET title=? WHERE id=(SELECT job_id FROM application_runs WHERE id=?)", ("x" * 513, run_id))
    conn.commit()
    with pytest.raises(RuntimeError, match="manifest_error"):
        get_application_review_details(conn, run_id=run_id, artifact_root=root)
    root.close()


def test_review_details_control_char_in_company_rejected(tmp_path: Path) -> None:
    conn = connect(tmp_path / "jobs.sqlite3")
    _initialize(conn, tmp_path)
    root, run_id, _ = _review_details_run(
        conn,
        tmp_path,
        status="review_ready",
        reason_code="draft_ready",
        manifest_stage="finished",
        artifacts={
            "observation": _valid_observation(),
            "plan": _valid_plan(),
            "actions": _valid_actions(),
        },
    )
    conn.execute("UPDATE jobs SET company=? WHERE id=(SELECT job_id FROM application_runs WHERE id=?)", ("bad\x00company", run_id))
    conn.commit()
    with pytest.raises(RuntimeError, match="manifest_error"):
        get_application_review_details(conn, run_id=run_id, artifact_root=root)
    root.close()


def test_review_details_oversized_timestamp_rejected(tmp_path: Path) -> None:
    conn = connect(tmp_path / "jobs.sqlite3")
    _initialize(conn, tmp_path)
    root, run_id, _ = _review_details_run(
        conn,
        tmp_path,
        status="review_ready",
        reason_code="draft_ready",
        manifest_stage="finished",
        artifacts={
            "observation": _valid_observation(),
            "plan": _valid_plan(),
            "actions": _valid_actions(),
        },
    )
    conn.execute(
        "UPDATE application_runs SET started_at=? WHERE id=?",
        ("x" * 65, run_id),
    )
    conn.commit()
    with pytest.raises(RuntimeError, match="manifest_error"):
        get_application_review_details(conn, run_id=run_id, artifact_root=root)
    root.close()


def test_review_details_browser_failure_ats_mismatch_rejected(tmp_path: Path) -> None:
    conn = connect(tmp_path / "jobs.sqlite3")
    _initialize(conn, tmp_path)
    failure = _valid_browser_failure(ats_policy="lever")
    root, run_id, _ = _review_details_run(
        conn,
        tmp_path,
        status="failed",
        reason_code="browser_error",
        manifest_stage="failed",
        artifacts={"browser_failure": failure},
    )
    with pytest.raises(RuntimeError, match="manifest_error"):
        get_application_review_details(conn, run_id=run_id, artifact_root=root)
    root.close()


def test_review_details_browser_failure_no_final_submit_false_rejected(tmp_path: Path) -> None:
    conn = connect(tmp_path / "jobs.sqlite3")
    _initialize(conn, tmp_path)
    failure = _valid_browser_failure()
    failure["no_final_submit"] = False
    root, run_id, _ = _review_details_run(
        conn,
        tmp_path,
        status="failed",
        reason_code="browser_error",
        manifest_stage="failed",
        artifacts={"browser_failure": failure},
    )
    with pytest.raises(RuntimeError, match="manifest_error"):
        get_application_review_details(conn, run_id=run_id, artifact_root=root)
    root.close()


def test_review_details_browser_failure_unsafe_code_rejected(tmp_path: Path) -> None:
    conn = connect(tmp_path / "jobs.sqlite3")
    _initialize(conn, tmp_path)
    failure = _valid_browser_failure()
    failure["code"] = "not_a_safe_code"
    root, run_id, _ = _review_details_run(
        conn,
        tmp_path,
        status="failed",
        reason_code="browser_error",
        manifest_stage="failed",
        artifacts={"browser_failure": failure},
    )
    with pytest.raises(RuntimeError, match="manifest_error"):
        get_application_review_details(conn, run_id=run_id, artifact_root=root)
    root.close()


@pytest.mark.parametrize(
    ("stage", "operation"),
    [
        ("not_a_stage", "observe"),
        ("observation", "not_an_operation"),
    ],
)
def test_review_details_browser_failure_invalid_stage_operation_rejected(
    tmp_path: Path, stage: str, operation: str
) -> None:
    conn = connect(tmp_path / "jobs.sqlite3")
    _initialize(conn, tmp_path)
    failure = _valid_browser_failure()
    failure["stage"] = stage
    failure["operation"] = operation
    root, run_id, _ = _review_details_run(
        conn,
        tmp_path,
        status="failed",
        reason_code="browser_error",
        manifest_stage="failed",
        artifacts={"browser_failure": failure},
    )
    with pytest.raises(RuntimeError, match="manifest_error"):
        get_application_review_details(conn, run_id=run_id, artifact_root=root)
    root.close()


@pytest.mark.parametrize(
    ("stage", "operation"),
    [
        ("startup", "fill"),
        ("navigation", "observe"),
        ("observation", "fill"),
        ("mutation", "start"),
        ("handoff", "close"),
        ("cleanup", "goto"),
    ],
)
def test_review_details_browser_failure_cross_paired_stage_operation_rejected(
    tmp_path: Path, stage: str, operation: str
) -> None:
    """Reject valid stage and operation values that are not an emitted pair."""
    conn = connect(tmp_path / "jobs.sqlite3")
    _initialize(conn, tmp_path)
    failure = _valid_browser_failure()
    failure["stage"] = stage
    failure["operation"] = operation
    root, run_id, _ = _review_details_run(
        conn,
        tmp_path,
        status="failed",
        reason_code="browser_error",
        manifest_stage="failed",
        artifacts={"browser_failure": failure},
    )
    with pytest.raises(RuntimeError, match="manifest_error"):
        get_application_review_details(conn, run_id=run_id, artifact_root=root)
    root.close()


def test_review_details_missing_latest_rejected(tmp_path: Path) -> None:
    """A current manifest must always include the bounded latest metadata."""
    conn = connect(tmp_path / "jobs.sqlite3")
    _initialize(conn, tmp_path)
    root, run_id, _ = _review_details_run(
        conn,
        tmp_path,
        status="review_ready",
        reason_code="draft_ready",
        manifest_stage="finished",
        artifacts={
            "observation": _valid_observation(),
            "plan": _valid_plan(),
            "actions": _valid_actions(),
        },
    )
    run_dir = tmp_path / "artifacts" / f"run-{run_id}"
    manifest_path = run_dir / "run.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    del manifest["latest"]
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(RuntimeError, match="manifest_error"):
        get_application_review_details(conn, run_id=run_id, artifact_root=root)
    root.close()


@pytest.mark.parametrize(
    "latest",
    [
        None,
        {"iteration": 1},
        {"stage": "finished"},
        {"iteration": "1", "stage": "finished"},
        {"iteration": -1, "stage": "finished"},
        {"iteration": 1, "stage": "bogus"},
    ],
)
def test_review_details_malformed_latest_rejected(
    tmp_path: Path, latest: object
) -> None:
    """latest must be a dict with bounded iteration and a known review stage."""
    conn = connect(tmp_path / "jobs.sqlite3")
    _initialize(conn, tmp_path)
    root, run_id, _ = _review_details_run(
        conn,
        tmp_path,
        status="review_ready",
        reason_code="draft_ready",
        manifest_stage="finished",
        artifacts={
            "observation": _valid_observation(),
            "plan": _valid_plan(),
            "actions": _valid_actions(),
        },
        manifest={"latest": latest},
    )
    with pytest.raises(RuntimeError, match="manifest_error"):
        get_application_review_details(conn, run_id=run_id, artifact_root=root)
    root.close()


def test_review_details_invalid_observation_blocker_code_rejected(tmp_path: Path) -> None:
    conn = connect(tmp_path / "jobs.sqlite3")
    _initialize(conn, tmp_path)
    observation = _valid_observation(blocker_codes=["not_allowed"])
    root, run_id, _ = _review_details_run(
        conn,
        tmp_path,
        status="review_ready",
        reason_code="draft_ready",
        manifest_stage="finished",
        artifacts={
            "observation": observation,
            "plan": _valid_plan(),
            "actions": _valid_actions(),
        },
    )
    with pytest.raises(RuntimeError, match="manifest_error"):
        get_application_review_details(conn, run_id=run_id, artifact_root=root)
    root.close()


def test_review_details_manifest_observation_path_mismatch_rejected(tmp_path: Path) -> None:
    conn = connect(tmp_path / "jobs.sqlite3")
    _initialize(conn, tmp_path)
    root, run_id, _ = _review_details_run(
        conn,
        tmp_path,
        status="review_ready",
        reason_code="draft_ready",
        manifest_stage="finished",
        artifacts={
            "observation": _valid_observation(),
            "plan": _valid_plan(),
            "actions": _valid_actions(),
        },
    )
    run_dir = tmp_path / "artifacts" / f"run-{run_id}"
    manifest_path = run_dir / "run.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    # Point the observation descriptor at a producer iteration artifact path
    # that exists and hashes correctly; the show endpoint must still reject it.
    manifest["artifacts"]["observation"]["path"] = "iterations/0001/observation.json"
    with root.open_run_dir(run_id) as run:
        result = run.write_json("iterations/0001/observation.json", _valid_observation())
        manifest["artifacts"]["observation"]["sha256"] = result.sha256
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(RuntimeError, match="manifest_error"):
        get_application_review_details(conn, run_id=run_id, artifact_root=root)
    root.close()


def test_review_details_observation_blocker_codes_allowlisted(tmp_path: Path) -> None:
    conn = connect(tmp_path / "jobs.sqlite3")
    _initialize(conn, tmp_path)
    codes = ["captcha", "authentication_required", "assessment_required", "unsupported_frame", "page_validation_error", "observation_too_large"]
    root, run_id, _ = _review_details_run(
        conn,
        tmp_path,
        status="review_ready",
        reason_code="draft_ready",
        manifest_stage="finished",
        artifacts={
            "observation": _valid_observation(blocker_codes=codes),
            "plan": _valid_plan(),
            "actions": _valid_actions(),
        },
    )
    result = get_application_review_details(conn, run_id=run_id, artifact_root=root)
    assert result["observation"]["blocker_codes"] == codes
    root.close()
# ── RPC contract/ledger tests ─────────────────────────────────────

_RPC_NOW = 4_000_000_000_000
_RPC_URL = "https://boards.greenhouse.io/acme/jobs/456"


def _uuid(number: int) -> str:
    return f"00000000-0000-4000-8000-{number:012d}"


def _rpc_request(
    *,
    request_id: str,
    operation: str = "run.start",
    run_id: int | None = None,
    url: str = _RPC_URL,
    payload: dict[str, object] | None = None,
) -> ApplicationRpcRequest:
    if operation == "run.start":
        body: dict[str, object] = {
            "goal": "prepare_application_draft",
            "job_url": url,
            "candidate_profile_id": "candidate-main",
            "configured_resume_id": "resume-main",
            "headed": True,
        }
    elif operation == "browser.fill_field":
        body = {
            "observation_sha256": "a" * 64,
            "element_id": "field-1",
            "value": "safe-value",
            "confidence": 0.9,
            "reason": "configured answer",
        }
    else:
        body = {}
    if payload:
        body.update(payload)
    return parse_application_request(
        {
            "protocol_version": APPLICATION_RPC_PROTOCOL_VERSION,
            "request_id": request_id,
            "operation": operation,
            "deadline_unix_ms": _RPC_NOW + 30_000,
            "run_id": run_id,
            "payload": body,
        },
        now_unix_ms=_RPC_NOW,
    )


def _rpc_job(conn: sqlite3.Connection, *, url: str = _RPC_URL) -> int:
    conn.execute(
        """
        INSERT INTO jobs (
            source, source_job_id, canonical_url, title, company, discovered_at,
            raw_json, first_seen_at, last_seen_at
        ) VALUES ('fixture', ?, ?, 'Engineer', 'Acme', '2026-07-10T00:00:00+00:00', '{}',
                  '2026-07-10T00:00:00+00:00', '2026-07-10T00:00:00+00:00')
        """,
        (f"rpc-job-{url}", url),
    )
    conn.commit()
    return int(conn.execute("SELECT last_insert_rowid()").fetchone()[0])


def _new_rpc_claim(
    conn: sqlite3.Connection,
    tmp_path: Path,
    *,
    request_id: str = _uuid(1),
    url: str = _RPC_URL,
    initialize: bool = True,
) -> tuple[ApplicationRpcRequest, RpcClaimOutcome]:
    if initialize:
        _initialize(conn, tmp_path)
    _rpc_job(conn, url=url)
    request = _rpc_request(request_id=request_id, url=url)
    claim = claim_application_job_for_rpc(
        conn, owner="rpc-owner", request=request, coordinator_id="coord-1"
    )
    assert claim.outcome == "new" and claim.claim is not None
    return request, claim


def _failure_response(
    request: ApplicationRpcRequest,
    *,
    state: str = "failed",
    action_sequence: int = 0,
    event_sequence: int = 0,
    error: str = "internal_error",
) -> dict[str, object]:
    return build_application_response(
        request,
        ok=False,
        state=state,
        action_sequence=action_sequence,
        event_sequence=event_sequence,
        error=error,
    )


def _child_request(parent: ApplicationRpcRequest, run_id: int) -> ApplicationRpcRequest:
    host_id = "host-1"
    tool_id = "tool-1"
    operation = "browser.fill_field"
    child_id = str(uuid5(UUID(parent.request_id), f"{host_id}\0{tool_id}\0{operation}"))
    return _rpc_request(
        request_id=child_id,
        operation=operation,
        run_id=run_id,
    )


def test_rpc_schema_is_exact_and_rejects_invalid_domains(tmp_path: Path) -> None:
    conn = connect(tmp_path / "jobs.sqlite3")
    _initialize(conn, tmp_path)
    assert rpc_schema_fingerprint(conn)["tables"]["application_rpc_requests"]["xinfo"]
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            """
            INSERT INTO application_rpc_requests
                (request_id, protocol_version, operation, semantic_sha256, request_json, state, created_at)
            VALUES ('REQ', 1, 'run.start', ?, '{}', 'pending', 'now')
            """,
            ("a" * 64,),
        )


def test_rpc_claim_returns_atomic_frozen_claim_and_exact_snapshot(tmp_path: Path) -> None:
    conn = connect(tmp_path / "jobs.sqlite3")
    request, result = _new_rpc_claim(conn, tmp_path)
    assert result.claim is not None
    assert result.claim.run_id == result.run_id
    with pytest.raises(TypeError):
        result.claim.job["title"] = "mutated"  # type: ignore[index]
    conn.execute("UPDATE jobs SET title='changed' WHERE id=?", (result.claim.job["id"],))
    conn.commit()
    assert result.claim.job["title"] == "Engineer"
    row = conn.execute(
        "SELECT request_json, run_id, state FROM application_rpc_requests WHERE request_id=?",
        (request.request_id,),
    ).fetchone()
    assert row["run_id"] == result.run_id and row["state"] == "pending"
    assert json.loads(row["request_json"])["operation"] == "run.start"
    owner = conn.execute(
        """
        SELECT coordinator_pid, coordinator_pgid, coordinator_birth
        FROM application_rpc_runs WHERE run_id=?
        """,
        (result.run_id,),
    ).fetchone()
    assert owner is not None
    assert owner["coordinator_pid"] == os.getpid()
    assert owner["coordinator_pgid"] > 0
    assert isinstance(owner["coordinator_birth"], str) and owner["coordinator_birth"]


def test_rpc_claim_identity_unavailable_fails_closed_without_mutation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    conn = connect(tmp_path / "jobs.sqlite3")
    _initialize(conn, tmp_path)
    _rpc_job(conn)
    request = _rpc_request(request_id=_uuid(30))
    monkeypatch.setattr(db_module, "_identity_payload", lambda *args, **kwargs: None)
    result = claim_application_job_for_rpc(
        conn, owner="rpc-owner", request=request, coordinator_id="coord-1"
    )
    assert result.outcome == "unavailable"
    assert conn.execute("SELECT status FROM jobs").fetchone()["status"] == "queued"
    assert conn.execute("SELECT COUNT(*) FROM application_runs").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM application_rpc_runs").fetchone()[0] == 0
    assert conn.execute(
        "SELECT COUNT(*) FROM application_rpc_requests WHERE request_id=?",
        (request.request_id,),
    ).fetchone()[0] == 0


def test_ordinary_claim_waits_for_prior_process_absence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conn = connect(tmp_path / "jobs.sqlite3")
    _initialize(conn, tmp_path)
    job_id = _job(conn, url="https://boards.greenhouse.io/acme/jobs/claim-gate")
    first = claim_next_application_job(conn, owner="first-owner")
    assert first is not None
    process = {
        "owner": {"pid": 101, "pgid": 101, "birth": "owner-birth"},
        "browser": {"pid": 202, "pgid": 202, "birth": "browser-birth"},
    }
    conn.execute(
        """
        UPDATE application_runs
        SET status='failed', reason_code='browser_error', outcome='retry',
            reviewed_at=?, finished_at=?, owner_pid=?, browser_pid=?,
            observation_json=?
        WHERE id=?
        """,
        (
            db_module.utc_now(),
            db_module.utc_now(),
            101,
            202,
            json.dumps({"_process": process}),
            first.run_id,
        ),
    )
    conn.execute("UPDATE jobs SET status='queued' WHERE id=?", (job_id,))
    conn.commit()
    states = {101: "live", 202: "live"}
    monkeypatch.setattr(
        db_module,
        "_process_group_state",
        lambda pid, *, expected=None: states[pid],
    )
    assert claim_next_application_job(conn, owner="blocked") is None
    assert conn.execute("SELECT status FROM jobs WHERE id=?", (job_id,)).fetchone()[0] == "queued"
    states[202] = "unknown"
    assert claim_next_application_job(conn, owner="still-blocked") is None
    states[101] = "absent"
    states[202] = "absent"
    second = claim_next_application_job(conn, owner="after-close")
    assert second is not None
    assert second.run_id != first.run_id


def test_rpc_claim_waits_for_prior_omp_absence_not_coordinator_pid(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conn = connect(tmp_path / "jobs.sqlite3")
    request, first = _new_rpc_claim(conn, tmp_path, request_id=_uuid(61))
    assert first.run_id is not None
    now = db_module.utc_now()
    conn.execute(
        """
        UPDATE application_runs
        SET status='failed', reason_code='browser_error', outcome='retry',
            reviewed_at=?, finished_at=?
        WHERE id=?
        """,
        (now, now, first.run_id),
    )
    conn.execute(
        """
        UPDATE application_rpc_runs
        SET state='failed', omp_process_pid=301, omp_process_pgid=301,
            omp_process_birth='omp-birth', omp_session_sha256=?
        WHERE run_id=?
        """,
        ("a" * 64, first.run_id),
    )
    job_id = int(
        conn.execute("SELECT job_id FROM application_runs WHERE id=?", (first.run_id,)).fetchone()[0]
    )
    conn.execute("UPDATE jobs SET status='queued' WHERE id=?", (job_id,))
    conn.commit()
    states = {301: "live"}
    monkeypatch.setattr(
        db_module,
        "_process_group_state",
        lambda pid, *, expected=None: states[pid],
    )
    blocked_request = _rpc_request(request_id=_uuid(62), url=_RPC_URL)
    blocked = claim_application_job_for_rpc(
        conn,
        owner="rpc-owner-2",
        request=blocked_request,
        coordinator_id="coord-2",
    )
    assert blocked.outcome == "unavailable"
    assert conn.execute("SELECT status FROM jobs WHERE id=?", (job_id,)).fetchone()[0] == "queued"
    states[301] = "absent"
    reopened = claim_application_job_for_rpc(
        conn,
        owner="rpc-owner-2",
        request=blocked_request,
        coordinator_id="coord-2",
    )
    assert reopened.outcome == "new"


def test_rpc_bound_start_request_completes_with_assigned_run_id(tmp_path: Path) -> None:
    conn = connect(tmp_path / "jobs.sqlite3")
    request, claim = _new_rpc_claim(conn, tmp_path, request_id=_uuid(21))
    response = build_application_response(
        request,
        ok=True,
        state="starting",
        action_sequence=0,
        event_sequence=0,
        run_id=claim.run_id,
        result={
            "ats": "greenhouse",
            "job_url": _RPC_URL,
            "reason_code": None,
            "current_step": None,
            "coordinator_state": "starting",
            "browser_state": "starting",
            "last_observation_sha256": None,
            "artifact_manifest_sha256": None,
            "human_review_ready": False,
            "handoff_committed": False,
            "automated_submission": False,
        },
    )
    completed = complete_rpc_request(conn, request=request, response=response)
    assert completed.state == "completed" and completed.run_id == claim.run_id
    assert complete_rpc_request(conn, request=request, response=response).response_json == completed.response_json


def test_rpc_unavailable_start_is_pending_then_idempotent_failure(tmp_path: Path) -> None:
    conn = connect(tmp_path / "jobs.sqlite3")
    _initialize(conn, tmp_path)
    request = _rpc_request(request_id=_uuid(2))
    first = claim_application_job_for_rpc(
        conn, owner="rpc-owner", request=request, coordinator_id="coord-1"
    )
    assert first.outcome == "unavailable" and first.claim is None and first.run_id is None
    replay = claim_application_job_for_rpc(
        conn, owner="rpc-owner", request=request, coordinator_id="coord-1"
    )
    assert replay.outcome == "pending" and replay.claim is None
    response = _failure_response(request, error="run_not_found")
    info = complete_rpc_request(conn, request=request, response=response)
    assert info.state == "completed"
    assert complete_rpc_request(conn, request=request, response=response).response_json == info.response_json
    assert claim_application_job_for_rpc(
        conn, owner="rpc-owner", request=request, coordinator_id="coord-1"
    ).outcome == "completed"
    assert info.state == "completed" and info.run_id is None
    assert get_rpc_request(conn, request.request_id).response_json == info.response_json  # type: ignore[union-attr]
    assert claim_application_job_for_rpc(
        conn, owner="rpc-owner", request=request, coordinator_id="coord-1"
    ).outcome == "completed"

    changed = _rpc_request(request_id=request.request_id, payload={"candidate_profile_id": "other-candidate"})
    assert claim_application_job_for_rpc(
        conn, owner="rpc-owner", request=changed, coordinator_id="coord-1"
    ).outcome == "conflict"


def test_rpc_unknown_status_is_unbound_failure_and_replays(tmp_path: Path) -> None:
    conn = connect(tmp_path / "jobs.sqlite3")
    _initialize(conn, tmp_path)
    request = _rpc_request(request_id=_uuid(14), operation="run.status", run_id=99999)
    info = reserve_rpc_request(conn, request=request)
    assert info.run_id is None
    completed = complete_rpc_request(
        conn, request=request, response=_failure_response(request, error="run_not_found")
    )
    assert completed.run_id is None
    replay = reserve_rpc_request(conn, request=request)
    assert replay.state == "completed" and replay.response_json == completed.response_json
    changed = _rpc_request(request_id=request.request_id, operation="run.cancel", run_id=99999)
    with pytest.raises(RuntimeError, match="conflicting request"):
        reserve_rpc_request(conn, request=changed)


def test_rpc_conflicting_same_id_does_not_mutate(tmp_path: Path) -> None:
    conn = connect(tmp_path / "jobs.sqlite3")
    request, first = _new_rpc_claim(conn, tmp_path, request_id=_uuid(3))
    different = _rpc_request(
        request_id=request.request_id,
        payload={"candidate_profile_id": "other-candidate"},
    )
    before = tuple(conn.execute("SELECT status FROM jobs").fetchone())
    result = claim_application_job_for_rpc(
        conn, owner="rpc-owner", request=different, coordinator_id="coord-1"
    )
    assert result.outcome == "conflict" and result.claim is None
    assert tuple(conn.execute("SELECT status FROM jobs").fetchone()) == before
    assert first.run_id == result.run_id or result.run_id is None


def test_rpc_child_parent_reservation_and_deterministic_uuid(tmp_path: Path) -> None:
    conn = connect(tmp_path / "jobs.sqlite3")
    parent, claim = _new_rpc_claim(conn, tmp_path, request_id=_uuid(4))
    child = _child_request(parent, claim.run_id)
    info = reserve_rpc_request(
        conn, request=child, parent_request_id=parent.request_id
    )
    assert info.parent_request_id == parent.request_id
    assert info.run_id == claim.run_id
    assert info.request_json == json.dumps(child.to_mapping(), sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    assert reserve_rpc_request(
        conn, request=child, parent_request_id=parent.request_id
    ).request_id == child.request_id
    with pytest.raises(RuntimeError):
        reserve_rpc_request(conn, request=child, parent_request_id=_uuid(404))


def test_rpc_reservation_created_marker_has_one_cross_connection_winner(tmp_path: Path) -> None:
    db_path = tmp_path / "jobs.sqlite3"
    initializer = connect(db_path)
    _initialize(initializer, tmp_path)
    initializer.close()
    request = _rpc_request(request_id=_uuid(21))
    barrier = threading.Barrier(2)
    outcomes: list[RpcRequestInfo] = []
    errors: list[BaseException] = []

    def attempt() -> None:
        conn = connect(db_path)
        try:
            barrier.wait()
            outcomes.append(reserve_rpc_request(conn, request=request))
        except BaseException as exc:
            errors.append(exc)
        finally:
            conn.close()

    threads = [threading.Thread(target=attempt) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert errors == []
    assert sorted(info.created for info in outcomes) == [False, True]
    assert len({info.request_id for info in outcomes}) == 1
    check = sqlite3.connect(db_path)
    try:
        assert len(
            check.execute(
                "SELECT request_id FROM application_rpc_requests WHERE request_id=?", (request.request_id,)
            ).fetchall()
        ) == 1
    finally:
        check.close()


def test_rpc_completion_round_trips_and_rejects_mismatch_noncanonical_and_malformed(tmp_path: Path) -> None:
    conn = connect(tmp_path / "jobs.sqlite3")
    parent, claim = _new_rpc_claim(conn, tmp_path, request_id=_uuid(5))
    child = _child_request(parent, claim.run_id)
    reserve_rpc_request(conn, request=child, parent_request_id=parent.request_id)
    response = _failure_response(child)
    completed = complete_rpc_request(
        conn, request=child, response=response, parent_request_id=parent.request_id
    )
    assert json.loads(completed.response_json or "{}")["request_id"] == child.request_id
    assert complete_rpc_request(
        conn, request=child, response=response, parent_request_id=parent.request_id
    ).response_json == completed.response_json
    with pytest.raises(RuntimeError, match="conflicting response"):
        complete_rpc_request(
            conn, request=child, response=_failure_response(child, error="action_rejected"), parent_request_id=parent.request_id
        )
    with pytest.raises(ValueError, match="canonical"):
        complete_rpc_request(
            conn,
            request=child,
            response=json.dumps(response, indent=2),
            parent_request_id=parent.request_id,
        )
    with pytest.raises(Exception):
        complete_rpc_request(
            conn, request=child, response="{malformed", parent_request_id=parent.request_id
        )


def test_rpc_utf8_response_byte_cap_is_enforced(tmp_path: Path) -> None:
    conn = connect(tmp_path / "jobs.sqlite3")
    parent, claim = _new_rpc_claim(conn, tmp_path, request_id=_uuid(6))
    child = _child_request(parent, claim.run_id)
    reserve_rpc_request(conn, request=child, parent_request_id=parent.request_id)
    oversized = dict(_failure_response(child))
    oversized["error"] = {"code": "internal_error", "message": "é" * 300_000}
    with pytest.raises(Exception):
        complete_rpc_request(
            conn, request=child, response=oversized, parent_request_id=parent.request_id
        )
    assert get_rpc_request(conn, child.request_id).state == "pending"  # type: ignore[union-attr]


def test_rpc_state_handoff_and_terminal_mutation_gates(tmp_path: Path) -> None:
    conn = connect(tmp_path / "jobs.sqlite3")
    request, claim = _new_rpc_claim(conn, tmp_path, request_id=_uuid(7))
    assert update_rpc_run_state(
        conn, run_id=claim.run_id, coordinator_id="coord-1", state="running", action_sequence=1
    )
    assert not update_rpc_run_state(
        conn, run_id=claim.run_id, coordinator_id="coord-1", state="starting", action_sequence=2
    )
    assert update_rpc_run_state(
        conn,
        run_id=claim.run_id,
        coordinator_id="coord-1",
        state="review_ready",
        action_sequence=2,
        human_review_ready=True,
        handoff_committed=True,
    )
    assert not update_rpc_run_observation(
        conn,
        run_id=claim.run_id,
        coordinator_id="coord-1",
        observation_sha256="a" * 64,
        action_sequence=3,
    )
    assert not request_rpc_cancellation(conn, run_id=claim.run_id, coordinator_id="coord-1")
    assert not update_rpc_run_artifact_manifest(
        conn, run_id=claim.run_id, coordinator_id="coord-1", manifest_sha256="b" * 64, action_sequence=3
    )
    assert request.request_id == get_rpc_request(conn, request.request_id).request_id  # type: ignore[union-attr]


def test_rpc_manual_blocked_handoff_and_resume_matrix(tmp_path: Path) -> None:
    conn = connect(tmp_path / "jobs.sqlite3")
    _, manual_claim = _new_rpc_claim(conn, tmp_path, request_id=_uuid(15))
    assert update_rpc_run_state(
        conn, run_id=manual_claim.run_id, coordinator_id="coord-1", state="running", action_sequence=1
    )
    assert update_rpc_run_state(
        conn,
        run_id=manual_claim.run_id,
        coordinator_id="coord-1",
        state="manual",
        action_sequence=2,
        handoff_committed=False,
    )
    assert update_rpc_run_state(
        conn, run_id=manual_claim.run_id, coordinator_id="coord-1", state="running", action_sequence=3
    )
    assert update_rpc_run_state(
        conn,
        run_id=manual_claim.run_id,
        coordinator_id="coord-1",
        state="manual",
        action_sequence=4,
        handoff_committed=True,
    )
    assert not update_rpc_run_state(
        conn, run_id=manual_claim.run_id, coordinator_id="coord-1", state="running", action_sequence=5
    )
    _, blocked_claim = _new_rpc_claim(
        conn,
        tmp_path,
        request_id=_uuid(16),
        url="https://boards.greenhouse.io/acme/jobs/457",
        initialize=False,
    )
    assert update_rpc_run_state(
        conn, run_id=blocked_claim.run_id, coordinator_id="coord-1", state="running", action_sequence=1
    )
    assert update_rpc_run_state(
        conn, run_id=blocked_claim.run_id, coordinator_id="coord-1", state="blocked", action_sequence=2
    )
    assert update_rpc_run_state(
        conn, run_id=blocked_claim.run_id, coordinator_id="coord-1", state="running", action_sequence=3
    )
    assert update_rpc_run_state(
        conn,
        run_id=blocked_claim.run_id,
        coordinator_id="coord-1",
        state="blocked",
        action_sequence=4,
        handoff_committed=True,
    )
    assert not update_rpc_run_state(
        conn, run_id=blocked_claim.run_id, coordinator_id="coord-1", state="running", action_sequence=5
    )
    _, invalid_claim = _new_rpc_claim(
        conn,
        tmp_path,
        request_id=_uuid(17),
        url="https://boards.greenhouse.io/acme/jobs/458",
        initialize=False,
    )
    assert update_rpc_run_state(
        conn, run_id=invalid_claim.run_id, coordinator_id="coord-1", state="running", action_sequence=1
    )
    assert not update_rpc_run_state(
        conn,
        run_id=invalid_claim.run_id,
        coordinator_id="coord-1",
        state="running",
        action_sequence=2,
        human_review_ready=True,
    )
    assert not update_rpc_run_state(
        conn,
        run_id=invalid_claim.run_id,
        coordinator_id="coord-1",
        state="manual",
        action_sequence=2,
        human_review_ready=True,
    )


def test_rpc_process_identity_requires_live_exact_immutable_capture(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    conn = connect(tmp_path / "jobs.sqlite3")
    _, claim = _new_rpc_claim(conn, tmp_path, request_id=_uuid(8))
    monkeypatch.setattr(
        db_module,
        "_capture_process_identity",
        lambda pid: {"pid": pid, "pgid": pid, "birth": "birth-a"},
    )
    assert update_rpc_run_process(
        conn,
        run_id=claim.run_id,
        coordinator_id="coord-1",
        pid=12345,
        session_sha256="a" * 64,
    )
    assert update_rpc_run_process(
        conn,
        run_id=claim.run_id,
        coordinator_id="coord-1",
        pid=12345,
        session_sha256="a" * 64,
    )
    assert not update_rpc_run_process(
        conn,
        run_id=claim.run_id,
        coordinator_id="coord-1",
        pid=12346,
        session_sha256="a" * 64,
    )


def test_rpc_process_identity_provisional_session_upgrades_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    conn = connect(tmp_path / "jobs.sqlite3")
    _, claim = _new_rpc_claim(conn, tmp_path, request_id=_uuid(801))
    monkeypatch.setattr(
        db_module,
        "_capture_process_identity",
        lambda pid: {"pid": pid, "pgid": pid, "birth": "birth-provisional"},
    )
    provisional = db_module.rpc_provisional_session_sha256(
        12345, 12345, "birth-provisional"
    )
    assert db_module.mark_rpc_omp_spawn_attempted(
        conn, run_id=claim.run_id, coordinator_id="coord-1"
    )
    assert not update_rpc_run_process(
        conn,
        run_id=claim.run_id,
        coordinator_id="coord-1",
        pid=12345,
        session_sha256=provisional,
        process_identity={"pid": 12345, "pgid": 12345, "birth": "stale-birth"},
    )
    assert update_rpc_run_process(
        conn,
        run_id=claim.run_id,
        coordinator_id="coord-1",
        pid=12345,
        session_sha256=provisional,
        process_identity={"pid": 12345, "pgid": 12345, "birth": "birth-provisional"},
    )
    assert update_rpc_run_process(
        conn,
        run_id=claim.run_id,
        coordinator_id="coord-1",
        pid=12345,
        session_sha256="a" * 64,
        process_identity={"pid": 12345, "pgid": 12345, "birth": "birth-provisional"},
    )
    observation = json.loads(
        conn.execute(
            "SELECT observation_json FROM application_runs WHERE id=?",
            (claim.run_id,),
        ).fetchone()[0]
    )
    assert "_omp_spawn_attempted" not in observation
    assert not update_rpc_run_process(
        conn,
        run_id=claim.run_id,
        coordinator_id="coord-1",
        pid=12345,
        session_sha256="b" * 64,
    )
    assert not update_rpc_run_process(
        conn,
        run_id=claim.run_id,
        coordinator_id="coord-1",
        pid=12345,
        session_sha256=provisional,
    )


def test_rpc_omp_spawn_marker_blocks_startup_without_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    conn = connect(tmp_path / "jobs.sqlite3")
    _, claim = _new_rpc_claim(conn, tmp_path, request_id=_uuid(802))
    root = ArtifactRoot.open(tmp_path / "artifacts", cwd=tmp_path)
    assert db_module.mark_rpc_omp_spawn_attempted(
        conn, run_id=claim.run_id, coordinator_id="coord-1"
    )
    monkeypatch.setattr(db_module, "_coordinator_identity_state", lambda row: "absent")
    with pytest.raises(RuntimeError, match="reconciliation conflict"):
        initialize_database(conn, migration_artifact_root=root)
    observation = json.loads(
        conn.execute(
            "SELECT observation_json FROM application_runs WHERE id=?",
            (claim.run_id,),
        ).fetchone()[0]
    )
    assert observation["_omp_spawn_attempted"] is True
    root.close()




def test_abort_rpc_run_for_shutdown_cleans_exact_groups_before_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    conn = connect(tmp_path / "jobs.sqlite3")
    root, run_id, child, identities, _live, signals = _rpc_handoff_recovery_fixture(
        conn,
        tmp_path,
        monkeypatch,
        committed=False,
        review_state="prepared",
        omp_live=False,
    )
    current = db_module._capture_rpc_coordinator_identity()
    assert current is not None
    conn.execute(
        """
        UPDATE application_rpc_runs
        SET coordinator_pid=?, coordinator_pgid=?, coordinator_birth=?
        WHERE run_id=?
        """,
        (current["pid"], current["pgid"], current["birth"], run_id),
    )
    conn.commit()
    assert db_module.abort_rpc_run_for_shutdown(
        conn, run_id=run_id, coordinator_id="coord-1"
    )
    assert signals == [
        (identities["owner"]["pgid"], signal.SIGTERM),
        (identities["owner"]["pgid"], signal.SIGKILL),
        (identities["browser"]["pgid"], signal.SIGTERM),
        (identities["browser"]["pgid"], signal.SIGKILL),
    ]
    assert tuple(
        conn.execute(
            "SELECT state, handoff_committed FROM application_rpc_runs WHERE run_id=?",
            (run_id,),
        ).fetchone()
    ) == ("failed", 0)
    assert tuple(
        conn.execute(
            "SELECT status, reason_code, outcome FROM application_runs WHERE id=?",
            (run_id,),
        ).fetchone()
    ) == ("failed", "abandoned_running_attempt", "retry")
    assert conn.execute(
        "SELECT status FROM jobs WHERE id=(SELECT job_id FROM application_runs WHERE id=?)",
        (run_id,),
    ).fetchone()["status"] == "queued"
    assert get_rpc_request(conn, child.request_id).state == "completed"  # type: ignore[union-attr]
    root.close()
    conn.close()


def test_abort_rpc_run_for_shutdown_unknown_identity_keeps_claim(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    conn = connect(tmp_path / "jobs.sqlite3")
    root, run_id, _child, _identities, _live, signals = _rpc_handoff_recovery_fixture(
        conn,
        tmp_path,
        monkeypatch,
        committed=False,
        review_state="prepared",
        omp_live=False,
    )
    current = db_module._capture_rpc_coordinator_identity()
    assert current is not None
    conn.execute(
        """
        UPDATE application_rpc_runs
        SET coordinator_pid=?, coordinator_pgid=?, coordinator_birth=?
        WHERE run_id=?
        """,
        (current["pid"], current["pgid"], current["birth"], run_id),
    )
    conn.commit()
    monkeypatch.setattr(
        db_module,
        "_process_group_state",
        lambda pid, expected=None: "unknown"
        if expected is not None and expected.get("pid") == 61202
        else ("live" if expected is not None else "absent"),
    )
    assert not db_module.abort_rpc_run_for_shutdown(
        conn, run_id=run_id, coordinator_id="coord-1"
    )
    assert signals == []
    assert conn.execute(
        "SELECT state FROM application_rpc_runs WHERE run_id=?", (run_id,)
    ).fetchone()["state"] == "running"
    root.close()
    conn.close()


def test_rpc_events_require_run_provenance_and_ordered_low_entropy_codes(tmp_path: Path) -> None:
    conn = connect(tmp_path / "jobs.sqlite3")
    parent, claim = _new_rpc_claim(conn, tmp_path, request_id=_uuid(9))
    child = _child_request(parent, claim.run_id)
    reserve_rpc_request(conn, request=child, parent_request_id=parent.request_id)
    assert append_rpc_event(
        conn,
        run_id=claim.run_id,
        request_id=parent.request_id,
        event_type="run_started",
        summary_code="started",
    ) == 1
    assert append_rpc_event(
        conn,
        run_id=claim.run_id,
        request_id=child.request_id,
        event_type="action_allowed",
        summary_code="allowed",
    ) == 2
    with pytest.raises(ValueError):
        append_rpc_event(
            conn, run_id=claim.run_id, request_id=child.request_id,
            event_type="action_allowed", summary_code="model text",
        )
    with pytest.raises(RuntimeError, match="provenance mismatch"):
        append_rpc_event(
            conn, run_id=claim.run_id + 1, request_id=child.request_id,
            event_type="action_allowed", summary_code="allowed",
        )
    events = replay_rpc_events(conn, claim.run_id)
    assert [event.sequence for event in events] == [1, 2]
    assert latest_rpc_event(conn, claim.run_id).summary_code == "allowed"  # type: ignore[union-attr]


def test_rpc_run_transition_rolls_back_when_event_append_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    conn = connect(tmp_path / "jobs.sqlite3")
    parent, claim = _new_rpc_claim(conn, tmp_path, request_id=_uuid(31))
    before = tuple(
        conn.execute(
            """
            SELECT state, action_sequence, last_observation_sha256,
                   artifact_manifest_sha256, version
            FROM application_rpc_runs WHERE run_id=?
            """,
            (claim.run_id,),
        ).fetchone()
    )
    monkeypatch.setattr(
        db_module,
        "_append_rpc_event_locked",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("event append failed")),
    )
    with pytest.raises(RuntimeError, match="event append failed"):
        commit_rpc_run_transition(
            conn,
            RpcRunTransition(
                run_id=claim.run_id,
                coordinator_id="coord-1",
                request_id=parent.request_id,
                action_sequence=1,
                event_type="run_started",
                summary_code="started",
                state="running",
                observation_sha256="a" * 64,
                manifest_sha256="b" * 64,
            ),
        )
    after = tuple(
        conn.execute(
            """
            SELECT state, action_sequence, last_observation_sha256,
                   artifact_manifest_sha256, version
            FROM application_rpc_runs WHERE run_id=?
            """,
            (claim.run_id,),
        ).fetchone()
    )
    assert after == before
    assert replay_rpc_events(conn, claim.run_id) == []


def test_rpc_run_status_exposes_reason_and_terminal_hashes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    conn = connect(tmp_path / "jobs.sqlite3")
    _, claim = _new_rpc_claim(conn, tmp_path, request_id=_uuid(18))
    initial = get_rpc_run_status(conn, claim.run_id)
    assert initial is not None
    assert initial.reason_code is None
    assert initial.job_url == _RPC_URL
    assert initial.apply_url.startswith("gh_hash:") and initial.apply_url != initial.job_url
    assert initial.last_observation_sha256 is None
    assert initial.artifact_manifest_sha256 is None
    assert update_rpc_run_observation(
        conn,
        run_id=claim.run_id,
        coordinator_id="coord-1",
        observation_sha256="e" * 64,
        action_sequence=1,
    )
    assert update_rpc_run_artifact_manifest(
        conn,
        run_id=claim.run_id,
        coordinator_id="coord-1",
        manifest_sha256="f" * 64,
        action_sequence=2,
    )
    monkeypatch.setattr(db_module, "_process_group_state", lambda pid, expected=None: "absent")
    monkeypatch.setattr(db_module, "_capture_process_identity", lambda pid: None)
    assert reconcile_abandoned_rpc_runs(conn).status == "reconciled"
    terminal = get_rpc_run_status(conn, claim.run_id)
    assert terminal is not None
    assert terminal.state == "failed"
    assert terminal.reason_code == "abandoned_running_attempt"
    assert terminal.last_observation_sha256 == "e" * 64
    assert terminal.artifact_manifest_sha256 == "f" * 64


def test_rpc_reconciliation_status_job_url_uses_bound_start_request(tmp_path: Path) -> None:
    conn = connect(tmp_path / "jobs.sqlite3")
    _, claim = _new_rpc_claim(conn, tmp_path, request_id=_uuid(21))
    job_id = int(conn.execute("SELECT job_id FROM application_runs WHERE id=?", (claim.run_id,)).fetchone()[0])
    conn.execute(
        "UPDATE jobs SET canonical_url=? WHERE id=?",
        ("https://boards.greenhouse.io/acme/jobs/999", job_id),
    )
    conn.commit()
    status = get_rpc_run_status(conn, claim.run_id)
    assert status is not None
    assert status.job_url == _RPC_URL




def test_rpc_reconciliation_partitions_live_conflict_and_absent_runs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    conn = connect(tmp_path / "jobs.sqlite3")
    absent_request, absent_claim = _new_rpc_claim(conn, tmp_path, request_id=_uuid(19))
    _, live_claim = _new_rpc_claim(
        conn,
        tmp_path,
        request_id=_uuid(20),
        url="https://boards.greenhouse.io/acme/jobs/457",
        initialize=False,
    )
    monkeypatch.setattr(
        db_module,
        "_capture_process_identity",
        lambda pid: None if pid == 424241 else {"pid": pid, "pgid": pid, "birth": "live-birth"},
    )
    assert update_rpc_run_process(
        conn,
        run_id=live_claim.run_id,
        coordinator_id="coord-1",
        pid=424242,
        session_sha256="1" * 64,
    )
    conn.execute(
        """
        UPDATE application_rpc_runs
        SET coordinator_pid=424241, coordinator_pgid=424241, coordinator_birth='absent-owner'
        WHERE run_id=?
        """,
        (absent_claim.run_id,),
    )
    conn.commit()
    monkeypatch.setattr(
        db_module,
        "_process_group_state",
        lambda pid, expected=None: "live" if pid in {424242, os.getpid()} else "absent",
    )
    result = reconcile_abandoned_rpc_runs(conn)
    assert result.status == "partial"
    assert result.run_ids == (absent_claim.run_id,)
    assert result.conflict_run_ids == (live_claim.run_id,)
    assert get_rpc_run_status(conn, absent_claim.run_id).state == "failed"  # type: ignore[union-attr]
    assert get_rpc_run_status(conn, live_claim.run_id).state == "starting"  # type: ignore[union-attr]
    assert get_rpc_request(conn, absent_request.request_id).state == "completed"  # type: ignore[union-attr]
    assert get_rpc_request(conn, _uuid(20)).state == "pending"  # type: ignore[union-attr]


def test_rpc_claim_then_reconcile_second_connection_preserves_live_owner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = tmp_path / "jobs.sqlite3"
    first = connect(db_path)
    request, claim = _new_rpc_claim(first, tmp_path, request_id=_uuid(32))
    before_rpc = tuple(
        first.execute(
            "SELECT state, action_sequence, version FROM application_rpc_runs WHERE run_id=?",
            (claim.run_id,),
        ).fetchone()
    )
    second = connect(db_path)
    monkeypatch.setattr(
        db_module,
        "_process_group_state",
        lambda pid, expected=None: "live" if pid == os.getpid() else "absent",
    )
    result = reconcile_abandoned_rpc_runs(second)
    assert result.status == "conflict"
    assert result.conflict_run_ids == (claim.run_id,)
    assert tuple(
        first.execute(
            "SELECT state, action_sequence, version FROM application_rpc_runs WHERE run_id=?",
            (claim.run_id,),
        ).fetchone()
    ) == before_rpc
    assert first.execute("SELECT status FROM jobs").fetchone()["status"] == "in_progress"
    assert get_rpc_request(first, request.request_id).state == "pending"  # type: ignore[union-attr]
    assert replay_rpc_events(first, claim.run_id) == []
    second.close()
    first.close()


def test_rpc_reconcile_proceeds_only_after_coordinator_owner_absent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    conn = connect(tmp_path / "jobs.sqlite3")
    _, claim = _new_rpc_claim(conn, tmp_path, request_id=_uuid(33))
    conn.execute(
        """
        UPDATE application_rpc_runs
        SET coordinator_pid=424240, coordinator_pgid=424240, coordinator_birth='gone-owner'
        WHERE run_id=?
        """,
        (claim.run_id,),
    )
    conn.commit()
    monkeypatch.setattr(db_module, "_process_group_state", lambda pid, expected=None: "absent")
    result = reconcile_abandoned_rpc_runs(conn)
    assert result.status == "reconciled"
    assert result.run_ids == (claim.run_id,)
    assert get_rpc_run_status(conn, claim.run_id).state == "failed"  # type: ignore[union-attr]


def test_rpc_composite_proposal_commit_is_atomic_and_replay_safe(tmp_path: Path) -> None:
    conn = connect(tmp_path / "jobs.sqlite3")
    parent, claim = _new_rpc_claim(conn, tmp_path, request_id=_uuid(10))
    child = _child_request(parent, claim.run_id)
    reserve_rpc_request(conn, request=child, parent_request_id=parent.request_id)
    bad = _failure_response(child, state="running", action_sequence=1, event_sequence=2)
    with pytest.raises(RuntimeError, match="event_sequence"):
        commit_rpc_proposal_result(
            conn,
            request=child,
            response=bad,
            coordinator_id="coord-1",
            action_sequence=1,
            event_type="action_rejected",
            summary_code="rejected",
            run_state="running",
            parent_request_id=parent.request_id,
        )
    assert get_rpc_run_status(conn, claim.run_id).action_sequence == 0  # type: ignore[union-attr]
    assert replay_rpc_events(conn, claim.run_id) == []
    good = _failure_response(child, state="running", action_sequence=1, event_sequence=1)
    info = commit_rpc_proposal_result(
        conn,
        request=child,
        response=good,
        coordinator_id="coord-1",
        action_sequence=1,
        event_type="action_rejected",
        summary_code="rejected",
        observation_sha256="c" * 64,
        run_state="running",
        parent_request_id=parent.request_id,
    )
    assert info.state == "completed"
    assert get_rpc_run_status(conn, claim.run_id).action_sequence == 1  # type: ignore[union-attr]


def test_rpc_pending_failure_rolls_back_application_rpc_event_and_child(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conn = connect(tmp_path / "jobs.sqlite3")
    try:
        parent, claim = _new_rpc_claim(conn, tmp_path, request_id=_uuid(90))
        child = _child_request(parent, claim.run_id)
        reserve_rpc_request(conn, request=child, parent_request_id=parent.request_id)
        finalization = {
            "status": "failed",
            "reason_code": "browser_error",
            "observation_summary": {"evidence": "preserved"},
            "plan_summary": {},
            "artifact_dir": None,
        }
        monkeypatch.setattr(
            db_module,
            "_append_rpc_event_locked",
            lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("injected event failure")),
        )
        with pytest.raises(RuntimeError, match="injected event failure"):
            commit_rpc_proposal_failure(
                conn,
                request=child,
                response=_failure_response(
                    child,
                    action_sequence=1,
                    event_sequence=1,
                    error="workflow_failed",
                ),
                coordinator_id="coord-1",
                action_sequence=1,
                application_finalization=finalization,
                parent_request_id=parent.request_id,
            )
        app = conn.execute(
            "SELECT status, outcome FROM application_runs WHERE id=?",
            (claim.run_id,),
        ).fetchone()
        assert tuple(app) == ("running", None)
        assert get_rpc_run_status(conn, claim.run_id).state == "starting"  # type: ignore[union-attr]
        assert get_rpc_request(conn, child.request_id).state == "pending"  # type: ignore[union-attr]
        assert replay_rpc_events(conn, claim.run_id) == []
    finally:
        conn.close()


def test_rpc_no_pending_failure_rolls_back_application_rpc_and_event(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conn = connect(tmp_path / "jobs.sqlite3")
    try:
        parent, claim = _new_rpc_claim(conn, tmp_path, request_id=_uuid(91))
        finalization = {
            "status": "failed",
            "reason_code": "browser_error",
            "observation_summary": {"evidence": "preserved"},
            "plan_summary": {},
            "artifact_dir": None,
        }
        monkeypatch.setattr(
            db_module,
            "_append_rpc_event_locked",
            lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("injected event failure")),
        )
        with pytest.raises(RuntimeError, match="injected event failure"):
            commit_rpc_failure(
                conn,
                run_id=claim.run_id,
                coordinator_id="coord-1",
                request_id=parent.request_id,
                action_sequence=1,
                application_finalization=finalization,
            )
        app = conn.execute(
            """
            SELECT a.status, a.outcome, j.status AS job_status
            FROM application_runs AS a JOIN jobs AS j ON j.id=a.job_id
            WHERE a.id=?
            """,
            (claim.run_id,),
        ).fetchone()
        assert tuple(app) == ("running", None, "in_progress")
        assert get_rpc_run_status(conn, claim.run_id).state == "starting"  # type: ignore[union-attr]
        assert replay_rpc_events(conn, claim.run_id) == []
    finally:
        conn.close()


def test_rpc_pending_failure_ack_loss_recovers_existing_child_commit(
    tmp_path: Path,
) -> None:
    conn = connect(tmp_path / "jobs.sqlite3")
    try:
        parent, claim = _new_rpc_claim(conn, tmp_path, request_id=_uuid(93))
        child = _child_request(parent, claim.run_id)
        reserve_rpc_request(conn, request=child, parent_request_id=parent.request_id)
        finalization = {
            "status": "failed",
            "reason_code": "browser_error",
            "observation_summary": {"evidence": "preserved"},
            "plan_summary": {},
            "artifact_dir": None,
        }
        first = commit_rpc_proposal_failure(
            conn,
            request=child,
            response=_failure_response(
                child,
                action_sequence=1,
                event_sequence=1,
                error="workflow_failed",
            ),
            coordinator_id="coord-1",
            action_sequence=1,
            application_finalization=finalization,
            parent_request_id=parent.request_id,
        )
        second = commit_rpc_proposal_failure(
            conn,
            request=child,
            response=_failure_response(
                child,
                action_sequence=2,
                event_sequence=2,
                error="workflow_failed",
            ),
            coordinator_id="coord-1",
            action_sequence=2,
            application_finalization=finalization,
            parent_request_id=parent.request_id,
        )
        assert first.state == second.state == "completed"
        events = replay_rpc_events(conn, claim.run_id)
        assert len(events) == 1 and events[0].event_type == "run_failed"
        assert get_rpc_request(conn, child.request_id).response_json == second.response_json  # type: ignore[union-attr]
    finally:
        conn.close()


def test_rpc_no_pending_failure_ack_loss_recovers_existing_event(
    tmp_path: Path,
) -> None:
    conn = connect(tmp_path / "jobs.sqlite3")
    try:
        parent, claim = _new_rpc_claim(conn, tmp_path, request_id=_uuid(94))
        finalization = {
            "status": "failed",
            "reason_code": "browser_error",
            "observation_summary": {"evidence": "preserved"},
            "plan_summary": {},
            "artifact_dir": None,
        }
        first = commit_rpc_failure(
            conn,
            run_id=claim.run_id,
            coordinator_id="coord-1",
            request_id=parent.request_id,
            action_sequence=1,
            application_finalization=finalization,
        )
        second = commit_rpc_failure(
            conn,
            run_id=claim.run_id,
            coordinator_id="coord-1",
            request_id=parent.request_id,
            action_sequence=2,
            application_finalization=finalization,
        )
        assert first.sequence == second.sequence == 1
        events = replay_rpc_events(conn, claim.run_id)
        assert len(events) == 1 and events[0].event_type == "run_failed"
    finally:
        conn.close()


def test_rpc_unsafe_blocked_failure_is_rejected_without_split(tmp_path: Path) -> None:
    conn = connect(tmp_path / "jobs.sqlite3")
    try:
        parent, claim = _new_rpc_claim(conn, tmp_path, request_id=_uuid(92))
        with pytest.raises(ValueError):
            commit_rpc_failure(
                conn,
                run_id=claim.run_id,
                coordinator_id="coord-1",
                request_id=parent.request_id,
                action_sequence=1,
                application_finalization={
                    "status": "blocked",
                    "reason_code": "unsafe_navigation_target",
                    "observation_summary": {},
                    "plan_summary": {},
                    "artifact_dir": None,
                },
            )
        assert get_rpc_run_status(conn, claim.run_id).state == "starting"  # type: ignore[union-attr]
        assert replay_rpc_events(conn, claim.run_id) == []
        app = conn.execute(
            "SELECT status, outcome FROM application_runs WHERE id=?",
            (claim.run_id,),
        ).fetchone()
        assert tuple(app) == ("running", None)
    finally:
        conn.close()


def test_rpc_reconciliation_absent_is_idempotent_and_finalizes_application(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    conn = connect(tmp_path / "jobs.sqlite3")
    request, claim = _new_rpc_claim(conn, tmp_path, request_id=_uuid(11))
    monkeypatch.setattr(db_module, "_process_group_state", lambda pid, expected=None: "absent")
    monkeypatch.setattr(db_module, "_capture_process_identity", lambda pid: None)
    result = reconcile_abandoned_rpc_runs(conn)
    assert result.status == "reconciled" and result.run_ids == (claim.run_id,)
    rpc = get_rpc_run_status(conn, claim.run_id)
    app = conn.execute(
        "SELECT status, reason_code, outcome, reviewed_at FROM application_runs WHERE id=?",
        (claim.run_id,),
    ).fetchone()
    assert rpc.state == "failed"  # type: ignore[union-attr]
    assert tuple(app) == ("failed", "abandoned_running_attempt", "retry", app["reviewed_at"])
    stored = get_rpc_request(conn, request.request_id)
    assert stored.state == "completed"  # type: ignore[union-attr]
    assert replay_rpc_events(conn, claim.run_id)[0].event_type == "run_failed"
    assert reconcile_abandoned_rpc_runs(conn).status == "noop"


def test_rpc_reconciliation_absent_manual_and_blocked_closes_review_and_requeues(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    conn = connect(tmp_path / "jobs.sqlite3")
    manual_request, manual_claim = _new_rpc_claim(conn, tmp_path, request_id=_uuid(22))
    assert update_rpc_run_state(
        conn,
        run_id=manual_claim.run_id,
        coordinator_id="coord-1",
        state="running",
        action_sequence=1,
    )
    finish_application_run(
        conn,
        run_id=manual_claim.run_id,
        status="manual",
        reason_code="page_validation_error",
        observation_summary={"manual_evidence": "preserved"},
        plan_summary={"manual_step": "review"},
    )
    assert update_rpc_run_state(
        conn,
        run_id=manual_claim.run_id,
        coordinator_id="coord-1",
        state="manual",
        action_sequence=2,
    )
    manual_child = _child_request(manual_request, manual_claim.run_id)
    reserve_rpc_request(conn, request=manual_child, parent_request_id=manual_request.request_id)

    blocked_request, blocked_claim = _new_rpc_claim(
        conn,
        tmp_path,
        request_id=_uuid(23),
        url="https://boards.greenhouse.io/acme/jobs/457",
        initialize=False,
    )
    assert update_rpc_run_state(
        conn,
        run_id=blocked_claim.run_id,
        coordinator_id="coord-1",
        state="running",
        action_sequence=1,
    )
    finish_application_run(
        conn,
        run_id=blocked_claim.run_id,
        status="blocked",
        reason_code="ats_mismatch",
        observation_summary={"blocked_evidence": "preserved"},
        plan_summary={"blocked_step": "review"},
    )
    assert update_rpc_run_state(
        conn,
        run_id=blocked_claim.run_id,
        coordinator_id="coord-1",
        state="blocked",
        action_sequence=2,
    )
    blocked_child = _child_request(blocked_request, blocked_claim.run_id)
    reserve_rpc_request(conn, request=blocked_child, parent_request_id=blocked_request.request_id)

    before: dict[int, dict[str, object]] = {}
    for run_id in (manual_claim.run_id, blocked_claim.run_id):
        row = conn.execute(
            """
            SELECT status, reason_code, finished_at, observation_json, plan_json, artifact_dir
            FROM application_runs WHERE id=?
            """,
            (run_id,),
        ).fetchone()
        assert row is not None
        before[run_id] = dict(row)
    monkeypatch.setattr(db_module, "_process_group_state", lambda pid, expected=None: "absent")
    monkeypatch.setattr(db_module, "_capture_process_identity", lambda pid: None)
    result = reconcile_abandoned_rpc_runs(conn)
    assert result.status == "reconciled"
    assert result.run_ids == (manual_claim.run_id, blocked_claim.run_id)
    assert result.event_sequences == ((manual_claim.run_id, 1), (blocked_claim.run_id, 1))

    for run_id, request, child, expected_status, expected_reason in (
        (manual_claim.run_id, manual_request, manual_child, "manual", "page_validation_error"),
        (blocked_claim.run_id, blocked_request, blocked_child, "blocked", "ats_mismatch"),
    ):
        rpc = get_rpc_run_status(conn, run_id)
        assert rpc is not None and rpc.state == "failed"
        app = conn.execute(
            "SELECT * FROM application_runs WHERE id=?",
            (run_id,),
        ).fetchone()
        assert app is not None
        assert app["status"] == expected_status
        assert app["reason_code"] == expected_reason
        assert app["outcome"] == "retry" and app["reviewed_at"] is not None
        for field, value in before[run_id].items():
            assert app[field] == value
        job_status = conn.execute(
            "SELECT j.status FROM jobs j JOIN application_runs a ON a.job_id=j.id WHERE a.id=?",
            (run_id,),
        ).fetchone()
        assert job_status["status"] == "queued"  # type: ignore[index]
        for stored in (get_rpc_request(conn, request.request_id), get_rpc_request(conn, child.request_id)):
            assert stored is not None and stored.state == "completed"
            assert json.loads(stored.response_json or "{}")["state"] == "failed"
        events = replay_rpc_events(conn, run_id)
        assert len(events) == 1
        assert events[0].event_type == "run_failed"
        assert events[0].summary_code == "failed"
    assert reconcile_abandoned_rpc_runs(conn).status == "noop"


@pytest.mark.parametrize(
    ("terminal_status", "reason_code"),
    (
        ("manual", "page_validation_error"),
        ("blocked", "ats_mismatch"),
    ),
)
def test_rpc_cancellation_terminalizes_processless_review_state(
    tmp_path: Path,
    terminal_status: str,
    reason_code: str,
) -> None:
    conn = connect(tmp_path / f"{terminal_status}.sqlite3")
    try:
        request, claim = _new_rpc_claim(conn, tmp_path, request_id=_uuid(80 + len(terminal_status)))
        assert update_rpc_run_state(
            conn,
            run_id=claim.run_id,
            coordinator_id="coord-1",
            state="running",
            action_sequence=1,
        )
        finish_application_run(
            conn,
            run_id=claim.run_id,
            status=terminal_status,  # type: ignore[arg-type]
            reason_code=reason_code,
        )
        assert update_rpc_run_state(
            conn,
            run_id=claim.run_id,
            coordinator_id="coord-1",
            state=terminal_status,
            action_sequence=2,
        )
        assert request_rpc_cancellation(
            conn,
            run_id=claim.run_id,
            coordinator_id="coord-1",
        )
        app = conn.execute(
            """
            SELECT a.status, a.reason_code, a.outcome, j.status AS job_status
            FROM application_runs AS a
            JOIN jobs AS j ON j.id=a.job_id
            WHERE a.id=?
            """,
            (claim.run_id,),
        ).fetchone()
        assert tuple(app) == (
            "failed",
            "abandoned_running_attempt",
            "retry",
            "queued",
        )
        status = get_rpc_run_status(conn, claim.run_id)
        assert status is not None
        assert status.state == "failed" and status.cancellation_requested
        events = replay_rpc_events(conn, claim.run_id)
        assert events[-1].event_type == "run_failed"
        assert sum(event.event_type == "run_failed" for event in events) == 1
        assert not request_rpc_cancellation(
            conn,
            run_id=claim.run_id,
            coordinator_id="coord-1",
        )
    finally:
        conn.close()


def test_cancel_first_atomically_blocks_handoff_intent_binding(tmp_path: Path) -> None:
    conn = connect(tmp_path / "jobs.sqlite3")
    try:
        parent, claim = _new_rpc_claim(
            conn,
            tmp_path,
            request_id=_uuid(86),
        )
        child = _rpc_request(
            request_id=_uuid(87),
            operation="browser.prepare_human_handoff",
            run_id=claim.run_id,
            payload={"observation_sha256": "a" * 64},
        )
        reserve_rpc_request(
            conn,
            request=child,
            parent_request_id=parent.request_id,
        )
        assert request_rpc_cancellation(
            conn,
            run_id=claim.run_id,
            coordinator_id="coord-1",
        )
        before = conn.execute(
            "SELECT observation_json FROM application_runs WHERE id=?",
            (claim.run_id,),
        ).fetchone()["observation_json"]
        intent = {
            "application_finalization": {},
            "artifact_manifest_sha256": "b" * 64,
            "artifact_sha256": "c" * 64,
            "child_request_id": child.request_id,
            "commit_token_sha256": "d" * 64,
            "job_id": int(claim.claim.job["id"]),
            "observation_sha256": "a" * 64,
            "parent_request_id": parent.request_id,
            "session_id": "cancelled-session",
            "proposal_result": {},
        }

        with pytest.raises(RuntimeError, match="handoff intent provenance mismatch"):
            bind_rpc_handoff_intent(
                conn,
                request=child,
                coordinator_id="coord-1",
                intent=intent,
            )

        after = conn.execute(
            "SELECT observation_json FROM application_runs WHERE id=?",
            (claim.run_id,),
        ).fetchone()["observation_json"]
        assert after == before
        assert "_handoff_intent" not in json.loads(after)
        status = get_rpc_run_status(conn, claim.run_id)
        assert status is not None and status.cancellation_requested
        assert status.handoff_committed is False
    finally:
        conn.close()


def test_rpc_reconciliation_manual_blocked_handoff_live_unknown_stay_untouched(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    conn = connect(tmp_path / "jobs.sqlite3")

    live_request, live_claim = _new_rpc_claim(conn, tmp_path, request_id=_uuid(24))
    assert update_rpc_run_state(
        conn,
        run_id=live_claim.run_id,
        coordinator_id="coord-1",
        state="running",
        action_sequence=1,
    )
    finish_application_run(conn, run_id=live_claim.run_id, status="manual", reason_code="page_validation_error")
    conn.execute(
        """
        UPDATE application_rpc_runs
        SET omp_process_pid=91001, omp_process_pgid=91001,
            omp_process_birth='live-birth', omp_session_sha256=?
        WHERE run_id=?
        """,
        ("a" * 64, live_claim.run_id),
    )
    conn.commit()
    assert update_rpc_run_state(
        conn,
        run_id=live_claim.run_id,
        coordinator_id="coord-1",
        state="manual",
        action_sequence=2,
    )

    unknown_request, unknown_claim = _new_rpc_claim(
        conn,
        tmp_path,
        request_id=_uuid(25),
        url="https://boards.greenhouse.io/acme/jobs/457",
        initialize=False,
    )
    assert update_rpc_run_state(
        conn,
        run_id=unknown_claim.run_id,
        coordinator_id="coord-1",
        state="running",
        action_sequence=1,
    )
    finish_application_run(conn, run_id=unknown_claim.run_id, status="blocked", reason_code="ats_mismatch")
    conn.execute(
        """
        UPDATE application_rpc_runs
        SET omp_process_pid=91002, omp_process_pgid=91002,
            omp_process_birth='unknown-birth', omp_session_sha256=?
        WHERE run_id=?
        """,
        ("b" * 64, unknown_claim.run_id),
    )
    conn.commit()
    assert update_rpc_run_state(
        conn,
        run_id=unknown_claim.run_id,
        coordinator_id="coord-1",
        state="blocked",
        action_sequence=2,
    )

    handed_manual_request, handed_manual_claim = _new_rpc_claim(
        conn,
        tmp_path,
        request_id=_uuid(26),
        url="https://boards.greenhouse.io/acme/jobs/458",
        initialize=False,
    )
    assert update_rpc_run_state(
        conn,
        run_id=handed_manual_claim.run_id,
        coordinator_id="coord-1",
        state="running",
        action_sequence=1,
    )
    finish_application_run(conn, run_id=handed_manual_claim.run_id, status="manual", reason_code="page_validation_error")
    assert update_rpc_run_state(
        conn,
        run_id=handed_manual_claim.run_id,
        coordinator_id="coord-1",
        state="manual",
        action_sequence=2,
        handoff_committed=True,
    )

    handed_blocked_request, handed_blocked_claim = _new_rpc_claim(
        conn,
        tmp_path,
        request_id=_uuid(27),
        url="https://boards.greenhouse.io/acme/jobs/459",
        initialize=False,
    )
    assert update_rpc_run_state(
        conn,
        run_id=handed_blocked_claim.run_id,
        coordinator_id="coord-1",
        state="running",
        action_sequence=1,
    )
    finish_application_run(conn, run_id=handed_blocked_claim.run_id, status="blocked", reason_code="ats_mismatch")
    assert update_rpc_run_state(
        conn,
        run_id=handed_blocked_claim.run_id,
        coordinator_id="coord-1",
        state="blocked",
        action_sequence=2,
        handoff_committed=True,
    )

    monkeypatch.setattr(
        db_module,
        "_process_group_state",
        lambda pid, expected=None: "live" if pid == 91001 else "unknown",
    )
    protected = (live_claim, unknown_claim, handed_manual_claim, handed_blocked_claim)
    before_rpc = {
        claim.run_id: tuple(
            conn.execute(
                "SELECT state, handoff_committed, version FROM application_rpc_runs WHERE run_id=?",
                (claim.run_id,),
            ).fetchone()
        )
        for claim in protected
    }
    result = reconcile_abandoned_rpc_runs(conn)
    assert result.status == "conflict"
    assert result.conflict_run_ids == (live_claim.run_id, unknown_claim.run_id)
    for claim, request in (
        (live_claim, live_request),
        (unknown_claim, unknown_request),
        (handed_manual_claim, handed_manual_request),
        (handed_blocked_claim, handed_blocked_request),
    ):
        after_rpc = tuple(
            conn.execute(
                "SELECT state, handoff_committed, version FROM application_rpc_runs WHERE run_id=?",
                (claim.run_id,),
            ).fetchone()
        )
        assert after_rpc == before_rpc[claim.run_id]
        app = conn.execute(
            "SELECT status, outcome, reviewed_at FROM application_runs WHERE id=?",
            (claim.run_id,),
        ).fetchone()
        assert app["outcome"] is None and app["reviewed_at"] is None  # type: ignore[index]
        job = conn.execute(
            "SELECT j.status FROM jobs j JOIN application_runs a ON a.job_id=j.id WHERE a.id=?",
            (claim.run_id,),
        ).fetchone()
        assert job["status"] == "in_progress"  # type: ignore[index]
        assert get_rpc_request(conn, request.request_id).state == "pending"  # type: ignore[union-attr]
        assert replay_rpc_events(conn, claim.run_id) == []


def test_rpc_reconciliation_completes_null_run_pending_start(tmp_path: Path) -> None:
    conn = connect(tmp_path / "jobs.sqlite3")
    _initialize(conn, tmp_path)
    request = _rpc_request(request_id=_uuid(12))
    claim = claim_application_job_for_rpc(
        conn, owner="rpc-owner", request=request, coordinator_id="coord-1"
    )
    assert claim.outcome == "unavailable"
    result = reconcile_abandoned_rpc_runs(conn)
    assert result.status == "reconciled" and result.run_ids == ()
    stored = get_rpc_request(conn, request.request_id)
    assert stored.state == "completed" and json.loads(stored.response_json)["error"]["code"] == "internal_error"  # type: ignore[union-attr]


def test_rpc_reconciliation_live_or_unknown_identity_is_fixed_conflict(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    conn = connect(tmp_path / "jobs.sqlite3")
    _, claim = _new_rpc_claim(conn, tmp_path, request_id=_uuid(13))
    conn.execute(
        """
        UPDATE application_rpc_runs
        SET omp_process_pid=12345, omp_process_pgid=12345,
            omp_process_birth='birth-a', omp_session_sha256=?
        WHERE run_id=?
        """,
        ("a" * 64, claim.run_id),
    )
    conn.commit()
    before = tuple(conn.execute("SELECT state, version FROM application_rpc_runs WHERE run_id=?", (claim.run_id,)).fetchone())
    monkeypatch.setattr(db_module, "_process_group_state", lambda pid, expected=None: "live")
    conflict = reconcile_abandoned_rpc_runs(conn)
    assert conflict.status == "conflict"
    after = tuple(conn.execute("SELECT state, version FROM application_rpc_runs WHERE run_id=?", (claim.run_id,)).fetchone())
    assert after == before
    monkeypatch.setattr(db_module, "_process_group_state", lambda pid, expected=None: "unknown")
    assert reconcile_abandoned_rpc_runs(conn).status == "conflict"


def test_rpc_precommit_closed_cleanup_requeues_only_after_generic_reconcile(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    conn = connect(tmp_path / "jobs.sqlite3")
    parent, claim = _new_rpc_claim(conn, tmp_path, request_id=_uuid(40))
    observation_sha256 = "a" * 64
    child = _rpc_request(
        request_id=_uuid(41),
        operation="browser.prepare_human_handoff",
        run_id=claim.run_id,
        payload={"observation_sha256": observation_sha256},
    )
    reserve_rpc_request(conn, request=child, parent_request_id=parent.request_id)
    owner = {"pid": 12460, "pgid": 12460, "birth": "fixture-process"}
    browser = {"pid": 23470, "pgid": 23470, "birth": "fixture-process"}
    omp = {"pid": 34580, "pgid": 34580, "birth": "fixture-process"}
    original_capture = db_module._capture_process_identity

    def capture(pid: int):
        if pid in {owner["pid"], browser["pid"], omp["pid"]}:
            return {"pid": pid, "pgid": pid, "birth": "fixture-process"}
        return original_capture(pid)

    monkeypatch.setattr(db_module, "_capture_process_identity", capture)
    assert update_rpc_run_process(
        conn,
        run_id=claim.run_id,
        coordinator_id="coord-1",
        pid=omp["pid"],
        session_sha256="b" * 64,
    )
    assert register_application_artifact(conn, run_id=claim.run_id, artifact_dir=f"run-{claim.run_id}")
    assert register_application_session(conn, run_id=claim.run_id, session_id="precommit-session", session_state="open")
    assert mark_application_spawn_attempted(conn, run_id=claim.run_id, session_id="precommit-session")
    assert register_application_owner_process(
        conn, run_id=claim.run_id, owner_pid=owner["pid"], process_identity=owner
    )
    assert register_application_browser_process(
        conn, run_id=claim.run_id, browser_pid=browser["pid"], process_identity=browser
    )
    token = "c" * 64
    finalization = {
        "artifact_dir": f"run-{claim.run_id}",
        "observation_summary": {},
        "plan_summary": {},
        "reason_code": "draft_ready",
        "status": "review_ready",
    }
    proposal_result = {
        "outcome": "committed",
        "reason_code": "draft_ready",
        "observation_sha256": observation_sha256,
        "unresolved_required_count": 0,
        "automated_submission": False,
    }
    root = ArtifactRoot.open(tmp_path / "artifacts", cwd=tmp_path)
    with root.create_run_dir(claim.run_id) as run:
        run.write_json(
            "run.json",
            {
                "run_id": claim.run_id,
                "job_id": int(claim.claim.job["id"]),  # type: ignore[union-attr]
                "ats_policy": "greenhouse",
                "commit_token_sha256": token,
            },
        )
        run_raw = run.read_bytes("run.json")
        run.write_json(
            "review_session.json",
            {
                "version": 1,
                "run_id": claim.run_id,
                "job_id": int(claim.claim.job["id"]),  # type: ignore[union-attr]
                "session_id": "precommit-session",
                "owner_pid": owner["pid"],
                "owner_pgid": owner["pgid"],
                "owner_birth": owner["birth"],
                "owner_identity": owner,
                "browser_pid": browser["pid"],
                "browser_pgid": browser["pgid"],
                "browser_birth": browser["birth"],
                "browser_identity": browser,
                "state": "closed",
                "cleanup": True,
                "cleanup_trigger": "stdin_eof",
                "terminal_reason": "page_not_stable",
                "commit_token_sha256": None,
            },
        )
    intent = {
        "application_finalization": finalization,
        "artifact_manifest_sha256": hashlib.sha256(run_raw).hexdigest(),
        "artifact_sha256": "d" * 64,
        "child_request_id": child.request_id,
        "commit_token_sha256": token,
        "job_id": int(claim.claim.job["id"]),  # type: ignore[union-attr]
        "observation_sha256": observation_sha256,
        "parent_request_id": parent.request_id,
        "session_id": "precommit-session",
        "proposal_result": proposal_result,
    }
    conn.execute(
        "UPDATE application_rpc_runs SET ats_policy='greenhouse' WHERE run_id=?",
        (claim.run_id,),
    )
    conn.commit()
    bound = bind_rpc_handoff_intent(
        conn, request=child, coordinator_id="coord-1", intent=intent
    )
    assert bound["expected_rpc_version"] >= 1
    conn.execute(
        """
        UPDATE application_rpc_runs
        SET coordinator_pid=987654, coordinator_pgid=987654,
            coordinator_birth='gone-coordinator'
        WHERE run_id=?
        """,
        (claim.run_id,),
    )
    conn.commit()
    monkeypatch.setattr(db_module, "_capture_process_identity", lambda pid: None)
    monkeypatch.setattr(db_module, "_process_group_state", lambda pid, expected=None: "absent")
    recovered = recover_rpc_handoffs(conn, artifact_root=root)
    assert recovered.status == "recovered"
    assert recovered.run_ids == (claim.run_id,)
    assert get_rpc_request(conn, child.request_id).state == "pending"  # type: ignore[union-attr]
    reconciled = reconcile_abandoned_rpc_runs(conn)
    assert reconciled.status == "reconciled"
    app_row = conn.execute(
        "SELECT status, reason_code, outcome FROM application_runs WHERE id=?", (claim.run_id,)
    ).fetchone()
    assert tuple(app_row) == ("failed", "abandoned_running_attempt", "retry")
    assert conn.execute("SELECT status FROM jobs WHERE id=?", (int(claim.claim.job["id"]),)).fetchone()["status"] == "queued"  # type: ignore[union-attr]
    child_after = get_rpc_request(conn, child.request_id)
    assert child_after.state == "completed"  # type: ignore[union-attr]
    response_json = child_after.response_json  # type: ignore[union-attr]
    assert reconcile_abandoned_rpc_runs(conn).status == "noop"
    assert get_rpc_request(conn, child.request_id).response_json == response_json  # type: ignore[union-attr]
    root.close()
    conn.close()


def _rpc_handoff_recovery_fixture(
    conn: sqlite3.Connection,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    committed: bool,
    review_state: str,
    detached: bool | None = True,
    owner_live: bool = True,
    browser_live: bool = True,
    omp_live: bool = False,
    include_review: bool = True,
) -> tuple[ArtifactRoot, int, ApplicationRpcRequest, dict[str, dict[str, object]], dict[str, bool], list[tuple[int, signal.Signals]]]:
    parent, claim = _new_rpc_claim(
        conn, tmp_path, request_id=_uuid(80)
    )
    observation_sha256 = "a" * 64
    child = _rpc_request(
        request_id=_uuid(81),
        operation="browser.prepare_human_handoff",
        run_id=claim.run_id,
        payload={"observation_sha256": observation_sha256},
    )
    reserve_rpc_request(conn, request=child, parent_request_id=parent.request_id)
    identities: dict[str, dict[str, object]] = {
        "owner": {"pid": 61101, "pgid": 61101, "birth": "owner-birth"},
        "browser": {"pid": 61202, "pgid": 61202, "birth": "browser-birth"},
        "omp": {"pid": 61303, "pgid": 61303, "birth": "omp-birth"},
    }
    live = {"owner": True, "browser": True, "omp": True}
    signals: list[tuple[int, signal.Signals]] = []

    def capture(pid: int) -> dict[str, object] | None:
        for kind, identity in identities.items():
            if identity["pid"] == pid:
                return dict(identity) if live[kind] else None
        return None

    def process_state(pid: int, *, expected: dict[str, object] | None = None) -> str:
        for kind, identity in identities.items():
            if identity["pid"] == pid:
                if not live[kind]:
                    return "absent"
                if expected is not None and dict(expected) != dict(identity):
                    return "unknown"
                return "live"
        return "absent"

    def killpg(pgid: int, signum: signal.Signals) -> None:
        signals.append((pgid, signum))
        if signum == signal.SIGKILL:
            for kind, identity in identities.items():
                if identity["pgid"] == pgid:
                    live[kind] = False

    monkeypatch.setattr(db_module, "_capture_process_identity", capture)
    monkeypatch.setattr(db_module, "_RPC_HANDOFF_TERM_TIMEOUT_SECONDS", 0.0)
    monkeypatch.setattr(db_module, "_RPC_HANDOFF_KILL_TIMEOUT_SECONDS", 0.0)
    monkeypatch.setattr(db_module, "_RPC_HANDOFF_PROBE_INTERVAL_SECONDS", 0.0)
    monkeypatch.setattr(db_module.os, "killpg", killpg)
    monkeypatch.setattr(db_module, "_process_group_state", process_state)
    assert update_rpc_run_process(
        conn,
        run_id=claim.run_id,
        coordinator_id="coord-1",
        pid=int(identities["omp"]["pid"]),
        session_sha256="b" * 64,
    )
    assert register_application_artifact(
        conn, run_id=claim.run_id, artifact_dir=f"run-{claim.run_id}"
    )
    assert register_application_session(
        conn,
        run_id=claim.run_id,
        session_id="handoff-session",
        session_state="open" if committed else "prepared",
    )
    assert mark_application_spawn_attempted(
        conn, run_id=claim.run_id, session_id="handoff-session"
    )
    assert register_application_owner_process(
        conn,
        run_id=claim.run_id,
        owner_pid=int(identities["owner"]["pid"]),
        process_identity=identities["owner"],  # type: ignore[arg-type]
    )
    assert register_application_browser_process(
        conn,
        run_id=claim.run_id,
        browser_pid=int(identities["browser"]["pid"]),
        process_identity=identities["browser"],  # type: ignore[arg-type]
    )
    token = "c" * 64
    job_id = int(claim.claim.job["id"])  # type: ignore[union-attr]
    finalization = {
        "artifact_dir": f"run-{claim.run_id}",
        "observation_summary": {},
        "plan_summary": {},
        "reason_code": "draft_ready",
        "status": "review_ready",
    }
    proposal_result = {
        "outcome": "committed",
        "reason_code": "draft_ready",
        "observation_sha256": observation_sha256,
        "unresolved_required_count": 0,
        "automated_submission": False,
    }
    finalization_artifact = {
        "version": 1,
        "run_id": claim.run_id,
        "job_id": job_id,
        "operation": "browser.prepare_human_handoff",
        "session_id": "handoff-session",
        "child_request_id": child.request_id,
        "parent_request_id": parent.request_id,
        "commit_token_sha256": token,
        "observation_sha256": observation_sha256,
        "automated_submission": False,
        "status": "review_ready",
        "reason_code": "draft_ready",
        "unresolved_required_count": 0,
    }
    root = ArtifactRoot.open(tmp_path / "artifacts", cwd=tmp_path)
    with root.create_run_dir(claim.run_id) as run:
        run.write_json("handoff_finalization.json", finalization_artifact)
        finalization_raw = run.read_bytes("handoff_finalization.json")
        finalization_sha = hashlib.sha256(finalization_raw).hexdigest()
        run.write_json(
            "run.json",
            {
                "run_id": claim.run_id,
                "job_id": job_id,
                "ats_policy": "greenhouse",
                "commit_token_sha256": token,
                "artifacts": {
                    "handoff_finalization": {
                        "path": "handoff_finalization.json",
                        "sha256": finalization_sha,
                        "iteration": 0,
                        "stage": "finished",
                    }
                },
            },
        )
        run_raw = run.read_bytes("run.json")
        if include_review:
            review: dict[str, object] = {
                "version": 1,
                "ats_policy": "greenhouse",
                "run_id": claim.run_id,
                "job_id": job_id,
                "session_id": "handoff-session",
                "owner_pid": identities["owner"]["pid"],
                "owner_pgid": identities["owner"]["pgid"],
                "owner_birth": identities["owner"]["birth"],
                "owner_identity": identities["owner"],
                "browser_pid": identities["browser"]["pid"],
                "browser_pgid": identities["browser"]["pgid"],
                "browser_birth": identities["browser"]["birth"],
                "browser_identity": identities["browser"],
                "state": review_state,
                "spawn_attempted": True,
                "commit_token_sha256": token if (committed or review_state == "open_guarded") else None,
            }
            if detached is not None:
                review["detached"] = detached
            if review_state == "closed":
                review.update(
                    {
                        "cleanup": True,
                        "cleanup_trigger": "stdin_eof",
                        "terminal_reason": "page_not_stable",
                    }
                )
            run.write_json("review_session.json", review)
    intent = {
        "application_finalization": finalization,
        "artifact_manifest_sha256": hashlib.sha256(run_raw).hexdigest(),
        "artifact_sha256": finalization_sha,
        "child_request_id": child.request_id,
        "commit_token_sha256": token,
        "job_id": job_id,
        "observation_sha256": observation_sha256,
        "parent_request_id": parent.request_id,
        "session_id": "handoff-session",
        "proposal_result": proposal_result,
    }
    conn.execute(
        "UPDATE application_rpc_runs SET ats_policy='greenhouse' WHERE run_id=?",
        (claim.run_id,),
    )
    conn.commit()
    bind_rpc_handoff_intent(
        conn, request=child, coordinator_id="coord-1", intent=intent
    )
    if committed:
        conn.execute(
            """
            UPDATE application_runs
            SET status='review_ready', reason_code='draft_ready',
                finished_at=COALESCE(finished_at, ?)
            WHERE id=?
            """,
            (db_module.utc_now(), claim.run_id),
        )
        conn.execute(
            """
            UPDATE application_rpc_requests
            SET state='completed', response_json='{}', completed_at=?
            WHERE request_id=?
            """,
            (db_module.utc_now(), child.request_id),
        )
        conn.execute(
            """
            UPDATE application_rpc_runs
            SET state='review_ready', human_review_ready=1,
                handoff_committed=1
            WHERE run_id=?
            """,
            (claim.run_id,),
        )
    else:
        conn.execute(
            "UPDATE application_rpc_runs SET state='running' WHERE run_id=?",
            (claim.run_id,),
        )
    conn.execute(
        """
        UPDATE application_rpc_runs
        SET coordinator_pid=987654, coordinator_pgid=987654,
            coordinator_birth='gone-coordinator'
        WHERE run_id=?
        """,
        (claim.run_id,),
    )
    conn.commit()
    live.update(
        owner=owner_live,
        browser=browser_live,
        omp=omp_live,
    )
    return root, claim.run_id, child, identities, live, signals


def test_prepared_live_handoff_cleanup_requeues_via_generic_reconcile(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    conn = connect(tmp_path / "jobs.sqlite3")
    root, run_id, child, identities, live, signals = _rpc_handoff_recovery_fixture(
        conn,
        tmp_path,
        monkeypatch,
        committed=False,
        review_state="prepared",
    )
    result = recover_rpc_handoffs(conn, artifact_root=root)
    assert result.status == "recovered" and result.run_ids == (run_id,)
    assert signals == [
        (identities["owner"]["pgid"], signal.SIGTERM),
        (identities["owner"]["pgid"], signal.SIGKILL),
        (identities["browser"]["pgid"], signal.SIGTERM),
        (identities["browser"]["pgid"], signal.SIGKILL),
    ]
    observation = json.loads(
        conn.execute(
            "SELECT observation_json FROM application_runs WHERE id=?", (run_id,)
        ).fetchone()[0]
    )
    assert "_handoff_intent" not in observation
    assert "_handoff_precommit_intent" in observation
    assert reconcile_abandoned_rpc_runs(conn).status == "reconciled"
    app_row = conn.execute(
        "SELECT status, outcome FROM application_runs WHERE id=?", (run_id,)
    ).fetchone()
    assert tuple(app_row) == ("failed", "retry")
    assert get_rpc_request(conn, child.request_id).state == "completed"  # type: ignore[union-attr]
    root.close()
    conn.close()


def test_initialize_requeues_precommit_after_live_recovery_rebind(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    conn = connect(tmp_path / "jobs.sqlite3")
    root, run_id, child, identities, _live, signals = _rpc_handoff_recovery_fixture(
        conn,
        tmp_path,
        monkeypatch,
        committed=False,
        review_state="prepared",
    )
    root.close()
    current_identity = db_module._capture_rpc_coordinator_identity()
    assert current_identity is not None
    original_capture = db_module._capture_process_identity
    original_process_state = db_module._process_group_state

    def capture_with_coordinator(pid: int) -> dict[str, object] | None:
        if pid == current_identity["pid"]:
            return dict(current_identity)
        return original_capture(pid)

    def process_state_with_coordinator(
        pid: int, *, expected: dict[str, object] | None = None
    ) -> str:
        if pid == current_identity["pid"]:
            return (
                "live"
                if expected is None or dict(expected) == dict(current_identity)
                else "unknown"
            )
        return original_process_state(pid, expected=expected)

    monkeypatch.setattr(
        db_module, "_capture_process_identity", capture_with_coordinator
    )
    monkeypatch.setattr(
        db_module, "_process_group_state", process_state_with_coordinator
    )
    monkeypatch.setattr(
        db_module,
        "_capture_rpc_coordinator_identity",
        lambda: dict(current_identity),
    )
    with ArtifactRoot.open(tmp_path / "artifacts", cwd=tmp_path) as recovery_root:
        initialize_database(
            conn,
            migration_artifact_root=recovery_root,
            expected_coordinator_id="coord-1",
        )
    assert signals == [
        (identities["owner"]["pgid"], signal.SIGTERM),
        (identities["owner"]["pgid"], signal.SIGKILL),
        (identities["browser"]["pgid"], signal.SIGTERM),
        (identities["browser"]["pgid"], signal.SIGKILL),
    ]
    assert tuple(
        conn.execute(
            "SELECT status, outcome FROM application_runs WHERE id=?", (run_id,)
        ).fetchone()
    ) == ("failed", "retry")
    assert tuple(
        conn.execute(
            "SELECT state, handoff_committed FROM application_rpc_runs WHERE run_id=?",
            (run_id,),
        ).fetchone()
    ) == ("failed", 0)
    assert get_rpc_request(conn, child.request_id).state == "completed"  # type: ignore[union-attr]
    conn.close()


def test_precommit_recovery_marker_survives_coordinator_crash_gap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    conn = connect(tmp_path / "jobs.sqlite3")
    root, run_id, child, _identities, _live, _signals = _rpc_handoff_recovery_fixture(
        conn,
        tmp_path,
        monkeypatch,
        committed=False,
        review_state="prepared",
    )
    root.close()
    current_identity = db_module._capture_rpc_coordinator_identity()
    assert current_identity is not None
    original_capture = db_module._capture_process_identity
    original_process_state = db_module._process_group_state

    def capture_with_coordinator(pid: int) -> dict[str, object] | None:
        if pid == current_identity["pid"]:
            return dict(current_identity)
        return original_capture(pid)

    def process_state_with_coordinator(
        pid: int, *, expected: dict[str, object] | None = None
    ) -> str:
        if pid == current_identity["pid"]:
            return (
                "live"
                if expected is None or dict(expected) == dict(current_identity)
                else "unknown"
            )
        return original_process_state(pid, expected=expected)

    monkeypatch.setattr(
        db_module, "_capture_process_identity", capture_with_coordinator
    )
    monkeypatch.setattr(
        db_module, "_process_group_state", process_state_with_coordinator
    )
    monkeypatch.setattr(
        db_module,
        "_capture_rpc_coordinator_identity",
        lambda: dict(current_identity),
    )
    with ArtifactRoot.open(tmp_path / "artifacts", cwd=tmp_path) as recovery_root:
        result = recover_rpc_handoffs(
            conn,
            artifact_root=recovery_root,
            expected_coordinator_id="coord-1",
        )
    assert result.status == "recovered" and result.run_ids == (run_id,)
    marker = json.loads(
        conn.execute(
            "SELECT observation_json FROM application_runs WHERE id=?",
            (run_id,),
        ).fetchone()[0]
    )["_handoff_precommit_recovery"]
    assert marker["coordinator_pid"] == current_identity["pid"]

    # The next startup sees the exact recorded coordinator as absent.
    def former_owner_absent(pid: int) -> dict[str, object] | None:
        if pid == current_identity["pid"]:
            return None
        return original_capture(pid)

    def process_state_after_crash(
        pid: int, *, expected: dict[str, object] | None = None
    ) -> str:
        if pid == current_identity["pid"]:
            return "absent"
        return original_process_state(pid, expected=expected)

    monkeypatch.setattr(db_module, "_capture_process_identity", former_owner_absent)
    monkeypatch.setattr(
        db_module, "_process_group_state", process_state_after_crash
    )
    former_row = conn.execute(
        "SELECT * FROM application_rpc_runs WHERE run_id=?", (run_id,)
    ).fetchone()
    assert db_module._coordinator_identity_state(former_row) == "absent"
    result = reconcile_abandoned_rpc_runs(
        conn, expected_coordinator_id="coord-1"
    )
    assert result.status == "reconciled"
    assert result.run_ids == (run_id,)
    assert tuple(
        conn.execute(
            "SELECT status, outcome FROM application_runs WHERE id=?", (run_id,)
        ).fetchone()
    ) == ("failed", "retry")
    assert get_rpc_request(conn, child.request_id).state == "completed"  # type: ignore[union-attr]
    conn.close()


@pytest.mark.parametrize("subordinate", ("omp_live", "malformed_browser"))
def test_precommit_recovery_marker_does_not_override_subordinate_conflict(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    subordinate: str,
) -> None:
    conn = connect(tmp_path / "jobs.sqlite3")
    root, run_id, _child, _identities, live, _signals = _rpc_handoff_recovery_fixture(
        conn,
        tmp_path,
        monkeypatch,
        committed=False,
        review_state="prepared",
    )
    root.close()
    current_identity = db_module._capture_rpc_coordinator_identity()
    assert current_identity is not None
    original_capture = db_module._capture_process_identity
    original_process_state = db_module._process_group_state

    def capture_with_coordinator(pid: int) -> dict[str, object] | None:
        if pid == current_identity["pid"]:
            return dict(current_identity)
        return original_capture(pid)

    def process_state_with_coordinator(
        pid: int, *, expected: dict[str, object] | None = None
    ) -> str:
        if pid == current_identity["pid"]:
            return (
                "live"
                if expected is None or dict(expected) == dict(current_identity)
                else "unknown"
            )
        return original_process_state(pid, expected=expected)

    monkeypatch.setattr(
        db_module, "_capture_process_identity", capture_with_coordinator
    )
    monkeypatch.setattr(
        db_module, "_process_group_state", process_state_with_coordinator
    )
    monkeypatch.setattr(
        db_module,
        "_capture_rpc_coordinator_identity",
        lambda: dict(current_identity),
    )
    with ArtifactRoot.open(tmp_path / "artifacts", cwd=tmp_path) as recovery_root:
        result = recover_rpc_handoffs(
            conn,
            artifact_root=recovery_root,
            expected_coordinator_id="coord-1",
        )
    assert result.status == "recovered" and result.run_ids == (run_id,)
    if subordinate == "omp_live":
        live["omp"] = True
    else:
        observation = json.loads(
            conn.execute(
                "SELECT observation_json FROM application_runs WHERE id=?",
                (run_id,),
            ).fetchone()[0]
        )
        process = observation["_process"]
        process.pop("browser")
        conn.execute(
            "UPDATE application_runs SET observation_json=? WHERE id=?",
            (json.dumps(observation), run_id),
        )
        conn.commit()
    before = tuple(
        conn.execute(
            "SELECT state, version FROM application_rpc_runs WHERE run_id=?",
            (run_id,),
        ).fetchone()
    )
    result = reconcile_abandoned_rpc_runs(
        conn, expected_coordinator_id="coord-1"
    )
    assert result.status == "conflict"
    assert result.conflict_run_ids == (run_id,)
    assert tuple(
        conn.execute(
            "SELECT state, version FROM application_rpc_runs WHERE run_id=?",
            (run_id,),
        ).fetchone()
    ) == before
    conn.close()






def test_uncommitted_open_guarded_detached_both_live_replays_without_cleanup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    conn = connect(tmp_path / "jobs.sqlite3")
    root, run_id, child, _identities, _live, signals = _rpc_handoff_recovery_fixture(
        conn,
        tmp_path,
        monkeypatch,
        committed=False,
        review_state="open_guarded",
        detached=True,
        owner_live=True,
        browser_live=True,
    )
    result = recover_rpc_handoffs(conn, artifact_root=root)
    assert result.status == "recovered" and result.run_ids == (run_id,)
    assert signals == []
    app_row = conn.execute(
        "SELECT status, reason_code, outcome FROM application_runs WHERE id=?",
        (run_id,),
    ).fetchone()
    rpc_row = conn.execute(
        """
        SELECT state, human_review_ready, handoff_committed
        FROM application_rpc_runs WHERE run_id=?
        """,
        (run_id,),
    ).fetchone()
    assert tuple(app_row) == ("review_ready", "draft_ready", None)
    assert tuple(rpc_row) == ("review_ready", 1, 1)
    assert get_rpc_request(conn, child.request_id).state == "completed"  # type: ignore[union-attr]
    root.close()
    conn.close()


def test_uncommitted_open_guarded_partial_handoff_downgrades_non_ready(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    conn = connect(tmp_path / "jobs.sqlite3")
    root, run_id, _child, identities, _live, signals = _rpc_handoff_recovery_fixture(
        conn,
        tmp_path,
        monkeypatch,
        committed=False,
        review_state="open_guarded",
        detached=True,
        owner_live=False,
        browser_live=True,
    )
    result = recover_rpc_handoffs(conn, artifact_root=root)
    assert result.status == "recovered" and result.run_ids == (run_id,)
    assert signals == [
        (identities["browser"]["pgid"], signal.SIGTERM),
        (identities["browser"]["pgid"], signal.SIGKILL),
    ]
    app_row = conn.execute(
        "SELECT status, reason_code FROM application_runs WHERE id=?", (run_id,)
    ).fetchone()
    rpc_row = conn.execute(
        "SELECT state, human_review_ready, handoff_committed FROM application_rpc_runs WHERE run_id=?",
        (run_id,),
    ).fetchone()
    assert tuple(app_row) == ("manual", "page_not_stable")
    assert tuple(rpc_row) == ("manual", 0, 1)
    root.close()
    conn.close()


def test_uncommitted_recovery_rebinds_then_closed_window_downgrades(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    conn = connect(tmp_path / "jobs.sqlite3")
    root, run_id, _child, _identities, _live, signals = _rpc_handoff_recovery_fixture(
        conn,
        tmp_path,
        monkeypatch,
        committed=False,
        review_state="open_guarded",
        detached=True,
        owner_live=False,
        browser_live=True,
        omp_live=False,
    )
    current = db_module._capture_rpc_coordinator_identity()
    assert current is not None
    result = recover_rpc_handoffs(conn, artifact_root=root)
    assert result.status == "recovered" and result.run_ids == (run_id,)
    row = conn.execute(
        """
        SELECT coordinator_pid, coordinator_pgid, coordinator_birth,
               omp_session_sha256, state, handoff_committed
        FROM application_rpc_runs WHERE run_id=?
        """,
        (run_id,),
    ).fetchone()
    assert (
        row["coordinator_pid"],
        row["coordinator_pgid"],
        row["coordinator_birth"],
        row["omp_session_sha256"],
        row["state"],
        row["handoff_committed"],
    ) == (
        current["pid"],
        current["pgid"],
        current["birth"],
        "b" * 64,
        "manual",
        1,
    )
    assert signals == [
        (_identities["browser"]["pgid"], signal.SIGTERM),
        (_identities["browser"]["pgid"], signal.SIGKILL),
    ]
    with root.open_run_dir(run_id) as run:
        review = run.read_json("review_session.json")
        review.update(
            {
                "state": "closed",
                "cleanup": True,
                "cleanup_trigger": "stdin_eof",
                "detached": False,
                "terminal_reason": "page_not_stable",
            }
        )
        run.write_json("review_session.json", review)
    assert db_module.reconcile_committed_handoff_failure(
        conn,
        run_id=run_id,
        coordinator_id="coord-1",
        artifact_root=root,
    )
    status = get_rpc_run_status(conn, run_id)
    assert status is not None
    assert status.state == "manual"
    assert status.reason_code == "page_not_stable"
    root.close()
    conn.close()


def test_coordinator_pid_reuse_is_absent_without_signaling_replacement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    conn = connect(tmp_path / "jobs.sqlite3")
    root, run_id, _child, _identities, _live, signals = _rpc_handoff_recovery_fixture(
        conn,
        tmp_path,
        monkeypatch,
        committed=True,
        review_state="open_guarded",
        detached=True,
        omp_live=False,
    )
    recorded = conn.execute(
        "SELECT coordinator_pid, coordinator_pgid, coordinator_birth FROM application_rpc_runs WHERE run_id=?",
        (run_id,),
    ).fetchone()
    previous_capture = db_module._capture_process_identity

    def capture(pid: int) -> dict[str, object] | None:
        if pid == int(recorded["coordinator_pid"]):
            return {
                "pid": int(recorded["coordinator_pid"]),
                "pgid": int(recorded["coordinator_pgid"]),
                "birth": "replacement-birth",
            }
        return previous_capture(pid)

    monkeypatch.setattr(db_module, "_capture_process_identity", capture)
    assert db_module._coordinator_identity_state(recorded) == "absent"
    result = recover_rpc_handoffs(conn, artifact_root=root)
    assert result.status == "noop"
    assert signals == []
    row = conn.execute(
        "SELECT state, human_review_ready FROM application_rpc_runs WHERE run_id=?",
        (run_id,),
    ).fetchone()
    assert tuple(row) == ("review_ready", 1)
    root.close()
    conn.close()


def test_committed_missing_review_manifest_quarantines_non_ready(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    conn = connect(tmp_path / "jobs.sqlite3")
    root, run_id, _child, identities, _live, signals = _rpc_handoff_recovery_fixture(
        conn,
        tmp_path,
        monkeypatch,
        committed=True,
        review_state="open_guarded",
        detached=True,
        include_review=False,
    )
    result = recover_rpc_handoffs(conn, artifact_root=root)
    assert result.status == "recovered" and result.run_ids == (run_id,)
    assert signals == [
        (identities["owner"]["pgid"], signal.SIGTERM),
        (identities["owner"]["pgid"], signal.SIGKILL),
        (identities["browser"]["pgid"], signal.SIGTERM),
        (identities["browser"]["pgid"], signal.SIGKILL),
    ]
    app_row = conn.execute(
        "SELECT status, reason_code FROM application_runs WHERE id=?", (run_id,)
    ).fetchone()
    rpc_row = conn.execute(
        "SELECT state, human_review_ready FROM application_rpc_runs WHERE run_id=?",
        (run_id,),
    ).fetchone()
    assert tuple(app_row) == ("manual", "page_not_stable")
    assert tuple(rpc_row) == ("manual", 0)
    root.close()
    conn.close()


def test_committed_handoff_drains_bound_live_omp_before_healthy_fast_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    conn = connect(tmp_path / "jobs.sqlite3")
    root, run_id, _child, identities, _live, signals = _rpc_handoff_recovery_fixture(
        conn,
        tmp_path,
        monkeypatch,
        committed=True,
        review_state="open_guarded",
        detached=True,
        omp_live=True,
    )
    result = recover_rpc_handoffs(conn, artifact_root=root)
    assert result.status == "noop"
    assert signals == [
        (identities["omp"]["pgid"], signal.SIGTERM),
        (identities["omp"]["pgid"], signal.SIGKILL),
    ]
    rpc_row = conn.execute(
        "SELECT state, human_review_ready FROM application_rpc_runs WHERE run_id=?",
        (run_id,),
    ).fetchone()
    assert tuple(rpc_row) == ("review_ready", 1)
    root.close()
    conn.close()


def test_committed_review_identity_mismatch_quarantines_without_signal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    conn = connect(tmp_path / "jobs.sqlite3")
    root, run_id, _child, identities, _live, signals = _rpc_handoff_recovery_fixture(
        conn,
        tmp_path,
        monkeypatch,
        committed=True,
        review_state="open_guarded",
        detached=True,
    )
    with root.open_run_dir(run_id) as run:
        review = json.loads(run.read_bytes("review_session.json").decode("utf-8"))
        review["owner_pid"] = int(identities["owner"]["pid"]) + 1
        run.write_json("review_session.json", review)
    result = recover_rpc_handoffs(conn, artifact_root=root)
    assert result.status == "recovered" and result.run_ids == (run_id,)
    assert signals == [
        (identities["owner"]["pgid"], signal.SIGTERM),
        (identities["owner"]["pgid"], signal.SIGKILL),
        (identities["browser"]["pgid"], signal.SIGTERM),
        (identities["browser"]["pgid"], signal.SIGKILL),
    ]
    app_row = conn.execute(
        "SELECT status, reason_code FROM application_runs WHERE id=?", (run_id,)
    ).fetchone()
    rpc_row = conn.execute(
        "SELECT state, human_review_ready FROM application_rpc_runs WHERE run_id=?",
        (run_id,),
    ).fetchone()
    assert tuple(app_row) == ("manual", "page_not_stable")
    assert tuple(rpc_row) == ("manual", 0)
    root.close()
    conn.close()


def test_committed_corrupt_review_manifest_cleans_bound_groups_before_quarantine(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    conn = connect(tmp_path / "jobs.sqlite3")
    root, run_id, _child, identities, _live, signals = _rpc_handoff_recovery_fixture(
        conn,
        tmp_path,
        monkeypatch,
        committed=True,
        review_state="open_guarded",
        detached=True,
    )
    with root.open_run_dir(run_id) as run:
        run.write_bytes("review_session.json", b"{")
    result = recover_rpc_handoffs(conn, artifact_root=root)
    assert result.status == "recovered" and result.run_ids == (run_id,)
    assert signals == [
        (identities["owner"]["pgid"], signal.SIGTERM),
        (identities["owner"]["pgid"], signal.SIGKILL),
        (identities["browser"]["pgid"], signal.SIGTERM),
        (identities["browser"]["pgid"], signal.SIGKILL),
    ]
    assert tuple(
        conn.execute(
            "SELECT status, reason_code FROM application_runs WHERE id=?",
            (run_id,),
        ).fetchone()
    ) == ("manual", "page_not_stable")
    root.close()
    conn.close()


@pytest.mark.parametrize(
    ("absent_kind", "live_kind"),
    [("owner", "browser"), ("browser", "owner")],
)
def test_partial_handoff_cleanup_supervises_only_exact_live_sibling(
    absent_kind: str,
    live_kind: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    identities = {
        "owner": {"pid": 50101, "pgid": 50101, "birth": "owner-birth"},
        "browser": {"pid": 50202, "pgid": 50202, "birth": "browser-birth"},
    }
    live = {kind: kind == live_kind for kind in identities}
    signals: list[tuple[int, signal.Signals]] = []

    def capture(pid: int) -> dict[str, object] | None:
        for kind, identity in identities.items():
            if identity["pid"] == pid:
                return dict(identity) if live[kind] else None
        return None

    def killpg(pgid: int, signum: signal.Signals) -> None:
        signals.append((pgid, signum))
        if signum == signal.SIGKILL:
            for kind, identity in identities.items():
                if identity["pgid"] == pgid:
                    live[kind] = False

    monkeypatch.setattr(
        db_module,
        "_process_group_state",
        lambda pid, expected=None: (
            "live"
            if any(identity["pid"] == pid and live[kind] for kind, identity in identities.items())
            else "absent"
        ),
    )
    monkeypatch.setattr(db_module, "_capture_process_identity", capture)
    monkeypatch.setattr(db_module, "_RPC_HANDOFF_TERM_TIMEOUT_SECONDS", 0.0)
    monkeypatch.setattr(db_module, "_RPC_HANDOFF_KILL_TIMEOUT_SECONDS", 0.0)
    monkeypatch.setattr(db_module, "_RPC_HANDOFF_PROBE_INTERVAL_SECONDS", 0.0)
    monkeypatch.setattr(db_module.os, "killpg", killpg)

    mode = db_module._supervise_partial_handoff_processes(identities)

    assert mode == "partial"
    assert signals == [
        (identities[live_kind]["pgid"], signal.SIGTERM),
        (identities[live_kind]["pgid"], signal.SIGKILL),
    ]
    assert not live[absent_kind] and not live[live_kind]


def test_partial_handoff_cleanup_unknown_identity_fails_closed_without_signal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    owner = {"pid": 50303, "pgid": 50303, "birth": "owner-birth"}
    browser = {"pid": 50404, "pgid": 50404, "birth": "browser-birth"}
    signals: list[tuple[int, signal.Signals]] = []

    def capture(pid: int) -> dict[str, object] | None:
        if pid == owner["pid"]:
            return None
        if pid == browser["pid"]:
            return {"pid": pid, "pgid": pid, "birth": "reused-browser"}
        return None

    monkeypatch.setattr(
        db_module,
        "_process_group_state",
        lambda pid, expected=None: (
            "unknown" if pid == browser["pid"] else "absent"
        ),
    )
    monkeypatch.setattr(db_module, "_capture_process_identity", capture)
    monkeypatch.setattr(
        db_module.os,
        "killpg",
        lambda pgid, signum: signals.append((pgid, signum)),
    )

    with pytest.raises(RuntimeError, match="unknown"):
        db_module._supervise_partial_handoff_processes(
            {"owner": owner, "browser": browser}
        )
    assert signals == []


def test_partial_handoff_cleanup_both_live_preserves_healthy_window(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    owner = {"pid": 50505, "pgid": 50505, "birth": "owner-birth"}
    browser = {"pid": 50606, "pgid": 50606, "birth": "browser-birth"}
    signals: list[tuple[int, signal.Signals]] = []
    monkeypatch.setattr(
        db_module,
        "_process_group_state",
        lambda pid, expected=None: "live",
    )
    monkeypatch.setattr(
        db_module,
        "_capture_process_identity",
        lambda pid: (
            dict(owner)
            if pid == owner["pid"]
            else (dict(browser) if pid == browser["pid"] else None)
        ),
    )
    monkeypatch.setattr(
        db_module.os,
        "killpg",
        lambda pgid, signum: signals.append((pgid, signum)),
    )

    assert (
        db_module._supervise_partial_handoff_processes(
            {"owner": owner, "browser": browser}
        )
        == "healthy"
    )
    assert signals == []


def test_handoff_quarantine_running_state_sets_finished_at_and_keeps_claim(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    conn = connect(tmp_path / "jobs.sqlite3")
    parent, claim = _new_rpc_claim(conn, tmp_path, request_id=_uuid(70))
    child = _rpc_request(
        request_id=_uuid(71),
        operation="browser.prepare_human_handoff",
        run_id=claim.run_id,
        payload={"observation_sha256": "a" * 64},
    )
    reserve_rpc_request(conn, request=child, parent_request_id=parent.request_id)
    observation = json.loads(
        conn.execute(
            "SELECT observation_json FROM application_runs WHERE id=?",
            (claim.run_id,),
        ).fetchone()[0]
    )
    observation["_handoff_intent"] = {
        "child_request_id": child.request_id,
        "parent_request_id": parent.request_id,
        "observation_sha256": "a" * 64,
    }
    conn.execute(
        """
        UPDATE application_runs SET observation_json=? WHERE id=?
        """,
        (json.dumps(observation), claim.run_id),
    )
    conn.execute(
        """
        UPDATE application_rpc_runs
        SET coordinator_pid=987654, coordinator_pgid=987654,
            coordinator_birth='gone-coordinator'
        WHERE run_id=?
        """,
        (claim.run_id,),
    )
    conn.commit()
    monkeypatch.setattr(db_module, "_capture_process_identity", lambda pid: None)
    rpc_row = conn.execute(
        "SELECT * FROM application_rpc_runs WHERE run_id=?", (claim.run_id,)
    ).fetchone()
    app_row = conn.execute(
        "SELECT * FROM application_runs WHERE id=?", (claim.run_id,)
    ).fetchone()

    assert db_module._quarantine_rpc_handoff(
        conn, rpc_row=rpc_row, application_row=app_row
    )
    app_after = conn.execute(
        "SELECT status, reason_code, finished_at, outcome FROM application_runs WHERE id=?",
        (claim.run_id,),
    ).fetchone()
    rpc_after = conn.execute(
        "SELECT state, human_review_ready, handoff_committed FROM application_rpc_runs WHERE run_id=?",
        (claim.run_id,),
    ).fetchone()
    assert tuple(app_after) == ("manual", "page_not_stable", app_after["finished_at"], None)
    assert app_after["finished_at"]
    assert tuple(rpc_after) == ("manual", 0, 0)
    assert tuple(
        conn.execute(
            "SELECT event_type, summary_code FROM application_progress_events WHERE run_id=?",
            (claim.run_id,),
        ).fetchone()
    ) == ("manual_intervention_required", "page_not_stable")
    child_after = get_rpc_request(conn, child.request_id)
    assert child_after.state == "completed"  # type: ignore[union-attr]
    assert json.loads(child_after.response_json)["result"]["reason_code"] == "page_not_stable"  # type: ignore[union-attr]
    conn.close()


def test_registration_rejects_cross_run_session_reuse_transactionally(tmp_path: Path) -> None:
    conn = connect(tmp_path / "jobs.sqlite3")
    _initialize(conn, tmp_path)
    _job(conn, url="https://boards.greenhouse.io/acme/jobs/session-a")
    _job(conn, url="https://boards.greenhouse.io/acme/jobs/session-b")
    first = claim_next_application_job(conn, owner="owner-a")
    second = claim_next_application_job(conn, owner="owner-b")
    assert first is not None and second is not None
    assert register_application_artifact(conn, run_id=first.run_id, artifact_dir=f"run-{first.run_id}")
    assert register_application_artifact(conn, run_id=second.run_id, artifact_dir=f"run-{second.run_id}")
    assert register_application_session(conn, run_id=first.run_id, session_id="shared-session")
    before = tuple(
        conn.execute(
            "SELECT session_id, observation_json FROM application_runs WHERE id=?",
            (second.run_id,),
        ).fetchone()
    )
    assert register_application_session(conn, run_id=second.run_id, session_id="shared-session") is False
    after = tuple(
        conn.execute(
            "SELECT session_id, observation_json FROM application_runs WHERE id=?",
            (second.run_id,),
        ).fetchone()
    )
    assert after == before


def test_registration_rejects_exact_identity_across_runs_and_kinds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    conn = connect(tmp_path / "jobs.sqlite3")
    _initialize(conn, tmp_path)
    _job(conn, url="https://boards.greenhouse.io/acme/jobs/process-a")
    _job(conn, url="https://boards.greenhouse.io/acme/jobs/process-b")
    first = claim_next_application_job(conn, owner="owner-a")
    second = claim_next_application_job(conn, owner="owner-b")
    assert first is not None and second is not None
    for claim, session_id in ((first, "process-session-a"), (second, "process-session-b")):
        assert register_application_artifact(conn, run_id=claim.run_id, artifact_dir=f"run-{claim.run_id}")
        assert register_application_session(conn, run_id=claim.run_id, session_id=session_id)
        assert mark_application_spawn_attempted(conn, run_id=claim.run_id, session_id=session_id)
    identities = {
        601: {"pid": 601, "pgid": 601, "birth": "birth-owner"},
        602: {"pid": 602, "pgid": 602, "birth": "birth-browser"},
        603: {"pid": 603, "pgid": 603, "birth": "birth-second-owner"},
    }
    monkeypatch.setattr(db_module, "_capture_process_identity", lambda pid: identities[pid])
    assert register_application_owner_process(
        conn, run_id=first.run_id, owner_pid=601, process_identity=identities[601]
    )
    assert register_application_browser_process(
        conn, run_id=first.run_id, browser_pid=602, process_identity=identities[602]
    )
    before = tuple(
        conn.execute(
            "SELECT owner_pid, browser_pid, observation_json FROM application_runs WHERE id=?",
            (second.run_id,),
        ).fetchone()
    )
    assert register_application_owner_process(
        conn, run_id=second.run_id, owner_pid=601, process_identity=identities[601]
    ) is False
    assert tuple(
        conn.execute(
            "SELECT owner_pid, browser_pid, observation_json FROM application_runs WHERE id=?",
            (second.run_id,),
        ).fetchone()
    ) == before
    assert register_application_owner_process(
        conn, run_id=second.run_id, owner_pid=603, process_identity=identities[603]
    )
    assert register_application_browser_process(
        conn, run_id=second.run_id, browser_pid=601, process_identity=identities[601]
    ) is False


def test_registration_allows_reused_pid_after_reviewed_owner_closes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    conn = connect(tmp_path / "jobs.sqlite3")
    _initialize(conn, tmp_path)
    _job(conn, url="https://boards.greenhouse.io/acme/jobs/reuse-a")
    first = claim_next_application_job(conn, owner="owner-a")
    assert first is not None
    assert register_application_artifact(conn, run_id=first.run_id, artifact_dir=f"run-{first.run_id}")
    assert register_application_session(conn, run_id=first.run_id, session_id="reuse-session-a")
    assert mark_application_spawn_attempted(conn, run_id=first.run_id, session_id="reuse-session-a")
    birth = {"value": "birth-old"}
    monkeypatch.setattr(
        db_module,
        "_capture_process_identity",
        lambda pid: {"pid": pid, "pgid": pid, "birth": birth["value"]},
    )
    old_identity = {"pid": 604, "pgid": 604, "birth": "birth-old"}
    assert register_application_owner_process(
        conn, run_id=first.run_id, owner_pid=604, process_identity=old_identity
    )
    finish_application_run(conn, run_id=first.run_id, status="failed", reason_code="browser_error")
    conn.execute(
        "UPDATE application_runs SET outcome='skipped', reviewed_at=? WHERE id=?",
        ("2026-07-10T00:04:00+00:00", first.run_id),
    )
    conn.commit()

    _job(conn, url="https://boards.greenhouse.io/acme/jobs/reuse-b")
    second = claim_next_application_job(conn, owner="owner-b")
    assert second is not None
    assert register_application_artifact(conn, run_id=second.run_id, artifact_dir=f"run-{second.run_id}")
    assert register_application_session(conn, run_id=second.run_id, session_id="reuse-session-b")
    assert mark_application_spawn_attempted(conn, run_id=second.run_id, session_id="reuse-session-b")
    birth["value"] = "birth-new"
    new_identity = {"pid": 604, "pgid": 604, "birth": "birth-new"}
    assert register_application_owner_process(
        conn, run_id=second.run_id, owner_pid=604, process_identity=new_identity
    )


def test_process_probe_distinguishes_surviving_group_from_pid_reuse(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    expected = {"pgid": 44, "birth": "birth-a"}
    monkeypatch.setattr(
        db_module,
        "_capture_process_identity",
        lambda pid: None,
    )
    monkeypatch.setattr(
        db_module,
        "_group_members",
        lambda pgid: {999},
    )
    assert db_module._process_group_state(
        123,
        expected=expected,
    ) == "live"
    monkeypatch.setattr(
        db_module,
        "_capture_process_identity",
        lambda pid: {
            "pid": pid,
            "pgid": 44,
            "birth": "birth-new",
        },
    )
    assert db_module._process_group_state(
        123,
        expected=expected,
    ) == "unknown"
    monkeypatch.setattr(
        db_module,
        "_group_members",
        lambda pgid: {123},
    )
    assert db_module._process_group_state(
        123,
        expected=expected,
    ) == "unknown"
    monkeypatch.setattr(
        db_module,
        "_group_members",
        lambda pgid: set(),
    )
    assert db_module._process_group_state(
        123,
        expected=expected,
    ) == "unknown"


def test_reconcile_closed_page_not_stable_downgrades_without_manifest_rewrite(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    conn = connect(tmp_path / "jobs.sqlite3")
    _initialize(conn, tmp_path)
    job_id = _job(conn, url="https://boards.greenhouse.io/acme/jobs/452")
    claim = claim_next_application_job(conn, owner="owner")
    assert claim is not None
    root = ArtifactRoot.open(tmp_path / "artifacts", cwd=tmp_path)
    owner = {"pid": 12452, "pgid": 12452, "birth": "birth-owner-proof"}
    browser = {"pid": 23462, "pgid": 23462, "birth": "birth-browser-proof"}
    monkeypatch.setattr(db_module, "_process_group_state", lambda pid, expected=None: "absent")
    monkeypatch.setattr(
        db_module,
        "_capture_process_identity",
        lambda pid: {"pid": pid, "pgid": pid, "birth": owner["birth"] if pid == owner["pid"] else browser["birth"]},
    )
    assert register_application_artifact(conn, run_id=claim.run_id, artifact_dir=f"run-{claim.run_id}")
    assert register_application_session(conn, run_id=claim.run_id, session_id="session-closed-proof", session_state="open")
    assert mark_application_spawn_attempted(conn, run_id=claim.run_id, session_id="session-closed-proof")
    assert register_application_owner_process(
        conn, run_id=claim.run_id, owner_pid=owner["pid"], process_identity=owner
    )
    assert register_application_browser_process(
        conn, run_id=claim.run_id, browser_pid=browser["pid"], process_identity=browser
    )
    monkeypatch.setattr(db_module, "_capture_process_identity", lambda pid: None)
    with root.create_run_dir(claim.run_id) as run:
        _write_review_manifest(
            run,
            {
                "run_id": claim.run_id,
                "job_id": job_id,
                "session_id": "session-closed-proof",
                "owner_pid": owner["pid"],
                "owner_pgid": owner["pgid"],
                "owner_birth": owner["birth"],
                "browser_pid": browser["pid"],
                "browser_pgid": browser["pgid"],
                "browser_birth": browser["birth"],
                "state": "closed",
                "cleanup": True,
                "cleanup_trigger": "browser_exit",
                "terminal_reason": "page_not_stable",
            },
        )
        before_manifest = run.read_bytes("review_session.json")
    finish_application_run(
        conn,
        run_id=claim.run_id,
        status="review_ready",
        reason_code="draft_ready",
    )
    assert reconcile_open_session_failure(
        conn,
        run_id=claim.run_id,
        session_id="session-closed-proof",
        reason_code="page_not_stable",
        artifact_root=root,
    ) is True
    row = conn.execute(
        "SELECT status, reason_code FROM application_runs WHERE id=?", (claim.run_id,)
    ).fetchone()
    assert (row["status"], row["reason_code"]) == ("manual", "page_not_stable")
    assert conn.execute("SELECT status FROM jobs WHERE id=?", (job_id,)).fetchone()["status"] == "in_progress"
    with root.open_run_dir(claim.run_id) as run:
        assert run.read_bytes("review_session.json") == before_manifest
    root.close()
    conn.close()


def test_committed_partial_handoff_cleans_sibling_and_downgrades(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    conn = connect(tmp_path / "jobs.sqlite3")
    root, run_id, _child, identities, _live, signals = _rpc_handoff_recovery_fixture(
        conn,
        tmp_path,
        monkeypatch,
        committed=True,
        review_state="open_guarded",
        detached=True,
        owner_live=False,
        browser_live=True,
    )
    result = recover_rpc_handoffs(conn, artifact_root=root)
    assert result.status == "recovered" and result.run_ids == (run_id,)
    assert signals == [
        (identities["browser"]["pgid"], signal.SIGTERM),
        (identities["browser"]["pgid"], signal.SIGKILL),
    ]
    app_row = conn.execute(
        "SELECT status, reason_code FROM application_runs WHERE id=?", (run_id,)
    ).fetchone()
    rpc_row = conn.execute(
        "SELECT state, human_review_ready FROM application_rpc_runs WHERE run_id=?",
        (run_id,),
    ).fetchone()
    assert tuple(app_row) == ("manual", "page_not_stable")
    assert tuple(rpc_row) == ("manual", 0)
    root.close()
    conn.close()


@pytest.mark.parametrize(
    ("durable_state", "reason_code"),
    [
        ("review_ready", "draft_ready"),
        ("manual", "page_not_stable"),
        ("blocked", "ats_mismatch"),
    ],
)
def test_committed_healthy_handoff_requires_exact_binding_and_stays_ready(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    durable_state: str,
    reason_code: str,
) -> None:
    conn = connect(tmp_path / "jobs.sqlite3")
    root, run_id, _child, _identities, _live, signals = _rpc_handoff_recovery_fixture(
        conn,
        tmp_path,
        monkeypatch,
        committed=True,
        review_state="open_guarded",
        detached=True,
    )
    conn.execute(
        "UPDATE application_runs SET status=?, reason_code=? WHERE id=?",
        (durable_state, reason_code, run_id),
    )
    conn.execute(
        "UPDATE application_rpc_runs SET state=?, human_review_ready=? WHERE run_id=?",
        (durable_state, int(durable_state == "review_ready"), run_id),
    )
    conn.commit()
    result = recover_rpc_handoffs(conn, artifact_root=root)
    assert result.status == "noop"
    assert signals == []
    app_row = conn.execute(
        "SELECT status, reason_code FROM application_runs WHERE id=?", (run_id,)
    ).fetchone()
    rpc_row = conn.execute(
        "SELECT state, human_review_ready FROM application_rpc_runs WHERE run_id=?",
        (run_id,),
    ).fetchone()
    assert tuple(app_row) == (durable_state, reason_code)
    assert tuple(rpc_row) == (durable_state, int(durable_state == "review_ready"))
    root.close()
    conn.close()


@pytest.mark.parametrize(
    ("durable_state", "reason_code"),
    [
        ("review_ready", "draft_ready"),
        ("manual", "page_not_stable"),
        ("blocked", "ats_mismatch"),
    ],
)
def test_committed_handoff_restart_rebinds_owner_for_later_close(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    durable_state: str,
    reason_code: str,
) -> None:
    conn = connect(tmp_path / "jobs.sqlite3")
    root, run_id, _child, _identities, _live, signals = _rpc_handoff_recovery_fixture(
        conn,
        tmp_path,
        monkeypatch,
        committed=True,
        review_state="open_guarded",
        detached=True,
    )
    conn.execute(
        "UPDATE application_runs SET status=?, reason_code=? WHERE id=?",
        (durable_state, reason_code, run_id),
    )
    conn.execute(
        "UPDATE application_rpc_runs SET state=?, human_review_ready=? WHERE run_id=?",
        (durable_state, int(durable_state == "review_ready"), run_id),
    )
    conn.commit()
    before = conn.execute(
        "SELECT coordinator_pid, coordinator_birth FROM application_rpc_runs WHERE run_id=?",
        (run_id,),
    ).fetchone()
    assert db_module.reconcile_committed_handoff_failure(
        conn,
        run_id=run_id,
        coordinator_id="coord-1",
        artifact_root=root,
        recovery=True,
    ) is False
    after = conn.execute(
        """
        SELECT coordinator_id, state, human_review_ready,
               coordinator_pid, coordinator_pgid, coordinator_birth
        FROM application_rpc_runs WHERE run_id=?
        """,
        (run_id,),
    ).fetchone()
    assert (after["state"], after["human_review_ready"]) == (
        durable_state,
        int(durable_state == "review_ready"),
    )
    assert (after["coordinator_pid"], after["coordinator_birth"]) != (
        before["coordinator_pid"],
        before["coordinator_birth"],
    )
    assert db_module._rpc_owner_matches(after, "coord-1")
    assert signals == []
    root.close()
    conn.close()


def test_rpc_recovery_expected_coordinator_filters_foreign_rows_and_allows_rightful_owner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = tmp_path / "jobs.sqlite3"
    conn = connect(db_path)
    root, run_id, _child, _identities, _live, signals = _rpc_handoff_recovery_fixture(
        conn,
        tmp_path,
        monkeypatch,
        committed=True,
        review_state="open_guarded",
        detached=True,
        owner_live=False,
        browser_live=False,
    )
    root.close()
    before = tuple(
        conn.execute(
            """
            SELECT coordinator_id, coordinator_pid, coordinator_pgid,
                   coordinator_birth, state, handoff_committed
            FROM application_rpc_runs WHERE run_id=?
            """,
            (run_id,),
        ).fetchone()
    )
    foreign = connect(db_path)
    with ArtifactRoot.open(tmp_path / "artifacts", cwd=tmp_path) as foreign_root:
        initialize_database(
            foreign,
            migration_artifact_root=foreign_root,
            expected_coordinator_id="coord-2",
        )
    foreign.close()
    assert signals == []
    assert tuple(
        conn.execute(
            """
            SELECT coordinator_id, coordinator_pid, coordinator_pgid,
                   coordinator_birth, state, handoff_committed
            FROM application_rpc_runs WHERE run_id=?
            """,
            (run_id,),
        ).fetchone()
    ) == before
    with ArtifactRoot.open(tmp_path / "artifacts", cwd=tmp_path) as rightful_root:
        result = recover_rpc_handoffs(
            conn,
            artifact_root=rightful_root,
            expected_coordinator_id="coord-1",
        )
    assert result.status == "recovered"
    assert result.run_ids == (run_id,)
    assert signals == []
    assert tuple(
        conn.execute(
            "SELECT coordinator_id, state FROM application_rpc_runs WHERE run_id=?",
            (run_id,),
        ).fetchone()
    ) == ("coord-1", "manual")
    conn.close()


def test_rpc_recovery_competing_stale_claim_cannot_quarantine_winner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = tmp_path / "jobs.sqlite3"
    setup = connect(db_path)
    root, run_id, _child, identities, _live, signals = _rpc_handoff_recovery_fixture(
        setup,
        tmp_path,
        monkeypatch,
        committed=True,
        review_state="open_guarded",
        detached=True,
        include_review=False,
    )
    root.close()
    setup.close()
    current_identity = db_module._capture_rpc_coordinator_identity()
    assert current_identity is not None
    original_capture = db_module._capture_process_identity
    original_process_state = db_module._process_group_state

    def capture_with_coordinator(pid: int) -> dict[str, object] | None:
        if pid == current_identity["pid"]:
            return dict(current_identity)
        return original_capture(pid)

    def process_state_with_coordinator(
        pid: int, *, expected: dict[str, object] | None = None
    ) -> str:
        if pid == current_identity["pid"]:
            return (
                "live"
                if expected is None or dict(expected) == dict(current_identity)
                else "unknown"
            )
        return original_process_state(pid, expected=expected)

    def capture_coordinator_identity() -> dict[str, object]:
        return dict(current_identity)

    monkeypatch.setattr(
        db_module, "_capture_process_identity", capture_with_coordinator
    )
    monkeypatch.setattr(
        db_module, "_process_group_state", process_state_with_coordinator
    )
    monkeypatch.setattr(
        db_module, "_capture_rpc_coordinator_identity", capture_coordinator_identity
    )
    barrier = threading.Barrier(2)
    original_claim = db_module._claim_rpc_handoff_recovery

    def synchronized_claim(*args: object, **kwargs: object) -> object:
        barrier.wait(timeout=5)
        return original_claim(*args, **kwargs)

    monkeypatch.setattr(
        db_module, "_claim_rpc_handoff_recovery", synchronized_claim
    )

    def recover_one() -> object:
        connection = connect(db_path)
        try:
            with ArtifactRoot.open(tmp_path / "artifacts", cwd=tmp_path) as artifact_root:
                return recover_rpc_handoffs(
                    connection,
                    artifact_root=artifact_root,
                    expected_coordinator_id="coord-1",
                )
        finally:
            connection.close()

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda _index: recover_one(), (0, 1)))
    statuses = sorted(result.status for result in results)  # type: ignore[union-attr]
    assert statuses == ["noop", "recovered"]
    assert signals == [
        (identities["owner"]["pgid"], signal.SIGTERM),
        (identities["owner"]["pgid"], signal.SIGKILL),
        (identities["browser"]["pgid"], signal.SIGTERM),
        (identities["browser"]["pgid"], signal.SIGKILL),
    ]
    row = connect(db_path)
    try:
        assert tuple(
            row.execute(
                "SELECT coordinator_id, state FROM application_rpc_runs WHERE run_id=?",
                (run_id,),
            ).fetchone()
        ) == ("coord-1", "manual")
    finally:
        row.close()


def test_claim_blocks_unregistered_application_spawn_marker(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conn = connect(tmp_path / "jobs.sqlite3")
    _initialize(conn, tmp_path)
    job_id = _job(conn, url="https://boards.greenhouse.io/acme/jobs/unregistered")
    first = claim_next_application_job(conn, owner="first-owner")
    assert first is not None
    now = db_module.utc_now()
    conn.execute(
        """
        UPDATE application_runs
        SET status='failed', reason_code='browser_error', outcome='retry',
            reviewed_at=?, finished_at=?, observation_json=?
        WHERE id=?
        """,
        (now, now, json.dumps({"_spawn_attempted": True, "_process": {}}), first.run_id),
    )
    conn.execute("UPDATE jobs SET status='queued' WHERE id=?", (job_id,))
    conn.commit()
    assert claim_next_application_job(conn, owner="blocked") is None
    conn.execute(
        """
        UPDATE application_runs
        SET owner_pid=101, browser_pid=202, observation_json=?
        WHERE id=?
        """,
        (
            json.dumps(
                {
                    "_spawn_attempted": True,
                    "_process": {
                        "owner": {"pid": 101, "pgid": 101, "birth": "owner"},
                        "browser": {"pid": 202, "pgid": 202, "birth": "browser"},
                    },
                }
            ),
            first.run_id,
        ),
    )
    conn.commit()
    monkeypatch.setattr(
        db_module,
        "_process_group_state",
        lambda pid, *, expected=None: "absent",
    )
    assert claim_next_application_job(conn, owner="after-proof") is not None


def test_rpc_claim_blocks_unregistered_omp_spawn_marker(
    tmp_path: Path,
) -> None:
    conn = connect(tmp_path / "jobs.sqlite3")
    request, first = _new_rpc_claim(conn, tmp_path, request_id=_uuid(71))
    assert first.run_id is not None
    now = db_module.utc_now()
    job_id = int(
        conn.execute(
            "SELECT job_id FROM application_runs WHERE id=?", (first.run_id,)
        ).fetchone()[0]
    )
    conn.execute(
        """
        UPDATE application_runs
        SET status='failed', reason_code='browser_error', outcome='retry',
            reviewed_at=?, finished_at=?, observation_json=?
        WHERE id=?
        """,
        (now, now, json.dumps({"_omp_spawn_attempted": True}), first.run_id),
    )
    conn.execute("UPDATE jobs SET status='queued' WHERE id=?", (job_id,))
    conn.commit()
    blocked = claim_application_job_for_rpc(
        conn,
        owner="rpc-owner-2",
        request=_rpc_request(request_id=_uuid(72), url=_RPC_URL),
        coordinator_id="coord-2",
    )
    assert blocked.outcome == "unavailable"
def test_legacy_claim_skips_conflicting_queued_job_and_claims_next_safe_candidate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    conn = connect(tmp_path / "jobs.sqlite3")
    _initialize(conn, tmp_path)
    first_job = _job(conn, url="https://boards.greenhouse.io/acme/jobs/conflicting")
    second_job = _job(conn, url="https://boards.greenhouse.io/acme/jobs/safe")
    first = claim_next_application_job(conn, owner="first-owner")
    assert first is not None and first.job["id"] == first_job
    now = db_module.utc_now()
    conn.execute(
        """
        UPDATE application_runs
        SET status='failed', reason_code='browser_error', outcome='retry',
            reviewed_at=?, finished_at=?,
            observation_json=?
        WHERE id=?
        """,
        (now, now, json.dumps({"_spawn_attempted": True, "_process": {}}), first.run_id),
    )
    conn.execute("UPDATE jobs SET status='queued' WHERE id=?", (first_job,))
    conn.commit()
    assert claim_next_application_job(conn, owner="safe-owner").job["id"] == second_job  # type: ignore[union-attr]
    assert conn.execute("SELECT status FROM jobs WHERE id=?", (first_job,)).fetchone()[0] == "queued"
    conn.close()


def test_legacy_claim_skips_quarantined_queued_job_and_claims_next_safe_candidate(
    tmp_path: Path,
) -> None:
    conn = connect(tmp_path / "jobs.sqlite3")
    _initialize(conn, tmp_path)
    first_job = _job(conn, url="https://boards.greenhouse.io/acme/jobs/quarantined")
    second_job = _job(conn, url="https://boards.greenhouse.io/acme/jobs/available")
    first = claim_next_application_job(conn, owner="first-owner")
    assert first is not None and first.job["id"] == first_job
    now = db_module.utc_now()
    conn.execute(
        """
        UPDATE application_runs
        SET status='manual', reason_code='page_not_stable',
            finished_at=?, observation_json=?
        WHERE id=?
        """,
        (
            now,
            json.dumps({"_launch_cleanup_quarantine": {"reason_code": "page_not_stable"}}),
            first.run_id,
        ),
    )
    conn.execute("UPDATE jobs SET status='queued' WHERE id=?", (first_job,))
    conn.commit()
    next_claim = claim_next_application_job(conn, owner="safe-owner")
    assert next_claim is not None and next_claim.job["id"] == second_job
    assert conn.execute("SELECT status FROM jobs WHERE id=?", (first_job,)).fetchone()[0] == "queued"
    conn.close()


def test_verified_rpc_pre_spawn_abort_clears_marker_for_retryable_claim(
    tmp_path: Path,
) -> None:
    conn = connect(tmp_path / "jobs.sqlite3")
    request, claim = _new_rpc_claim(conn, tmp_path, request_id=_uuid(903))
    assert db_module.mark_rpc_omp_spawn_attempted(
        conn, run_id=claim.run_id, coordinator_id="coord-1"
    )
    info = db_module.abort_rpc_start(
        conn,
        request=request,
        coordinator_id="coord-1",
        error_code="unavailable",
        release_claim=True,
    )
    assert info.state == "completed"
    observation = json.loads(
        conn.execute(
            "SELECT observation_json FROM application_runs WHERE id=?", (claim.run_id,)
        ).fetchone()[0]
    )
    assert "_omp_spawn_attempted" not in observation
    retried = claim_next_application_job(conn, owner="retry-owner")
    assert retried is not None and retried.job["id"] == claim.claim.job["id"]  # type: ignore[union-attr]
    conn.close()


def test_rpc_claim_uses_canonical_storage_key_for_explicit_https_default_port(
    tmp_path: Path,
) -> None:
    conn = connect(tmp_path / "jobs.sqlite3")
    _initialize(conn, tmp_path)
    db_module.upsert_raw_job(
        conn,
        {
            "source_job_id": "rpc-default-port",
            "url": "https://boards.greenhouse.io:443/acme/jobs/456",
            "title": "Engineer",
            "company": "Acme",
        },
        source="fixture",
    )
    request = _rpc_request(request_id=_uuid(904), url=_RPC_URL)
    outcome = claim_application_job_for_rpc(
        conn,
        owner="rpc-owner",
        request=request,
        coordinator_id="coord-1",
    )
    assert outcome.outcome == "new" and outcome.claim is not None
    assert outcome.claim.job["canonical_url"] == _RPC_URL
    conn.close()

def test_rpc_no_job_deadline_expiry_rolls_back_rowless_reservation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conn = connect(tmp_path / "jobs.sqlite3")
    _initialize(conn, tmp_path)
    request = _rpc_request(request_id=_uuid(905))
    original_require = db_module._require_rpc_deadline_live
    calls = 0

    def expire_before_commit(deadline_unix_ms: int | None) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise db_module.RpcDeadlineExceeded("expired before rowless commit")
        original_require(deadline_unix_ms)

    monkeypatch.setattr(db_module, "_require_rpc_deadline_live", expire_before_commit)
    with pytest.raises(db_module.RpcDeadlineExceeded):
        claim_application_job_for_rpc(
            conn,
            owner="rpc-owner",
            request=request,
            coordinator_id="coord-1",
        )
    assert calls == 2
    assert conn.in_transaction is False
    assert conn.execute(
        "SELECT COUNT(*) FROM application_rpc_requests WHERE request_id=?",
        (request.request_id,),
    ).fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM application_runs").fetchone()[0] == 0
    conn.close()
