from __future__ import annotations

import hashlib
import hmac
import ipaddress
import json
import re
import os
import signal
import sqlite3
import stat
import subprocess
import time
from datetime import datetime, timezone
from uuid import UUID
from pathlib import Path
from dataclasses import dataclass
from typing import Any, Literal, Mapping
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from . import artifacts as _artifacts
from .artifacts import ArtifactRoot, ArtifactSecurityError
from .contracts import ApplicationClaim, PublicReasonCode, StoredJobInfo, thaw_json
from .application_rpc_contracts import (
    APPLICATION_OPERATIONS,
    APPLICATION_RPC_PROTOCOL_VERSION,
    BROWSER_OPERATIONS,
    MAX_APPLICATION_JSON_BYTES,
    PUBLIC_ERROR_MESSAGES,
    ApplicationRpcRequest,
    build_application_response,
    parse_application_response,
    semantic_request_sha256,
)
from .browser_adapter import SAFE_BROWSER_ERROR_CODES
from .safety import SUPPORTED_ATS_POLICIES


_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")

_RPC_OPERATION_SQL = ",".join(repr(value) for value in APPLICATION_OPERATIONS)
_RPC_ATS_SQL = ",".join(repr(value) for value in SUPPORTED_ATS_POLICIES)
_RPC_EVENT_TYPES = (
    "run_started", "page_observed", "field_resolved", "action_allowed",
    "action_rejected", "resume_uploaded", "validation_error",
    "manual_intervention_required", "review_ready", "browser_handed_off",
    "run_cancelled", "run_failed", "screenshot_captured",
    "awaiting_resume", "resume_requested",
)
_RPC_EVENT_TYPE_SQL = ",".join(repr(value) for value in _RPC_EVENT_TYPES)
_RPC_EVENT_SUMMARY_CODES = (
    "started", "observed", "allowed", "rejected", "uploaded",
    "validation_error", "awaiting_resume", "resume_requested",
    "manual_required", "review_ready", "handed_off", "cancelled",
    "failed", "captured",
    *tuple(code.value for code in PublicReasonCode),
)
_RPC_SUMMARY_SQL = ",".join(repr(value) for value in dict.fromkeys(_RPC_EVENT_SUMMARY_CODES))


SCHEMA_SQL = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS jobs (
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
    status TEXT NOT NULL DEFAULT 'queued' CHECK (status IN ('queued', 'in_progress', 'archived')),
    raw_json TEXT NOT NULL DEFAULT '{}',
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    CHECK (source_job_id IS NOT NULL OR canonical_url IS NOT NULL),
    UNIQUE(source, source_job_id),
    UNIQUE(canonical_url)
);

CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status);
CREATE INDEX IF NOT EXISTS idx_jobs_discovered_at ON jobs(discovered_at);
CREATE INDEX IF NOT EXISTS idx_jobs_posted_at ON jobs(posted_at);

CREATE TABLE IF NOT EXISTS sync_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source TEXT NOT NULL,
    profile TEXT,
    mode TEXT NOT NULL,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    checkpoint TEXT,
    success INTEGER NOT NULL DEFAULT 0,
    jobs_seen INTEGER NOT NULL DEFAULT 0,
    jobs_returned INTEGER NOT NULL DEFAULT 0,
    jobs_inserted INTEGER NOT NULL DEFAULT 0,
    jobs_updated INTEGER NOT NULL DEFAULT 0,
    error TEXT
);
"""


APPLICATION_STATUSES = ("running", "review_ready", "manual", "blocked", "failed")
APPLICATION_OUTCOMES = ("submitted", "skipped", "retry")
PUBLIC_REASON_CODES = tuple(code.value for code in PublicReasonCode)
REASON_STATUS = {
    "draft_ready": "review_ready",
    "artifact_error": "failed",
    "browser_error": "failed",
    "database_error": "failed",
    "handoff_failed": "failed",
    "abandoned_running_attempt": "failed",
    "legacy_run": "review_ready",
    "unsupported_ats": "blocked",
    "ats_mismatch": "blocked",
    "invalid_application_url": "blocked",
    "unsafe_navigation_target": "blocked",
    "unsafe_network_attempt": "blocked",
    "observation_too_large": "blocked",
    "captcha": "blocked",
    "authentication_required": "blocked",
    "assessment_required": "blocked",
    "unsupported_frame": "blocked",
}
for _reason in PUBLIC_REASON_CODES:
    REASON_STATUS.setdefault(_reason, "manual")
_REASON_STATUS_SQL = " OR ".join(
    f"(status = '{status}' AND reason_code = '{reason}')"
    for reason, status in REASON_STATUS.items()
    if reason != "legacy_run"
)


APPLICATION_SCHEMA_SQL = f"""
CREATE TABLE application_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id INTEGER NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    apply_url TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ({",".join(repr(v) for v in APPLICATION_STATUSES)})),
    reason_code TEXT CHECK (
        (status = 'running' AND reason_code IS NULL)
        OR (
            status <> 'running'
            AND reason_code IN ({",".join(repr(v) for v in PUBLIC_REASON_CODES)})
            AND (reason_code = 'legacy_run' OR {_REASON_STATUS_SQL})
        )
    ),
    owner TEXT NOT NULL,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    observation_json TEXT NOT NULL DEFAULT '{{}}',
    plan_json TEXT NOT NULL DEFAULT '{{}}',
    artifact_dir TEXT,
    session_id TEXT,
    owner_pid INTEGER CHECK (owner_pid IS NULL OR owner_pid > 0),
    browser_pid INTEGER CHECK (browser_pid IS NULL OR browser_pid > 0),
    outcome TEXT CHECK (outcome IS NULL OR outcome IN ({",".join(repr(v) for v in APPLICATION_OUTCOMES)})),
    reviewed_at TEXT,
    CHECK ((status = 'running' AND finished_at IS NULL) OR (status <> 'running' AND finished_at IS NOT NULL)),
    CHECK ((reviewed_at IS NULL AND outcome IS NULL) OR (reviewed_at IS NOT NULL AND outcome IS NOT NULL)),
    CHECK (session_id IS NULL OR artifact_dir IS NOT NULL),
    CHECK (browser_pid IS NULL OR owner_pid IS NOT NULL)
)
"""


APPLICATION_INDEX_SQL = (
    "CREATE INDEX idx_application_runs_job_id ON application_runs(job_id)",
    "CREATE INDEX idx_application_runs_status ON application_runs(status)",
    "CREATE INDEX idx_application_runs_latest ON application_runs(job_id, started_at DESC, id DESC)",
    "CREATE UNIQUE INDEX idx_application_runs_running_job ON application_runs(job_id) WHERE status='running'",
)
RPC_SCHEMA_SQL = f"""
CREATE TABLE application_rpc_requests (
    request_id TEXT PRIMARY KEY
        CHECK (
            length(request_id) = 36
            AND request_id = lower(request_id)
            AND substr(request_id, 9, 1) = '-'
            AND substr(request_id, 14, 1) = '-'
            AND substr(request_id, 19, 1) = '-'
            AND substr(request_id, 24, 1) = '-'
            AND request_id NOT GLOB '*[^0-9a-f-]*'
        ),
    protocol_version INTEGER NOT NULL CHECK (protocol_version = {APPLICATION_RPC_PROTOCOL_VERSION}),
    operation TEXT NOT NULL CHECK (operation IN ({_RPC_OPERATION_SQL})),
    semantic_sha256 TEXT NOT NULL CHECK (
        length(semantic_sha256) = 64
        AND semantic_sha256 = lower(semantic_sha256)
        AND semantic_sha256 NOT GLOB '*[^0-9a-f]*'
    ),
    request_json TEXT NOT NULL CHECK (length(CAST(request_json AS BLOB)) <= {MAX_APPLICATION_JSON_BYTES}),
    run_id INTEGER REFERENCES application_runs(id),
    parent_request_id TEXT REFERENCES application_rpc_requests(request_id),
    state TEXT NOT NULL CHECK (state IN ('pending', 'completed')),
    response_json TEXT CHECK (
        response_json IS NULL
        OR length(CAST(response_json AS BLOB)) <= {MAX_APPLICATION_JSON_BYTES}
    ),
    created_at TEXT NOT NULL CHECK (length(created_at) > 0),
    completed_at TEXT,
    CHECK (parent_request_id IS NULL OR parent_request_id <> request_id),
    CHECK ((state = 'pending' AND response_json IS NULL AND completed_at IS NULL) OR
           (state = 'completed' AND response_json IS NOT NULL AND completed_at IS NOT NULL))
);

CREATE TABLE application_rpc_runs (
    run_id INTEGER PRIMARY KEY REFERENCES application_runs(id),
    coordinator_id TEXT NOT NULL CHECK (length(coordinator_id) BETWEEN 1 AND 256),
    coordinator_pid INTEGER NOT NULL CHECK (coordinator_pid > 0),
    coordinator_pgid INTEGER NOT NULL CHECK (coordinator_pgid > 0),
    coordinator_birth TEXT NOT NULL CHECK (length(coordinator_birth) BETWEEN 1 AND 256),
    state TEXT NOT NULL CHECK (state IN ('starting', 'running', 'manual', 'blocked', 'review_ready', 'failed')),
    ats_policy TEXT CHECK (ats_policy IS NULL OR ats_policy IN ({_RPC_ATS_SQL})),
    omp_process_pid INTEGER CHECK (omp_process_pid IS NULL OR omp_process_pid > 0),
    omp_process_pgid INTEGER CHECK (omp_process_pgid IS NULL OR omp_process_pgid > 0),
    omp_process_birth TEXT CHECK (omp_process_birth IS NULL OR length(omp_process_birth) BETWEEN 1 AND 256),
    omp_session_sha256 TEXT CHECK (
        omp_session_sha256 IS NULL OR (
            length(omp_session_sha256) = 64
            AND omp_session_sha256 = lower(omp_session_sha256)
            AND omp_session_sha256 NOT GLOB '*[^0-9a-f]*'
        )
    ),
    last_observation_sha256 TEXT CHECK (
        last_observation_sha256 IS NULL OR (
            length(last_observation_sha256) = 64
            AND last_observation_sha256 = lower(last_observation_sha256)
            AND last_observation_sha256 NOT GLOB '*[^0-9a-f]*'
        )
    ),
    action_sequence INTEGER NOT NULL DEFAULT 0 CHECK (action_sequence >= 0),
    artifact_manifest_sha256 TEXT CHECK (
        artifact_manifest_sha256 IS NULL OR (
            length(artifact_manifest_sha256) = 64
            AND artifact_manifest_sha256 = lower(artifact_manifest_sha256)
            AND artifact_manifest_sha256 NOT GLOB '*[^0-9a-f]*'
        )
    ),
    current_form_step TEXT CHECK (current_form_step IS NULL OR length(current_form_step) BETWEEN 1 AND 256),
    human_review_ready INTEGER NOT NULL DEFAULT 0 CHECK (human_review_ready IN (0, 1)),
    handoff_committed INTEGER NOT NULL DEFAULT 0 CHECK (handoff_committed IN (0, 1)),
    cancellation_requested INTEGER NOT NULL DEFAULT 0 CHECK (cancellation_requested IN (0, 1)),
    automated_submission INTEGER NOT NULL DEFAULT 0 CHECK (automated_submission = 0),
    version INTEGER NOT NULL DEFAULT 1 CHECK (version >= 1),
    created_at TEXT NOT NULL CHECK (length(created_at) > 0),
    updated_at TEXT NOT NULL CHECK (length(updated_at) > 0),
    CHECK (
        (omp_process_pid IS NULL AND omp_process_pgid IS NULL AND omp_process_birth IS NULL AND omp_session_sha256 IS NULL)
        OR (omp_process_pid IS NOT NULL AND omp_process_pgid IS NOT NULL AND omp_process_birth IS NOT NULL AND omp_session_sha256 IS NOT NULL)
    ),
    CHECK (
        (state = 'review_ready' AND human_review_ready = 1 AND handoff_committed = 1)
        OR (state IN ('manual', 'blocked') AND human_review_ready = 0)
        OR (state IN ('starting', 'running', 'failed') AND human_review_ready = 0 AND handoff_committed = 0)
    ),
    CHECK (handoff_committed = 0 OR state IN ('manual', 'blocked', 'review_ready')),
    CHECK (human_review_ready = 0 OR (state = 'review_ready' AND handoff_committed = 1))
);

CREATE TABLE application_progress_events (
    run_id INTEGER NOT NULL REFERENCES application_rpc_runs(run_id),
    sequence INTEGER NOT NULL CHECK (sequence > 0),
    request_id TEXT NOT NULL REFERENCES application_rpc_requests(request_id),
    action_sequence INTEGER NOT NULL CHECK (action_sequence >= 0),
    timestamp TEXT NOT NULL CHECK (length(timestamp) > 0),
    event_type TEXT NOT NULL CHECK (event_type IN ({_RPC_EVENT_TYPE_SQL})),
    summary_code TEXT NOT NULL CHECK (summary_code IN ({_RPC_SUMMARY_SQL})),
    observation_sha256 TEXT CHECK (
        observation_sha256 IS NULL OR (
            length(observation_sha256) = 64
            AND observation_sha256 = lower(observation_sha256)
            AND observation_sha256 NOT GLOB '*[^0-9a-f]*'
        )
    ),
    PRIMARY KEY (run_id, sequence)
);
"""

RPC_INDEX_SQL = (
    "CREATE INDEX idx_rpc_requests_run_id ON application_rpc_requests(run_id)",
    "CREATE INDEX idx_rpc_requests_parent_request_id ON application_rpc_requests(parent_request_id)",
    "CREATE INDEX idx_rpc_requests_state ON application_rpc_requests(state)",
    "CREATE INDEX idx_rpc_runs_coordinator ON application_rpc_runs(coordinator_id)",
    "CREATE INDEX idx_rpc_runs_state ON application_rpc_runs(state)",
    "CREATE INDEX idx_progress_events_request ON application_progress_events(request_id)",
    "CREATE INDEX idx_progress_events_run_action ON application_progress_events(run_id, action_sequence, sequence)",
)
GENERATED_RESUMES_STATES = ("pending", "generating", "validating", "rendering", "ready", "failed", "superseded")

GENERATED_RESUMES_SCHEMA_SQL = f"""
CREATE TABLE generated_resumes (
    resume_id TEXT PRIMARY KEY,
    job_id INTEGER NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    job_snapshot_sha256 TEXT NOT NULL CHECK (
        length(job_snapshot_sha256) = 64
        AND job_snapshot_sha256 = lower(job_snapshot_sha256)
        AND job_snapshot_sha256 NOT GLOB '*[^0-9a-f]*'
    ),
    profile_sha256 TEXT NOT NULL CHECK (
        length(profile_sha256) = 64
        AND profile_sha256 = lower(profile_sha256)
        AND profile_sha256 NOT GLOB '*[^0-9a-f]*'
    ),
    source_resume_sha256 TEXT NOT NULL CHECK (
        length(source_resume_sha256) = 64
        AND source_resume_sha256 = lower(source_resume_sha256)
        AND source_resume_sha256 NOT GLOB '*[^0-9a-f]*'
    ),
    generation_config_sha256 TEXT NOT NULL CHECK (
        length(generation_config_sha256) = 64
        AND generation_config_sha256 = lower(generation_config_sha256)
        AND generation_config_sha256 NOT GLOB '*[^0-9a-f]*'
    ),
    state TEXT NOT NULL CHECK (state IN ({",".join(repr(v) for v in GENERATED_RESUMES_STATES)})),
    reason_code TEXT CHECK (
        (state <> 'failed' AND reason_code IS NULL)
        OR (state = 'failed' AND reason_code IS NOT NULL)
    ),
    artifact_dir TEXT CHECK (
        (state IN ('ready', 'superseded', 'failed') AND artifact_dir IS NOT NULL AND length(artifact_dir) > 0)
        OR (state IN ('pending', 'generating', 'validating', 'rendering') AND (artifact_dir IS NULL OR length(artifact_dir) > 0))
    ),
    completed_at TEXT CHECK (
        (state IN ('ready', 'superseded', 'failed') AND completed_at IS NOT NULL AND length(completed_at) > 0)
        OR (state IN ('pending', 'generating', 'validating', 'rendering') AND completed_at IS NULL)
    ),
    content_sha256 TEXT CHECK (
        (state IN ('ready', 'superseded') AND content_sha256 IS NOT NULL AND length(content_sha256) = 64 AND content_sha256 = lower(content_sha256) AND content_sha256 NOT GLOB '*[^0-9a-f]*')
        OR (state IN ('pending', 'generating', 'validating', 'rendering', 'failed') AND content_sha256 IS NULL)
    ),
    pdf_sha256 TEXT CHECK (
        (state IN ('ready', 'superseded') AND pdf_sha256 IS NOT NULL AND length(pdf_sha256) = 64 AND pdf_sha256 = lower(pdf_sha256) AND pdf_sha256 NOT GLOB '*[^0-9a-f]*')
        OR (state IN ('pending', 'generating', 'validating', 'rendering', 'failed') AND pdf_sha256 IS NULL)
    ),
    private_pdf_path TEXT CHECK (
        (state IN ('ready', 'superseded') AND private_pdf_path IS NOT NULL AND length(private_pdf_path) > 0)
        OR (state IN ('pending', 'generating', 'validating', 'rendering', 'failed') AND private_pdf_path IS NULL)
    ),
    created_at TEXT NOT NULL CHECK (length(created_at) > 0),
    updated_at TEXT NOT NULL CHECK (length(updated_at) > 0),
    score_json TEXT NOT NULL DEFAULT '{{}}'
)
"""

APPLICATION_RESUME_BINDINGS_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS application_resume_bindings (
    binding_id INTEGER PRIMARY KEY AUTOINCREMENT,
    resume_id TEXT NOT NULL REFERENCES generated_resumes(resume_id) ON DELETE CASCADE,
    run_id INTEGER NOT NULL REFERENCES application_runs(id) ON DELETE CASCADE,
    bound_at TEXT NOT NULL CHECK (length(bound_at) > 0),
    UNIQUE(run_id)
);
"""

GENERATED_RESUMES_INDEX_SQL = (
    "CREATE INDEX idx_generated_resumes_job_id ON generated_resumes(job_id)",
    "CREATE INDEX idx_generated_resumes_state ON generated_resumes(state)",
    "CREATE INDEX idx_generated_resumes_created ON generated_resumes(created_at DESC)",
    "CREATE UNIQUE INDEX idx_generated_resumes_ready_inputs ON generated_resumes(job_id, job_snapshot_sha256, profile_sha256, source_resume_sha256, generation_config_sha256) WHERE state = 'ready'",
)

RPC_RUN_STATES = ("starting", "running", "manual", "blocked", "review_ready", "failed")
_RPC_TERMINAL_STATES: frozenset[str] = frozenset({"review_ready", "failed"})
RPC_EVENT_TYPES = _RPC_EVENT_TYPES

LEGACY_APPLICATION_SQL = """
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
)
"""
LEGACY_INDEX_SQL = (
    "CREATE INDEX idx_application_runs_job_id ON application_runs(job_id)",
    "CREATE INDEX idx_application_runs_status ON application_runs(status)",
)
LEGACY_APPLICATION_COLUMNS = (
    "id", "job_id", "apply_url", "status", "reason", "started_at", "finished_at",
    "observation_json", "plan_json",
)
TARGET_APPLICATION_COLUMNS = (
    "id", "job_id", "apply_url", "status", "reason_code", "owner", "started_at",
    "finished_at", "observation_json", "plan_json", "artifact_dir", "session_id",
    "owner_pid", "browser_pid", "outcome", "reviewed_at",
)


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def encode_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)




def canonicalize_url(value: str | None) -> str | None:
    if not value or not value.strip():
        return None
    parsed = urlsplit(value.strip())
    if not parsed.netloc:
        return value.strip().rstrip("/") or None
    query = [
        (key, val)
        for key, val in parse_qsl(parsed.query, keep_blank_values=True)
        if not key.lower().startswith("utm_") and key.lower() not in {"gclid", "fbclid", "msclkid", "gh_src"}
    ]
    scheme = parsed.scheme.lower() or "https"
    netloc = parsed.netloc.lower()
    if scheme == "https" and netloc.endswith(":443"):
        netloc = netloc[:-4]
    path = parsed.path.rstrip("/") or "/"
    return urlunsplit((scheme, netloc, path, urlencode(query, doseq=True), ""))


def _sql_statements(sql: str) -> list[str]:
    return [statement.strip() for statement in sql.split(";") if statement.strip() and not statement.strip().upper().startswith("PRAGMA")]


# SQLite stores the spelling used by the creator in sqlite_schema.sql.  A
# fingerprint must compare the token stream, not that spelling: quoting an
# identifier, adding IF NOT EXISTS, or moving punctuation does not change the
# schema, while quoted literals and expressions remain byte-for-byte
# significant.
_SQL_KEYWORDS = frozenset(
    """
    ABORT ACTION AFTER ALL ALTER ANALYZE AND AS ASC ATTACH AUTOINCREMENT BEFORE
    BEGIN BETWEEN BY CASCADE CASE CAST CHECK COLLATE COLUMN COMMIT CONFLICT
    CONSTRAINT CREATE CROSS CURRENT CURRENT_DATE CURRENT_TIME CURRENT_TIMESTAMP
    DATABASE DEFAULT DEFERRABLE DEFERRED DELETE DESC DETACH DISTINCT DO DROP
    EACH ELSE END ESCAPE EXCEPT EXCLUDE EXCLUSIVE EXISTS EXPLAIN FAIL FILTER
    FIRST FOLLOWING FOR FOREIGN FROM FULL GENERATED GLOB GROUP GROUPS HAVING IF
    IGNORE IMMEDIATE IN INDEX INDEXED INITIALLY INNER INSERT INSTEAD INTERSECT
    INTO IS ISNULL JOIN KEY LAST LEFT LIKE LIMIT MATCH NATURAL NO NOT NOTHING
    NOTNULL NULL NULLS OF OFFSET ON OR ORDER OTHERS OUTER OVER PARTITION PLAN
    PRAGMA PRECEDING PRIMARY QUERY RAISE RANGE RECURSIVE REFERENCES REGEXP
    REINDEX RELEASE RENAME REPLACE RESTRICT RIGHT ROLLBACK ROW ROWS SAVEPOINT
    SELECT SET TABLE TEMP TEMPORARY THEN TIES TO TRANSACTION TRIGGER UNBOUNDED
    UNION UNIQUE UPDATE USING VACUUM VALUES VIEW VIRTUAL WHEN WHERE WINDOW WITH
    WITHOUT
    """.split()
)


def _sql_tokens(sql: str) -> list[tuple[str, str]]:
    """Lex enough SQLite SQL to preserve semantics while ignoring cosmetics."""
    tokens: list[tuple[str, str]] = []
    i = 0
    operators = (
        "->>",  # longest operators must be checked before their prefixes
        "||",
        "->",
        "<<",
        ">>",
        "<=",
        ">=",
        "<>",
        "!=",
        "==",
        "&",
        "|",
        "+",
        "-",
        "*",
        "/",
        "%",
        "=",
        "<",
        ">",
        "~",
        "^",
        "!",
        "?",
        ":",
    )
    punctuation = frozenset("(),.;")
    while i < len(sql):
        char = sql[i]
        if char.isspace():
            i += 1
            continue
        if char == "-" and i + 1 < len(sql) and sql[i + 1] == "-":
            newline = sql.find("\n", i + 2)
            i = len(sql) if newline < 0 else newline + 1
            continue
        if char == "/" and i + 1 < len(sql) and sql[i + 1] == "*":
            end = sql.find("*/", i + 2)
            if end < 0:
                raise ValueError("unterminated SQL block comment")
            i = end + 2
            continue
        if char in "'\"`[":
            close = "]" if char == "[" else char
            j = i + 1
            while j < len(sql):
                if sql[j] == close:
                    if close != "]" and j + 1 < len(sql) and sql[j + 1] == close:
                        j += 2
                        continue
                    break
                j += 1
            if j >= len(sql):
                raise ValueError("unterminated SQL quoted token")
            raw = sql[i : j + 1]
            if char == "'":
                tokens.append(("literal", raw))
            else:
                inner = raw[1:-1].replace(close * 2, close).lower()
                tokens.append(("word", inner))
            i = j + 1
            continue
        if char.isalpha() or char == "_":
            j = i + 1
            while j < len(sql) and (sql[j].isalnum() or sql[j] in "_$"):
                j += 1
            word = sql[i:j]
            kind = "keyword" if word.upper() in _SQL_KEYWORDS else "word"
            tokens.append((kind, word.upper() if kind == "keyword" else word.lower()))
            i = j
            continue
        if char.isdigit() or (char == "." and i + 1 < len(sql) and sql[i + 1].isdigit()):
            j = i + 1
            while j < len(sql) and (sql[j].isalnum() or sql[j] in "._"):
                j += 1
            if j < len(sql) and sql[j] in "+-" and j > i and sql[j - 1] in "eE":
                j += 1
                while j < len(sql) and sql[j].isdigit():
                    j += 1
            tokens.append(("number", sql[i:j].lower()))
            i = j
            continue
        operator = next((candidate for candidate in operators if sql.startswith(candidate, i)), None)
        if operator is not None:
            tokens.append(("operator", operator))
            i += len(operator)
            continue
        if char in punctuation:
            tokens.append(("punct", char))
            i += 1
            continue
        raise ValueError(f"unsupported SQL character at offset {i}")
    return tokens


def _canonicalize_sql(sql: str | None) -> str:
    tokens = _sql_tokens(str(sql or ""))
    # IF NOT EXISTS is grammar sugar and has no bearing on the resulting
    # object.  Restrict removal to the CREATE clause, never CHECK/default
    # expressions.
    if tokens and tokens[0] == ("keyword", "CREATE"):
        for index in range(1, min(len(tokens) - 2, 8)):
            before = [value for kind, value in tokens[1:index] if kind == "keyword"]
            if (
                tokens[index : index + 3] == [
                    ("keyword", "IF"),
                    ("keyword", "NOT"),
                    ("keyword", "EXISTS"),
                ]
                and any(value in {"TABLE", "INDEX"} for value in before)
            ):
                del tokens[index : index + 3]
                break
    # Typed tokens avoid ambiguity and preserve quoted literals exactly.
    encoded = []
    for kind, value in tokens:
        prefix = {
            "keyword": "K",
            "word": "I",
            "literal": "L",
            "number": "N",
            "operator": "O",
            "punct": "P",
        }[kind]
        encoded.append(f"{prefix}:{value}")
    return "|".join(encoded)






def _open_dir_component(parent_fd: int, name: str, *, create: bool) -> int:
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0)
    if create:
        try:
            os.mkdir(name, 0o700, dir_fd=parent_fd)
        except FileExistsError:
            pass
    fd = os.open(name, flags, dir_fd=parent_fd)
    st = os.fstat(fd)
    if not stat.S_ISDIR(st.st_mode):
        os.close(fd)
        raise PermissionError(f"SQLite path component is not a directory: {name}")
    return fd

def _validate_sqlite_parent_directory(st: os.stat_result) -> None:
    """Require user-controlled SQLite parents to be owner-private.

    System-owned roots remain traversable, and the conventional root-owned
    sticky temporary directory (for example ``/tmp``) is an intentional
    boundary for per-user temporary paths.
    """
    mode = stat.S_IMODE(st.st_mode)
    if st.st_uid == 0:
        if mode & stat.S_ISVTX:
            return
        if mode & (stat.S_IWGRP | stat.S_IWOTH):
            raise PermissionError("unsafe writable SQLite ancestor")
        return
    if st.st_uid != os.geteuid():
        raise PermissionError("SQLite parent must be owned by current user")
    if mode & 0o077:
        raise PermissionError("SQLite parent directory must be owner-private")


def _validate_sqlite_ancestor_directory(st: os.stat_result) -> None:
    mode = stat.S_IMODE(st.st_mode)
    # Only a root-owned sticky temporary boundary may be writable by others.
    if st.st_uid == 0 and mode & stat.S_ISVTX:
        return
    if mode & (stat.S_IWGRP | stat.S_IWOTH):
        raise PermissionError("unsafe writable SQLite ancestor")


def _secure_prepare_sqlite_path(db_path: Path, *, create: bool = True) -> None:
    """Validate and optionally prepare a SQLite path without following path symlinks."""
    raw = os.fspath(db_path)
    uid = os.geteuid()
    if raw == ":memory:":
        return
    absolute = os.path.isabs(raw)
    raw_components = raw.split(os.sep)
    if ".." in raw_components:
        raise PermissionError("SQLite path may not contain parent traversal")
    components = [part for part in raw_components if part not in ("", ".")]
    current_fd = os.open("/" if absolute else ".", os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0))
    opened = [current_fd]
    try:
        for component in components[:-1]:
            _validate_sqlite_ancestor_directory(os.fstat(current_fd))
            child_fd = _open_dir_component(current_fd, component, create=create)
            opened.append(child_fd)
            current_fd = child_fd
        parent = os.fstat(current_fd)
        _validate_sqlite_parent_directory(parent)
        for ancestor_fd in opened[:-1]:
            _validate_sqlite_ancestor_directory(os.fstat(ancestor_fd))
        filename = components[-1] if components else ""
        if not filename:
            raise PermissionError("SQLite path must name a database file")
        created = False
        st: os.stat_result | None = None
        try:
            st = os.stat(filename, dir_fd=current_fd, follow_symlinks=False)
        except FileNotFoundError:
            if not create:
                raise
            try:
                fd = os.open(filename, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0), 0o600, dir_fd=current_fd)
                os.close(fd)
                created = True
            except FileExistsError:
                st = os.stat(filename, dir_fd=current_fd, follow_symlinks=False)
        if not created:
            if st is None or not stat.S_ISREG(st.st_mode) or st.st_uid != uid:
                raise PermissionError("SQLite file must be a regular file owned by current user")
            if stat.S_IMODE(st.st_mode) & 0o077:
                raise PermissionError("SQLite file must be owner-private")
        for suffix in ("-wal", "-shm", "-journal"):
            sidecar = f"{filename}{suffix}"
            try:
                st = os.stat(sidecar, dir_fd=current_fd, follow_symlinks=False)
            except FileNotFoundError:
                continue
            if not stat.S_ISREG(st.st_mode) or st.st_uid != uid:
                raise PermissionError(f"SQLite sidecar is unsafe: {sidecar}")
            if stat.S_IMODE(st.st_mode) & 0o077:
                raise PermissionError(f"SQLite sidecar must be owner-private: {sidecar}")
    finally:
        for fd in reversed(opened):
            os.close(fd)


def connect(db_path: str | Path) -> sqlite3.Connection:
    if str(db_path) != ":memory:":
        _secure_prepare_sqlite_path(Path(db_path))
    connection = sqlite3.connect(str(db_path), timeout=30.0)
    try:
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        if str(db_path) != ":memory:":
            _secure_prepare_sqlite_path(Path(db_path))
    except BaseException:
        connection.close()
        raise
    return connection


def connect_read_only(db_path: str | Path) -> sqlite3.Connection:
    """Open an existing SQLite file without permitting writes."""
    if str(db_path) == ":memory:":
        raise sqlite3.OperationalError("read-only SQLite connection requires an existing file")
    path = Path(db_path)
    _secure_prepare_sqlite_path(path, create=False)
    uri = f"{path.absolute().as_uri()}?mode=ro"
    connection = sqlite3.connect(uri, uri=True, timeout=30.0)
    try:
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA query_only=ON")
        _secure_prepare_sqlite_path(path, create=False)
    except BaseException:
        connection.close()
        raise
    return connection


def _application_columns(connection: sqlite3.Connection) -> list[str]:
    return [str(row["name"]) for row in connection.execute("PRAGMA table_xinfo(application_runs)").fetchall()]


def _application_table_exists(connection: sqlite3.Connection) -> bool:
    return connection.execute("SELECT 1 FROM sqlite_schema WHERE type='table' AND name='application_runs'").fetchone() is not None

def _sequence_semantics(connection: sqlite3.Connection) -> dict[str, Any]:
    table = connection.execute("SELECT 1 FROM sqlite_schema WHERE name='sqlite_sequence' AND type='table'").fetchone() is not None
    row = connection.execute("SELECT seq FROM sqlite_sequence WHERE name='application_runs'").fetchone() if table else None
    maximum = connection.execute("SELECT COALESCE(MAX(id), 0) FROM application_runs").fetchone()[0] if _application_table_exists(connection) else 0
    seq = int(row["seq"]) if row and row["seq"] is not None else None
    valid = (int(maximum or 0) == 0 and seq in (None, 0)) or (seq is not None and seq >= int(maximum or 0))
    return {"table": table, "row": seq, "max_id": int(maximum or 0), "valid": valid}


def init_db(connection: sqlite3.Connection) -> None:
    """Initialize only the ingestion/backlog schema.

    Application-run schema creation and migration intentionally stay behind
    ``initialize_database`` because that operation requires a private
    ArtifactRoot and its migration transaction.
    """
    connection.execute("BEGIN IMMEDIATE")
    try:
        _ensure_core_schema(connection)
    except Exception:
        connection.rollback()
        raise
    connection.commit()


def application_schema_fingerprint(connection: sqlite3.Connection) -> dict[str, Any]:
    sql_row = connection.execute("SELECT sql FROM sqlite_schema WHERE type='table' AND name='application_runs'").fetchone()
    indexes: dict[str, Any] = {}
    for row in connection.execute("SELECT name, sql FROM sqlite_schema WHERE type='index' AND tbl_name='application_runs' ORDER BY name").fetchall():
        name = str(row["name"])
        listed = connection.execute("PRAGMA index_list(application_runs)").fetchall()
        listing = next((item for item in listed if item["name"] == name), None)
        columns = [dict(item) for item in connection.execute(f'PRAGMA index_xinfo("{name.replace(chr(34), chr(34) * 2)}")').fetchall()]
        indexes[name] = {"sql": _canonicalize_sql(row["sql"]), "unique": int(listing["unique"]) if listing else 0, "partial": int(listing["partial"]) if listing else 0, "columns": columns}
    sequence_semantics = _sequence_semantics(connection)
    return {
        "columns": _application_columns(connection),
        "table_sql": _canonicalize_sql(sql_row["sql"] if sql_row else ""),
        "xinfo": [dict(row) for row in connection.execute("PRAGMA table_xinfo(application_runs)").fetchall()],
        "foreign_keys": [dict(row) for row in connection.execute("PRAGMA foreign_key_list(application_runs)").fetchall()],
        "indexes": indexes,
        "triggers": [dict(row) for row in connection.execute("SELECT name, sql FROM sqlite_schema WHERE type='trigger' AND tbl_name='application_runs' ORDER BY name").fetchall()],
        "sequence": sequence_semantics.get("row"),
        "sequence_semantics": sequence_semantics,
    }


def _expected_application_fingerprint(*, legacy: bool = False) -> dict[str, Any]:
    probe = sqlite3.connect(":memory:")
    probe.row_factory = sqlite3.Row
    try:
        probe.execute("CREATE TABLE jobs (id INTEGER PRIMARY KEY AUTOINCREMENT)")
        sql = LEGACY_APPLICATION_SQL if legacy else APPLICATION_SCHEMA_SQL
        for statement in _sql_statements(sql):
            probe.execute(statement)
        for statement in LEGACY_INDEX_SQL if legacy else APPLICATION_INDEX_SQL:
            probe.execute(statement)
        return application_schema_fingerprint(probe)
    finally:
        probe.close()


def _schema_matches(actual: dict[str, Any], expected: dict[str, Any]) -> bool:
    for key in ("columns", "table_sql", "xinfo", "foreign_keys", "indexes", "triggers"):
        if actual[key] != expected[key]:
            return False
    semantics = actual["sequence_semantics"]
    return bool(semantics["valid"] and semantics["table"] == expected["sequence_semantics"]["table"])


def _is_target_application_schema(connection: sqlite3.Connection) -> bool:
    return _schema_matches(application_schema_fingerprint(connection), _expected_application_fingerprint())


def _is_legacy_application_schema(connection: sqlite3.Connection) -> bool:
    return _schema_matches(application_schema_fingerprint(connection), _expected_application_fingerprint(legacy=True))


def _rpc_table_exists(connection: sqlite3.Connection, name: str) -> bool:
    return connection.execute(
        "SELECT 1 FROM sqlite_schema WHERE type='table' AND name=?", (name,)
    ).fetchone() is not None


def rpc_schema_fingerprint(connection: sqlite3.Connection) -> dict[str, Any]:
    """Return a structural fingerprint of all three RPC adjunct tables."""
    tables: dict[str, Any] = {}
    for table_name in ("application_rpc_requests", "application_rpc_runs", "application_progress_events"):
        sql_row = connection.execute(
            "SELECT sql FROM sqlite_schema WHERE type='table' AND name=?", (table_name,)
        ).fetchone()
        indexes: dict[str, Any] = {}
        for idx_row in connection.execute(
            "SELECT name, sql FROM sqlite_schema WHERE type='index' AND tbl_name=? ORDER BY name",
            (table_name,),
        ).fetchall():
            name = str(idx_row["name"])
            listed = connection.execute(f"PRAGMA index_list({table_name})").fetchall()
            listing = next((item for item in listed if item["name"] == name), None)
            columns = [
                dict(item)
                for item in connection.execute(
                    f'PRAGMA index_xinfo("{name.replace(chr(34), chr(34) * 2)}")'
                ).fetchall()
            ]
            indexes[name] = {
                "sql": _canonicalize_sql(idx_row["sql"]),
                "unique": int(listing["unique"]) if listing else 0,
                "partial": int(listing["partial"]) if listing else 0,
                "columns": columns,
            }
        tables[table_name] = {
            "table_sql": _canonicalize_sql(sql_row["sql"] if sql_row else ""),
            "xinfo": [dict(row) for row in connection.execute(f"PRAGMA table_xinfo({table_name})").fetchall()],
            "foreign_keys": [dict(row) for row in connection.execute(f"PRAGMA foreign_key_list({table_name})").fetchall()],
            "indexes": indexes,
            "triggers": [
                dict(row)
                for row in connection.execute(
                    "SELECT name, sql FROM sqlite_schema WHERE type='trigger' AND tbl_name=? ORDER BY name",
                    (table_name,),
                ).fetchall()
            ],
        }
    return {"tables": tables}


def _expected_rpc_fingerprint() -> dict[str, Any]:
    probe = sqlite3.connect(":memory:")
    probe.row_factory = sqlite3.Row
    try:
        probe.execute("CREATE TABLE jobs (id INTEGER PRIMARY KEY AUTOINCREMENT)")
        probe.execute(APPLICATION_SCHEMA_SQL)
        for statement in _sql_statements(RPC_SCHEMA_SQL):
            probe.execute(statement)
        for statement in RPC_INDEX_SQL:
            probe.execute(statement)
        return rpc_schema_fingerprint(probe)
    finally:
        probe.close()


def _rpc_schema_matches(actual: dict[str, Any], expected: dict[str, Any]) -> bool:
    for table_name in ("application_rpc_requests", "application_rpc_runs", "application_progress_events"):
        a = actual["tables"].get(table_name, {})
        e = expected["tables"].get(table_name, {})
        for key in ("table_sql", "xinfo", "foreign_keys", "indexes", "triggers"):
            if a.get(key) != e.get(key):
                return False
    return True


def _is_target_rpc_schema(connection: sqlite3.Connection) -> bool:
    if not all(
        _rpc_table_exists(connection, name)
        for name in ("application_rpc_requests", "application_rpc_runs", "application_progress_events")
    ):
        return False
    return _rpc_schema_matches(rpc_schema_fingerprint(connection), _expected_rpc_fingerprint())


def _ensure_rpc_schema(connection: sqlite3.Connection) -> None:
    """Create RPC adjunct tables if absent; validate fingerprint if present."""
    rpc_exists = _rpc_table_exists(connection, "application_rpc_requests")
    if rpc_exists:
        if not _is_target_rpc_schema(connection):
            raise RuntimeError("rpc adjunct schema fingerprint mismatch")
        return
    for statement in _sql_statements(RPC_SCHEMA_SQL):
        connection.execute(statement)
    for statement in RPC_INDEX_SQL:
        connection.execute(statement)
    if not _is_target_rpc_schema(connection):
        raise RuntimeError("rpc adjunct schema initialization fingerprint mismatch")
def _generated_resumes_table_exists(connection: sqlite3.Connection) -> bool:
    return connection.execute(
        "SELECT 1 FROM sqlite_schema WHERE type='table' AND name='generated_resumes'"
    ).fetchone() is not None


def generated_resumes_schema_fingerprint(connection: sqlite3.Connection) -> dict[str, Any]:
    sql_row = connection.execute(
        "SELECT sql FROM sqlite_schema WHERE type='table' AND name='generated_resumes'"
    ).fetchone()
    indexes: dict[str, Any] = {}
    for idx_row in connection.execute(
        "SELECT name, sql FROM sqlite_schema WHERE type='index' AND tbl_name='generated_resumes' ORDER BY name"
    ).fetchall():
        name = str(idx_row["name"])
        listed = connection.execute("PRAGMA index_list(generated_resumes)").fetchall()
        listing = next((item for item in listed if item["name"] == name), None)
        columns = [
            dict(item)
            for item in connection.execute(
                f'PRAGMA index_xinfo("{name.replace(chr(34), chr(34) * 2)}")'
            ).fetchall()
        ]
        indexes[name] = {
            "sql": _canonicalize_sql(idx_row["sql"]),
            "unique": int(listing["unique"]) if listing else 0,
            "partial": int(listing["partial"]) if listing else 0,
            "columns": columns,
        }
    return {
        "table_sql": _canonicalize_sql(sql_row["sql"] if sql_row else ""),
        "xinfo": [dict(row) for row in connection.execute("PRAGMA table_xinfo(generated_resumes)").fetchall()],
        "foreign_keys": [dict(row) for row in connection.execute("PRAGMA foreign_key_list(generated_resumes)").fetchall()],
        "indexes": indexes,
    }


def _expected_generated_resumes_fingerprint() -> dict[str, Any]:
    probe = sqlite3.connect(":memory:")
    probe.row_factory = sqlite3.Row
    try:
        probe.execute("CREATE TABLE jobs (id INTEGER PRIMARY KEY AUTOINCREMENT)")
        for statement in _sql_statements(GENERATED_RESUMES_SCHEMA_SQL):
            probe.execute(statement)
        for statement in GENERATED_RESUMES_INDEX_SQL:
            probe.execute(statement)
        return generated_resumes_schema_fingerprint(probe)
    finally:
        probe.close()


def _is_target_generated_resumes_schema(connection: sqlite3.Connection) -> bool:
    if not _generated_resumes_table_exists(connection):
        return False
    actual = generated_resumes_schema_fingerprint(connection)
    expected = _expected_generated_resumes_fingerprint()
    for key in ("table_sql", "xinfo", "foreign_keys", "indexes"):
        if actual.get(key) != expected.get(key):
            return False
    return True


def _ensure_generated_resumes_schema(connection: sqlite3.Connection) -> None:
    """Create generated_resumes adjunct table if absent; validate fingerprint if present."""
    exists = _generated_resumes_table_exists(connection)
    if exists:
        if not _is_target_generated_resumes_schema(connection):
            raise RuntimeError("generated_resumes adjunct schema fingerprint mismatch")
        for statement in _sql_statements(APPLICATION_RESUME_BINDINGS_SCHEMA_SQL):
            connection.execute(statement)
        return
    for statement in _sql_statements(GENERATED_RESUMES_SCHEMA_SQL):
        connection.execute(statement)
    for statement in GENERATED_RESUMES_INDEX_SQL:
        connection.execute(statement)
    for statement in _sql_statements(APPLICATION_RESUME_BINDINGS_SCHEMA_SQL):
        connection.execute(statement)
    if not _is_target_generated_resumes_schema(connection):
        raise RuntimeError("generated_resumes adjunct schema initialization fingerprint mismatch")


class RpcDeadlineExceeded(RuntimeError):
    """A durable RPC transition reached SQLite after its deadline."""


def _rpc_deadline_expired(deadline_unix_ms: int | None) -> bool:
    if deadline_unix_ms is None:
        return False
    if type(deadline_unix_ms) is not int or deadline_unix_ms <= 0:
        raise TypeError("deadline_unix_ms must be a positive integer")
    return int(time.time() * 1000) >= deadline_unix_ms


def _require_rpc_deadline_live(deadline_unix_ms: int | None) -> None:
    if _rpc_deadline_expired(deadline_unix_ms):
        raise RpcDeadlineExceeded("RPC transition deadline exceeded")


def _rollback_expired_rpc_transition(
    connection: sqlite3.Connection,
    deadline_unix_ms: int | None,
) -> None:
    _require_rpc_deadline_live(deadline_unix_ms)


@dataclass(frozen=True)
class RpcClaimOutcome:
    outcome: Literal["new", "pending", "completed", "conflict", "unavailable"]
    run_id: int | None
    request_id: str
    coordinator_id: str
    claim: ApplicationClaim | None


@dataclass(frozen=True)
class RpcRequestInfo:
    request_id: str
    protocol_version: int
    operation: str
    semantic_sha256: str
    request_json: str
    run_id: int | None
    parent_request_id: str | None
    state: Literal["pending", "completed"]
    response_json: str | None
    created_at: str
    completed_at: str | None
    created: bool = False


@dataclass(frozen=True)
class RpcRunStatus:
    run_id: int
    coordinator_id: str
    state: str
    reason_code: str | None
    ats_policy: str | None
    apply_url: str
    job_url: str
    last_observation_sha256: str | None
    artifact_manifest_sha256: str | None
    action_sequence: int
    current_form_step: str | None
    human_review_ready: bool
    handoff_committed: bool
    cancellation_requested: bool
    automated_submission: bool
    version: int
    created_at: str
    updated_at: str
    latest_event_sequence: int = 0
    resume_eligible: bool = False


@dataclass(frozen=True)
class RpcEventInfo:
    run_id: int
    sequence: int
    request_id: str
    action_sequence: int
    timestamp: str
    event_type: str
    summary_code: str
    observation_sha256: str | None


@dataclass(frozen=True)
class RpcRunTransition:
    """One validated, event-backed non-proposal RPC run transition."""

    run_id: int
    coordinator_id: str
    request_id: str
    action_sequence: int
    event_type: str
    summary_code: str
    state: str | None = None
    ats_policy: str | None = None
    current_form_step: str | None = None
    observation_sha256: str | None = None
    manifest_sha256: str | None = None
    human_review_ready: bool | None = None
    handoff_committed: bool | None = None
    coordinator_pid: int | None = None
    coordinator_pgid: int | None = None
    coordinator_birth: str | None = None


@dataclass(frozen=True)
class RpcReconciliationResult:
    status: Literal["reconciled", "partial", "noop", "conflict"]
    run_ids: tuple[int, ...] = ()
    event_sequences: tuple[tuple[int, int], ...] = ()
    conflict_run_ids: tuple[int, ...] = ()


@dataclass(frozen=True)
class RpcHandoffRecoveryResult:
    status: Literal["recovered", "partial", "noop", "conflict"]
    run_ids: tuple[int, ...] = ()
    conflict_run_ids: tuple[int, ...] = ()
def _ensure_core_schema(connection: sqlite3.Connection) -> None:
    for statement in _sql_statements(SCHEMA_SQL):
        connection.execute(statement)
    columns = {row["name"] for row in connection.execute("PRAGMA table_xinfo(sync_runs)").fetchall()}
    if "checkpoint" not in columns:
        connection.execute("ALTER TABLE sync_runs ADD COLUMN checkpoint TEXT")


def _database_identity(connection: sqlite3.Connection) -> str:
    row = connection.execute("PRAGMA database_list").fetchone()
    locator = str(row[2]) if row and row[2] else ""
    if locator:
        try:
            identity = os.stat(locator, follow_symlinks=False)
            token = f"{identity.st_dev}:{identity.st_ino}:{identity.st_uid}"
        except OSError as exc:
            raise RuntimeError("database identity unavailable") from exc
    else:
        token = f"memory:{id(connection)}"
    return hashlib.sha256(token.encode("ascii", "strict")).hexdigest()


def _bind_artifact_root(connection: sqlite3.Connection, root: ArtifactRoot, *, create: bool = True) -> None:
    """Bind an artifact root to one secure SQLite file identity."""
    try:
        root_fd = root._require_fd()
    except Exception as exc:
        raise RuntimeError("artifact root is unavailable") from exc
    expected = _database_identity(connection).encode("ascii")
    name = ".database-identity"
    read_flags = os.O_RDONLY | os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(name, read_flags, dir_fd=root_fd)
    except FileNotFoundError:
        if not create:
            raise RuntimeError("artifact root is not bound to this database") from None
        temp_name = f".{name}.{os.urandom(16).hex()}.tmp"
        fd = os.open(
            temp_name,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0),
            0o600,
            dir_fd=root_fd,
        )
        try:
            try:
                os.fchmod(fd, 0o600)
                _artifacts._write_all(fd, expected)
                os.fsync(fd)
            except Exception:
                try:
                    os.unlink(temp_name, dir_fd=root_fd)
                except FileNotFoundError:
                    pass
                raise
        finally:
            os.close(fd)
        try:
            os.link(temp_name, name, src_dir_fd=root_fd, dst_dir_fd=root_fd, follow_symlinks=False)
        except FileExistsError:
            os.unlink(temp_name, dir_fd=root_fd)
            fd = os.open(name, read_flags, dir_fd=root_fd)
        except Exception:
            try:
                os.unlink(temp_name, dir_fd=root_fd)
            except FileNotFoundError:
                pass
            raise
        else:
            try:
                os.unlink(temp_name, dir_fd=root_fd)
                dir_fd = os.open(".", os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC, dir_fd=root_fd)
                try:
                    os.fsync(dir_fd)
                finally:
                    os.close(dir_fd)
            except Exception:
                raise
            return
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_uid != os.geteuid() or stat.S_IMODE(st.st_mode) & 0o077:
            raise RuntimeError("artifact root metadata is unsafe")
        actual = os.read(fd, len(expected) + 1)
    finally:
        os.close(fd)
    if actual != expected:
        raise RuntimeError("artifact root is bound to another database")


def _artifact_run_at_root(root: ArtifactRoot, run_id: int) -> Any:
    name = f"legacy-run-{int(run_id)}"
    fd = _artifacts._open_private_child_dir(root._require_fd(), name)
    return _artifacts.ArtifactRun(root, name, fd)


_MIGRATION_STATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS application_migration_state (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
)
"""
_WAL_CHECKPOINT_STATE_KEY = "legacy_wal_checkpoint"


def _checkpoint_pending(connection: sqlite3.Connection) -> bool:
    table = connection.execute(
        "SELECT 1 FROM sqlite_schema WHERE type='table' AND name='application_migration_state'"
    ).fetchone()
    if table is None:
        return False
    row = connection.execute(
        "SELECT value FROM application_migration_state WHERE key=?",
        (_WAL_CHECKPOINT_STATE_KEY,),
    ).fetchone()
    return bool(row is not None and row["value"] == "pending")


def _set_checkpoint_pending(connection: sqlite3.Connection) -> None:
    connection.execute(_MIGRATION_STATE_TABLE_SQL)
    connection.execute(
        "INSERT INTO application_migration_state(key, value) VALUES (?, 'pending') "
        "ON CONFLICT(key) DO UPDATE SET value='pending'",
        (_WAL_CHECKPOINT_STATE_KEY,),
    )


def _clear_checkpoint_pending(connection: sqlite3.Connection) -> None:
    connection.execute("BEGIN IMMEDIATE")
    try:
        connection.execute(
            "DELETE FROM application_migration_state WHERE key=?",
            (_WAL_CHECKPOINT_STATE_KEY,),
        )
    except Exception:
        connection.rollback()
        raise
    connection.commit()


def _checkpoint_wal(connection: sqlite3.Connection) -> None:
    try:
        result = connection.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
    except sqlite3.DatabaseError as exc:
        raise RuntimeError("WAL checkpoint failed") from exc
    if result is not None and int(result[0]) != 0:
        raise RuntimeError("WAL checkpoint busy")


def _erase_persistent_journal(connection: sqlite3.Connection) -> None:
    mode_row = connection.execute("PRAGMA journal_mode").fetchone()
    mode = str(mode_row[0]).lower() if mode_row else ""
    if mode != "persist":
        return
    db_row = connection.execute("PRAGMA database_list").fetchone()
    locator = str(db_row[2]) if db_row and db_row[2] else ""
    if not locator:
        return
    journal_path = f"{locator}-journal"
    try:
        fd = os.open(journal_path, os.O_RDWR | os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0))
    except FileNotFoundError:
        return
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_uid != os.geteuid() or stat.S_IMODE(st.st_mode) & 0o077:
            raise RuntimeError("SQLite journal is unsafe")
        size = st.st_size
        zero = b"\0" * min(65536, max(1, size))
        offset = 0
        while offset < size:
            written = os.pwrite(fd, zero[: min(len(zero), size - offset)], offset)
            if written <= 0:
                raise RuntimeError("SQLite journal erase made no progress")
            offset += written
        os.fsync(fd)
        os.ftruncate(fd, 0)
        os.fsync(fd)
    finally:
        os.close(fd)


def _legacy_artifact_run(root: ArtifactRoot, run_id: int) -> Any:
    return _artifact_run_at_root(root, run_id)


def _artifact_ref_for_run(root: ArtifactRoot, run_id: int) -> str:
    return f"legacy-run-{int(run_id)}"


def initialize_database(
    connection: sqlite3.Connection,
    *,
    migration_artifact_root: ArtifactRoot,
    expected_coordinator_id: str | None = None,
) -> None:
    if expected_coordinator_id is not None:
        expected_coordinator_id = _require_rpc_coordinator_id(expected_coordinator_id)
    if not isinstance(migration_artifact_root, ArtifactRoot):
        raise TypeError("migration_artifact_root must be an ArtifactRoot")
    # Lock before any schema read.  This makes classification and all DDL a
    # single serialized transaction across initializers.
    connection.execute("BEGIN IMMEDIATE")
    needs_checkpoint = False
    pending_checkpoint = False
    try:
        application_exists = _application_table_exists(connection)
        is_target = application_exists and _is_target_application_schema(connection)
        is_legacy = application_exists and _is_legacy_application_schema(connection)
        if application_exists and not (is_target or is_legacy):
            raise RuntimeError("unknown application_runs schema")
        _bind_artifact_root(connection, migration_artifact_root)
        pending_checkpoint = _checkpoint_pending(connection)
        if is_legacy:
            connection.execute("PRAGMA secure_delete=ON")
            if int(connection.execute("PRAGMA secure_delete").fetchone()[0]) != 1:
                raise RuntimeError("secure_delete is required for legacy migration")
            _set_checkpoint_pending(connection)
            needs_checkpoint = True
        _ensure_core_schema(connection)
        if not application_exists:
            connection.execute(APPLICATION_SCHEMA_SQL)
            for statement in APPLICATION_INDEX_SQL:
                connection.execute(statement)
        elif is_legacy:
            _migrate_legacy_application_runs(connection, migration_artifact_root)
        if not _is_target_application_schema(connection):
            raise RuntimeError("application_runs migration fingerprint mismatch")
        _ensure_rpc_schema(connection)
        _ensure_generated_resumes_schema(connection)
    except Exception:
        connection.rollback()
        raise
    connection.commit()
    if needs_checkpoint or pending_checkpoint:
        try:
            _checkpoint_wal(connection)
        except Exception as exc:
            # The migration transaction has already committed.  The durable
            # marker makes a later initializer retry rather than reporting a
            # rollback for an irreversible schema/data conversion.
            raise RuntimeError("legacy migration committed; WAL checkpoint pending") from exc
        try:
            _erase_persistent_journal(connection)
        except Exception as exc:
            raise RuntimeError("legacy migration journal erase failed") from exc
        try:
            _clear_checkpoint_pending(connection)
        except Exception as exc:
            raise RuntimeError("WAL checkpoint completed; pending marker clear failed") from exc
    # Recovery must precede generic abandoned-run reconciliation.  The latter
    # deliberately excludes every row with a bound handoff intent.
    recovery_result = recover_rpc_handoffs(
        connection,
        artifact_root=migration_artifact_root,
        expected_coordinator_id=expected_coordinator_id,
    )
    if recovery_result.conflict_run_ids:
        raise RuntimeError(
            "rpc handoff recovery conflict: "
            + ",".join(str(run_id) for run_id in recovery_result.conflict_run_ids)
        )
    reconciliation_result = reconcile_abandoned_rpc_runs(
        connection,
        expected_coordinator_id=expected_coordinator_id,
    )
    if reconciliation_result.conflict_run_ids:
        raise RuntimeError(
            "rpc abandoned-run reconciliation conflict: "
            + ",".join(
                str(run_id) for run_id in reconciliation_result.conflict_run_ids
            )
        )
def _require_rpc_request_id(value: Any) -> str:
    if type(value) is not str:
        raise TypeError("request_id must be a canonical UUID")
    try:
        if str(UUID(value)) != value:
            raise ValueError
    except (ValueError, AttributeError):
        raise ValueError("request_id must be a canonical lowercase UUID") from None
    return value


def _require_rpc_coordinator_id(value: Any) -> str:
    if type(value) is not str or not value or len(value) > 256:
        raise TypeError("coordinator_id must be a non-empty string <= 256 chars")
    if any(ord(c) < 0x20 or ord(c) > 0x7E for c in value):
        raise ValueError("coordinator_id must be printable ASCII")
    return value


def _require_rpc_operation(value: Any) -> str:
    if type(value) is not str or value not in APPLICATION_OPERATIONS:
        raise ValueError(f"operation must be one of {APPLICATION_OPERATIONS}")
    return value


def _require_rpc_protocol_version(value: Any) -> int:
    if type(value) is not int or value != APPLICATION_RPC_PROTOCOL_VERSION:
        raise TypeError("protocol_version must be the integer 1")
    return value


def _require_rpc_sha256(value: Any, name: str) -> str:
    if type(value) is not str or len(value) != 64:
        raise TypeError(f"{name} must be a 64-char hex string")
    if any(c not in "0123456789abcdef" for c in value):
        raise ValueError(f"{name} must be lowercase hex")
    return value


def _require_rpc_sha256_or_none(value: Any, name: str) -> str | None:
    if value is None:
        return None
    return _require_rpc_sha256(value, name)


def _require_rpc_event_type(value: Any) -> str:
    if type(value) is not str or value not in RPC_EVENT_TYPES:
        raise ValueError(f"event_type must be one of {RPC_EVENT_TYPES}")
    return value


def _require_rpc_summary_code(value: Any) -> str:
    if type(value) is not str or value not in _RPC_EVENT_SUMMARY_CODES:
        raise ValueError("summary_code is not an allowlisted low-entropy code")
    return value


def _require_rpc_form_step(value: Any) -> str | None:
    if value is None:
        return None
    if type(value) is not str or not value or len(value) > 256:
        raise TypeError("current_form_step must be a non-empty string <= 256 chars")
    if any(ord(c) < 0x20 or ord(c) > 0x7E for c in value):
        raise ValueError("current_form_step must be printable ASCII")
    return value


def _require_rpc_ats_policy(value: Any) -> str | None:
    if value is None:
        return None
    if type(value) is not str or value not in SUPPORTED_ATS_POLICIES:
        raise ValueError(f"ats_policy must be one of {SUPPORTED_ATS_POLICIES}")
    return value


def _require_rpc_action_sequence(value: Any) -> int:
    if type(value) is not int or value < 0:
        raise TypeError("action_sequence must be a non-negative integer")
    return value


def _require_rpc_positive_int(value: Any, name: str) -> int:
    if type(value) is not int or value <= 0:
        raise TypeError(f"{name} must be a positive integer")
    return value


def rpc_provisional_session_sha256(pid: int, pgid: int, birth: str) -> str:
    """Return the opaque startup session token for an exact OMP identity."""
    pid = _require_rpc_positive_int(pid, "pid")
    pgid = _require_rpc_positive_int(pgid, "pgid")
    if type(birth) is not str or not birth or len(birth) > 256:
        raise TypeError("birth must be a non-empty bounded string")
    payload = f"jobs-assistant:omp-provisional:v1\0{pid}\0{pgid}\0{birth}".encode(
        "utf-8", "surrogatepass"
    )
    return hashlib.sha256(payload).hexdigest()


def _capture_rpc_coordinator_identity() -> dict[str, Any] | None:
    """Capture this interpreter's coordinator identity without test seams."""
    pid = os.getpid()
    try:
        pgid = os.getpgid(pid)
    except (OSError, ProcessLookupError, PermissionError):
        return None
    birth = _process_birth_token(pid)
    if type(pgid) is not int or pgid <= 0 or type(birth) is not str or not birth:
        return None
    return {"pid": pid, "pgid": pgid, "birth": birth}


def _rpc_owner_matches(
    row: sqlite3.Row,
    coordinator_id: str,
    identity: Mapping[str, Any] | None = None,
) -> bool:
    try:
        coordinator_id = _require_rpc_coordinator_id(coordinator_id)
        if row["coordinator_id"] != coordinator_id:
            return False
        expected = {
            "pid": int(row["coordinator_pid"]),
            "pgid": int(row["coordinator_pgid"]),
            "birth": str(row["coordinator_birth"]),
        }
        observed = dict(identity) if identity is not None else _capture_rpc_coordinator_identity()
        if observed is None:
            return False
        return (
            type(observed.get("pid")) is int
            and type(observed.get("pgid")) is int
            and type(observed.get("birth")) is str
            and observed == expected
        )
    except (KeyError, TypeError, ValueError, RuntimeError, OSError):
        return False


def _rpc_run_row(connection: sqlite3.Connection, run_id: int) -> sqlite3.Row | None:
    return connection.execute(
        "SELECT * FROM application_rpc_runs WHERE run_id=?", (run_id,)
    ).fetchone()


def rpc_run_owner_matches(
    connection: sqlite3.Connection,
    *,
    run_id: int,
    coordinator_id: str,
    identity: Mapping[str, Any] | None = None,
) -> bool:
    """Return whether this caller may read a run's durable status."""
    run_id = _require_rpc_positive_int(run_id, "run_id")
    coordinator_id = _require_rpc_coordinator_id(coordinator_id)
    row = _rpc_run_row(connection, run_id)
    if row is None:
        return False
    if _rpc_owner_matches(row, coordinator_id, identity):
        return True
    return bool(
        str(row["coordinator_id"]) == coordinator_id
        and (
            (
                int(row["handoff_committed"]) == 1
                and str(row["state"]) in {"manual", "blocked", "review_ready"}
            )
            or str(row["state"]) == "failed"
        )
    )


def _rpc_request_row(connection: sqlite3.Connection, request_id: str) -> sqlite3.Row | None:
    return connection.execute(
        "SELECT * FROM application_rpc_requests WHERE request_id=?", (request_id,)
    ).fetchone()


def _rpc_request_info(row: sqlite3.Row) -> RpcRequestInfo:
    return RpcRequestInfo(
        request_id=str(row["request_id"]),
        protocol_version=int(row["protocol_version"]),
        operation=str(row["operation"]),
        semantic_sha256=str(row["semantic_sha256"]),
        request_json=str(row["request_json"]),
        run_id=int(row["run_id"]) if row["run_id"] is not None else None,
        parent_request_id=str(row["parent_request_id"]) if row["parent_request_id"] is not None else None,
        state=str(row["state"]),  # type: ignore[arg-type]
        response_json=str(row["response_json"]) if row["response_json"] is not None else None,
        created_at=str(row["created_at"]),
        completed_at=str(row["completed_at"]) if row["completed_at"] is not None else None,
    )


def _stored_rpc_request(row: sqlite3.Row) -> ApplicationRpcRequest:
    try:
        raw = json.loads(str(row["request_json"]))
        if not isinstance(raw, Mapping):
            raise ValueError
        request = ApplicationRpcRequest(
            int(raw["protocol_version"]),
            str(raw["request_id"]),
            str(raw["operation"]),
            int(raw["deadline_unix_ms"]),
            raw["run_id"] if raw["run_id"] is None else int(raw["run_id"]),
            raw["payload"],
        )
        if _canonical_rpc_json(request.to_mapping()) != str(row["request_json"]):
            raise ValueError
        if semantic_request_sha256(request) != str(row["semantic_sha256"]):
            raise ValueError
        if request.protocol_version != int(row["protocol_version"]) or request.operation != str(row["operation"]):
            raise ValueError
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise RuntimeError("stored RPC request integrity failure") from exc
    return request


def _canonical_rpc_json(value: Any) -> str:
    try:
        encoded = json.dumps(
            thaw_json(value),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        )
    except (TypeError, ValueError, UnicodeError) as exc:
        raise TypeError("response must be JSON-compatible") from exc
    if len(encoded.encode("utf-8")) > MAX_APPLICATION_JSON_BYTES:
        raise ValueError("response JSON exceeds the UTF-8 byte cap")
    return encoded


def _parse_rpc_response(
    response: Mapping[str, Any] | str | bytes | bytearray,
    *,
    request: ApplicationRpcRequest,
) -> tuple[Mapping[str, Any], str]:
    if not isinstance(response, Mapping) and not isinstance(response, (str, bytes, bytearray)):
        raise TypeError("response must be a mapping or JSON")
    raw_text: str | None = None
    if isinstance(response, (str, bytes, bytearray)):
        try:
            raw_text = response.decode("utf-8") if isinstance(response, (bytes, bytearray)) else response
        except UnicodeDecodeError as exc:
            raise ValueError("response JSON must be UTF-8") from exc
    parsed = parse_application_response(response, request=request)
    canonical = _canonical_rpc_json(parsed)
    if raw_text is not None and raw_text != canonical:
        raise ValueError("response JSON must use canonical encoding")
    return parsed, canonical


def _validate_rpc_parent(
    connection: sqlite3.Connection,
    *,
    request: ApplicationRpcRequest,
    run_id: int | None,
    parent_request_id: str | None,
) -> int | None:
    if run_id is not None:
        run_id = _require_rpc_positive_int(run_id, "run_id")
    if request.run_id is not None:
        if run_id is not None and run_id != request.run_id:
            raise ValueError("run_id does not match request")
        run_id = request.run_id
    if request.operation in BROWSER_OPERATIONS and parent_request_id is None:
        raise ValueError("browser requests require parent_request_id")
    if parent_request_id is not None:
        parent_request_id = _require_rpc_request_id(parent_request_id)
        parent = _rpc_request_row(connection, parent_request_id)
        if parent is None:
            raise RuntimeError("parent request not found")
        if run_id != (int(parent["run_id"]) if parent["run_id"] is not None else None):
            raise RuntimeError("parent request does not belong to run")
        if not str(parent["operation"]).startswith("run."):
            raise RuntimeError("parent request must be a lifecycle request")
    if run_id is not None and _rpc_run_row(connection, run_id) is None:
        if request.operation in BROWSER_OPERATIONS or parent_request_id is not None:
            raise RuntimeError("rpc run not found")
        return None
    return run_id


def _rpc_outcome(
    *,
    outcome: Literal["new", "pending", "completed", "conflict", "unavailable"],
    request_id: str,
    coordinator_id: str,
    run_id: int | None = None,
    claim: ApplicationClaim | None = None,
) -> RpcClaimOutcome:
    return RpcClaimOutcome(
        outcome=outcome,
        run_id=run_id,
        request_id=request_id,
        coordinator_id=coordinator_id,
        claim=claim,
    )


def claim_application_job_for_rpc(
    connection: sqlite3.Connection,
    *,
    owner: str,
    request: ApplicationRpcRequest,
    coordinator_id: str,
    coordinator_identity: Mapping[str, Any] | None = None,
) -> RpcClaimOutcome:
    """Atomically reserve a parsed ``run.start`` and claim exactly one job."""
    if type(owner) is not str or not owner.strip():
        raise TypeError("owner must be a non-empty string")
    if not isinstance(request, ApplicationRpcRequest):
        raise TypeError("request must be an ApplicationRpcRequest")
    if request.operation != "run.start" or request.run_id is not None:
        raise ValueError("claim_application_job_for_rpc requires an unbound run.start request")
    coordinator_id = _require_rpc_coordinator_id(coordinator_id)
    payload = request.payload
    if not isinstance(payload, Mapping) or type(payload.get("job_url")) is not str or not payload["job_url"]:
        raise ValueError("run.start payload must contain job_url")
    job_url = str(payload["job_url"])
    lookup_url = canonicalize_url(job_url)
    if lookup_url is None:
        raise ValueError("run.start payload must contain a canonical job_url")
    request_id = _require_rpc_request_id(request.request_id)
    semantic_sha256 = semantic_request_sha256(request)
    request_json = _canonical_rpc_json(request.to_mapping())

    connection.execute("BEGIN IMMEDIATE")
    try:
        _require_rpc_deadline_live(request.deadline_unix_ms)
        existing = _rpc_request_row(connection, request_id)
        if existing is not None:
            try:
                stored_request = _stored_rpc_request(existing)
            except RuntimeError:
                connection.rollback()
                return _rpc_outcome(
                    outcome="conflict", request_id=request_id, coordinator_id=coordinator_id
                )
            if (
                semantic_request_sha256(stored_request) != semantic_sha256
                or int(existing["protocol_version"]) != request.protocol_version
                or str(existing["operation"]) != request.operation
                or existing["parent_request_id"] is not None
            ):
                connection.rollback()
                return _rpc_outcome(
                    outcome="conflict", request_id=request_id, coordinator_id=coordinator_id
                )
            if existing["run_id"] is not None:
                owner_row = _rpc_run_row(connection, int(existing["run_id"]))
                owner_ok = (
                    owner_row is not None
                    and (
                        _rpc_owner_matches(
                            owner_row,
                            coordinator_id,
                            coordinator_identity,
                        )
                        if existing["state"] != "completed"
                        else str(owner_row["coordinator_id"]) == coordinator_id
                    )
                )
                if not owner_ok:
                    connection.rollback()
                    return _rpc_outcome(
                        outcome="conflict",
                        request_id=request_id,
                        coordinator_id=coordinator_id,
                    )
            connection.commit()
            return _rpc_outcome(
                outcome="completed" if existing["state"] == "completed" else "pending",
                request_id=request_id,
                coordinator_id=coordinator_id,
                run_id=int(existing["run_id"]) if existing["run_id"] is not None else None,
            )

        # ApplicationRpcRequest validates the ATS route; canonicalize only the
        # lookup key so request identity and exact requested-job semantics stay
        # bound to the caller's validated payload.
        row = connection.execute(
            """
            SELECT * FROM jobs
            WHERE status='queued' AND canonical_url=?
            ORDER BY posted_at DESC NULLS LAST, first_seen_at ASC, id ASC
            LIMIT 1
            """,
            (lookup_url,),
        ).fetchone()
        if row is not None and _prior_attempt_process_conflict(
            connection,
            job_id=int(row["id"]),
        ):
            connection.rollback()
            return _rpc_outcome(
                outcome="unavailable",
                request_id=request_id,
                coordinator_id=coordinator_id,
            )
        now = utc_now()
        if row is not None:
            try:
                if coordinator_identity is None:
                    captured_identity = _identity_payload(
                        None, os.getpid(), require_leader=False
                    )
                else:
                    raw_identity = dict(coordinator_identity)
                    if type(raw_identity.get("pid")) is not int:
                        raise RuntimeError("process identity unavailable")
                    captured_identity = _identity_payload(
                        raw_identity,
                        int(raw_identity["pid"]),
                        require_leader=False,
                    )
                if (
                    not isinstance(captured_identity, Mapping)
                    or type(captured_identity.get("pid")) is not int
                    or type(captured_identity.get("pgid")) is not int
                    or type(captured_identity.get("birth")) is not str
                    or captured_identity["pid"] <= 0
                    or captured_identity["pgid"] <= 0
                    or not captured_identity["birth"]
                ):
                    raise RuntimeError("process identity unavailable")
            except Exception:
                connection.rollback()
                return _rpc_outcome(
                    outcome="unavailable", request_id=request_id, coordinator_id=coordinator_id
                )
        else:
            captured_identity = None
        if row is None:
            connection.execute(
                """
                INSERT INTO application_rpc_requests
                    (request_id, protocol_version, operation, semantic_sha256, request_json, run_id, parent_request_id, state, created_at)
                VALUES (?, ?, ?, ?, ?, NULL, NULL, 'pending', ?)
                """,
                (request_id, request.protocol_version, request.operation, semantic_sha256, request_json, now),
            )
            _require_rpc_deadline_live(request.deadline_unix_ms)
            connection.commit()
            return _rpc_outcome(
                outcome="unavailable", request_id=request_id, coordinator_id=coordinator_id
            )

        changed = connection.execute(
            "UPDATE jobs SET status='in_progress' WHERE id=? AND status='queued'",
            (row["id"],),
        ).rowcount
        if changed != 1:
            connection.rollback()
            return _rpc_outcome(
                outcome="unavailable",
                request_id=request_id,
                coordinator_id=coordinator_id,
            )
        cur = connection.execute(
            """
            INSERT INTO application_runs (job_id, apply_url, status, reason_code, owner, started_at)
            VALUES (?, ?, 'running', NULL, ?, ?)
            """,
            (row["id"], _redacted_apply_url(str(row["canonical_url"])), owner, now),
        )
        if cur.lastrowid is None:
            raise RuntimeError("application run id unavailable")
        run_id = int(cur.lastrowid)
        selected = connection.execute("SELECT * FROM jobs WHERE id=?", (row["id"],)).fetchone()
        if selected is None:
            raise RuntimeError("claimed job disappeared")
        connection.execute(
            """
            INSERT INTO application_rpc_runs
                (
                    run_id, coordinator_id, coordinator_pid, coordinator_pgid, coordinator_birth,
                    state, action_sequence, version, created_at, updated_at
                )
            VALUES (?, ?, ?, ?, ?, 'starting', 0, 1, ?, ?)
            """,
            (
                run_id,
                coordinator_id,
                int(captured_identity["pid"]),
                int(captured_identity["pgid"]),
                str(captured_identity["birth"]),
                now,
                now,
            ),
        )
        connection.execute(
            """
            INSERT INTO application_rpc_requests
                (request_id, protocol_version, operation, semantic_sha256, request_json, run_id, parent_request_id, state, created_at)
            VALUES (?, ?, ?, ?, ?, ?, NULL, 'pending', ?)
            """,
            (request_id, request.protocol_version, request.operation, semantic_sha256, request_json, run_id, now),
        )
        claim = ApplicationClaim(run_id=run_id, job=dict(selected))
        _require_rpc_deadline_live(request.deadline_unix_ms)
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    return _rpc_outcome(
        outcome="new",
        run_id=run_id,
        request_id=request_id,
        coordinator_id=coordinator_id,
        claim=claim,
    )
def reserve_rpc_request(
    connection: sqlite3.Connection,
    *,
    request: ApplicationRpcRequest,
    parent_request_id: str | None = None,
    run_id: int | None = None,
) -> RpcRequestInfo:
    """Reserve a parsed request and validate its run/parent provenance."""
    if not isinstance(request, ApplicationRpcRequest):
        raise TypeError("request must be an ApplicationRpcRequest")
    request_id = _require_rpc_request_id(request.request_id)
    semantic_sha256 = semantic_request_sha256(request)
    request_json = _canonical_rpc_json(request.to_mapping())
    if parent_request_id is not None:
        parent_request_id = _require_rpc_request_id(parent_request_id)
    connection.execute("BEGIN IMMEDIATE")
    try:
        effective_run_id = _validate_rpc_parent(
            connection,
            request=request,
            run_id=run_id,
            parent_request_id=parent_request_id,
        )
        existing = _rpc_request_row(connection, request_id)
        if existing is not None:
            try:
                stored_request = _stored_rpc_request(existing)
            except RuntimeError:
                connection.rollback()
                raise RuntimeError("stored RPC request integrity failure")
            if (
                semantic_request_sha256(stored_request) != semantic_sha256
                or int(existing["protocol_version"]) != request.protocol_version
                or str(existing["operation"]) != request.operation
                or (int(existing["run_id"]) if existing["run_id"] is not None else None) != effective_run_id
                or (str(existing["parent_request_id"]) if existing["parent_request_id"] is not None else None)
                != parent_request_id
            ):
                connection.rollback()
                raise RuntimeError("conflicting request binding for existing request_id")
            connection.commit()
            return _rpc_request_info(existing)
        now = utc_now()
        connection.execute(
            """
            INSERT INTO application_rpc_requests
                (request_id, protocol_version, operation, semantic_sha256, request_json, run_id, parent_request_id, state, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', ?)
            """,
            (
                request_id, request.protocol_version, request.operation, semantic_sha256,
                request_json, effective_run_id, parent_request_id, now,
            ),
        )
    except Exception:
        connection.rollback()
        raise
    connection.commit()
    return RpcRequestInfo(
        request_id=request_id,
        protocol_version=request.protocol_version,
        operation=request.operation,
        semantic_sha256=semantic_sha256,
        request_json=request_json,
        run_id=effective_run_id,
        parent_request_id=parent_request_id,
        state="pending",
        response_json=None,
        created_at=now,
        completed_at=None,
        created=True,
    )


def complete_rpc_request(
    connection: sqlite3.Connection,
    *,
    request: ApplicationRpcRequest,
    response: Mapping[str, Any] | str | bytes | bytearray,
    parent_request_id: str | None = None,
    coordinator_id: str | None = None,
    allow_terminal_handoff_read: bool = False,
    deadline_unix_ms: int | None = None,
) -> RpcRequestInfo:
    """Validate and durably complete one parsed request before emission."""
    if not isinstance(request, ApplicationRpcRequest):
        raise TypeError("request must be an ApplicationRpcRequest")
    request_id = _require_rpc_request_id(request.request_id)
    if parent_request_id is not None:
        parent_request_id = _require_rpc_request_id(parent_request_id)
    if coordinator_id is not None:
        coordinator_id = _require_rpc_coordinator_id(coordinator_id)
    if deadline_unix_ms is not None and (
        type(deadline_unix_ms) is not int or deadline_unix_ms <= 0
    ):
        raise TypeError("deadline_unix_ms must be a positive integer")
    parsed, response_json = _parse_rpc_response(response, request=request)
    response_run_id = parsed["run_id"]
    if type(response_run_id) is not int and response_run_id is not None:
        raise ValueError("response run_id is invalid")

    connection.execute("BEGIN IMMEDIATE")
    try:
        existing = _rpc_request_row(connection, request_id)
        if existing is None:
            connection.rollback()
            raise RuntimeError("request not found")
        stored_request = _stored_rpc_request(existing)
        stored_run_id = int(existing["run_id"]) if existing["run_id"] is not None else None
        stored_parent = str(existing["parent_request_id"]) if existing["parent_request_id"] is not None else None
        owner_row = (
            _rpc_run_row(connection, stored_run_id)
            if coordinator_id is not None and stored_run_id is not None
            else None
        )
        if coordinator_id is not None and stored_run_id is not None:
            terminal_read_ok = (
                allow_terminal_handoff_read
                and stored_request.operation in {"run.status", "run.resume", "run.cancel"}
                and owner_row is not None
                and str(owner_row["coordinator_id"]) == coordinator_id
                and (
                    (
                        int(owner_row["handoff_committed"]) == 1
                        and str(owner_row["state"]) in {"manual", "blocked", "review_ready"}
                    )
                    or str(owner_row["state"]) == "failed"
                )
            )
            if owner_row is None or (not _rpc_owner_matches(owner_row, coordinator_id) and not terminal_read_ok):
                connection.rollback()
                raise RuntimeError("rpc run coordinator ownership mismatch")
        expected_response_run_id = stored_run_id if stored_run_id is not None else request.run_id
        if (
            semantic_request_sha256(stored_request) != semantic_request_sha256(request)
            or int(existing["protocol_version"]) != request.protocol_version
            or str(existing["operation"]) != request.operation
            or (request.operation != "run.start" and stored_run_id is not None and stored_run_id != request.run_id)
            or expected_response_run_id != response_run_id
            or parent_request_id != stored_parent
        ):
            connection.rollback()
            raise RuntimeError("request binding conflict")
        if existing["state"] == "completed":
            if str(existing["response_json"]) != response_json:
                connection.rollback()
                raise RuntimeError("conflicting response for completed request")
            connection.commit()
            return _rpc_request_info(existing)
        _require_rpc_deadline_live(deadline_unix_ms)
        now = utc_now()
        owner_clause = ""
        owner_params: tuple[Any, ...] = ()
        if coordinator_id is not None and stored_run_id is not None and owner_row is not None:
            terminal_read_row = (
                allow_terminal_handoff_read
                and (
                    (
                        int(owner_row["handoff_committed"]) == 1
                        and str(owner_row["state"]) in {"manual", "blocked", "review_ready"}
                    )
                    or str(owner_row["state"]) == "failed"
                )
            )
            if terminal_read_row:
                owner_clause = """
                  AND EXISTS (
                      SELECT 1 FROM application_rpc_runs
                      WHERE run_id=application_rpc_requests.run_id
                        AND coordinator_id=?
                        AND (
                            (handoff_committed=1 AND state IN ('manual', 'blocked', 'review_ready'))
                            OR state='failed'
                        )
                  )
                """
                owner_params = (coordinator_id,)
            else:
                owner_clause = """
                  AND EXISTS (
                      SELECT 1
                      FROM application_rpc_runs
                      WHERE run_id=application_rpc_requests.run_id
                        AND coordinator_id=?
                        AND coordinator_pid=? AND coordinator_pgid=? AND coordinator_birth=?
                  )
                """
                owner_params = (
                    coordinator_id,
                    int(owner_row["coordinator_pid"]),
                    int(owner_row["coordinator_pgid"]),
                    str(owner_row["coordinator_birth"]),
                )
        _require_rpc_deadline_live(deadline_unix_ms)
        changed = connection.execute(
            f"""
            UPDATE application_rpc_requests
            SET state='completed', response_json=?, completed_at=?
            WHERE request_id=? AND state='pending'{owner_clause}
            """,
            (response_json, now, request_id, *owner_params),
        ).rowcount
        if changed != 1:
            connection.rollback()
            raise RuntimeError("request not pending")
        completed = connection.execute(
            "SELECT * FROM application_rpc_requests WHERE request_id=?", (request_id,)
        ).fetchone()
        if completed is None:
            raise RuntimeError("completed request disappeared")
        _require_rpc_deadline_live(deadline_unix_ms)
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    return _rpc_request_info(completed)


def abort_rpc_start(
    connection: sqlite3.Connection,
    *,
    request: ApplicationRpcRequest,
    coordinator_id: str,
    error_code: str,
    release_claim: bool = True,
) -> RpcRequestInfo:
    """Fail a claimed ``run.start`` and optionally release its application claim."""
    if not isinstance(request, ApplicationRpcRequest) or request.operation != "run.start":
        raise ValueError("abort_rpc_start requires a run.start request")
    if request.run_id is not None:
        raise ValueError("abort_rpc_start requires an unbound run.start request")
    coordinator_id = _require_rpc_coordinator_id(coordinator_id)
    if type(error_code) is not str:
        raise TypeError("error_code must be a string")
    if type(release_claim) is not bool:
        raise TypeError("release_claim must be a boolean")
    request_id = _require_rpc_request_id(request.request_id)

    connection.execute("BEGIN IMMEDIATE")
    try:
        existing = _rpc_request_row(connection, request_id)
        if existing is None:
            raise RuntimeError("request not found")
        stored = _stored_rpc_request(existing)
        if (
            semantic_request_sha256(stored) != semantic_request_sha256(request)
            or int(existing["protocol_version"]) != request.protocol_version
            or str(existing["operation"]) != request.operation
            or existing["parent_request_id"] is not None
        ):
            raise RuntimeError("request binding conflict")
        if existing["state"] == "completed":
            connection.commit()
            return _rpc_request_info(existing)

        run_id = int(existing["run_id"]) if existing["run_id"] is not None else None
        action_sequence = 0
        event_sequence = 0
        rpc_row: sqlite3.Row | None = None
        if run_id is not None:
            rpc_row = _rpc_run_row(connection, run_id)
            application_row = connection.execute(
                "SELECT * FROM application_runs WHERE id=?", (run_id,)
            ).fetchone()
            if (
                rpc_row is None
                or application_row is None
                or not _rpc_owner_matches(rpc_row, coordinator_id)
            ):
                raise RuntimeError("rpc run coordinator ownership mismatch")
            action_sequence = int(rpc_row["action_sequence"])
            if str(rpc_row["state"]) not in _RPC_TERMINAL_STATES:
                action_sequence += 1
                now = utc_now()
                changed = connection.execute(
                    """
                    UPDATE application_rpc_runs
                    SET state=?, action_sequence=?, cancellation_requested=?,
                        version=version+1, updated_at=?
                    WHERE run_id=? AND coordinator_id=?
                      AND coordinator_pid=? AND coordinator_pgid=? AND coordinator_birth=?
                      AND state NOT IN ('failed', 'review_ready')
                    """,
                    (
                        "failed" if release_claim else "manual",
                        action_sequence,
                        int(error_code == "cancelled"),
                        now,
                        run_id,
                        coordinator_id,
                        int(rpc_row["coordinator_pid"]),
                        int(rpc_row["coordinator_pgid"]),
                        str(rpc_row["coordinator_birth"]),
                    ),
                ).rowcount
                if changed != 1:
                    raise RuntimeError("rpc run abort CAS failed")
                event_sequence = _append_rpc_event_locked(
                    connection,
                    run_id=run_id,
                    request_id=request_id,
                    event_type="run_failed" if release_claim else "manual_intervention_required",
                    summary_code="failed" if release_claim else "page_not_stable",
                    action_sequence=action_sequence,
                    observation_sha256=None,
                    allow_terminal=True,
                    coordinator_id=coordinator_id,
                )
            else:
                latest = _latest_rpc_event_row(connection, run_id)
                event_sequence = int(latest["sequence"]) if latest is not None else 0

            now = utc_now()
            observation = _decode_run_json(application_row["observation_json"])
            clear_omp_spawn_marker = (
                release_claim and observation.get("_omp_spawn_attempted") is True
            )
            if clear_omp_spawn_marker:
                observation.pop("_omp_spawn_attempted", None)
            if (
                application_row["status"] == "running"
                and application_row["outcome"] is None
                and application_row["reviewed_at"] is None
            ):
                if release_claim:
                    changed = connection.execute(
                        """
                        UPDATE application_runs
                        SET status='failed', reason_code='abandoned_running_attempt',
                            finished_at=?, outcome='retry', reviewed_at=?
                        WHERE id=? AND status='running'
                          AND reason_code IS NULL AND outcome IS NULL AND reviewed_at IS NULL
                        """,
                        (now, now, run_id),
                    ).rowcount
                else:
                    observation = _decode_run_json(application_row["observation_json"])
                    observation["_launch_cleanup_quarantine"] = {
                        "reason_code": "page_not_stable",
                    }
                    changed = connection.execute(
                        """
                        UPDATE application_runs
                        SET status='manual', reason_code='page_not_stable',
                            finished_at=?, observation_json=?
                        WHERE id=? AND status='running'
                          AND reason_code IS NULL AND outcome IS NULL AND reviewed_at IS NULL
                        """,
                        (now, encode_json(observation), run_id),
                    ).rowcount
                if changed != 1:
                    raise RuntimeError("application run abort CAS failed")
                if clear_omp_spawn_marker:
                    changed = connection.execute(
                        """
                        UPDATE application_runs
                        SET observation_json=?
                        WHERE id=? AND status='failed'
                          AND reason_code='abandoned_running_attempt'
                          AND outcome='retry' AND reviewed_at IS NOT NULL
                        """,
                        (encode_json(observation), run_id),
                    ).rowcount
                    if changed != 1:
                        raise RuntimeError("OMP spawn marker cleanup CAS failed")
            if release_claim:
                connection.execute(
                    """
                    UPDATE jobs SET status='queued'
                    WHERE id=(SELECT job_id FROM application_runs WHERE id=?)
                      AND status='in_progress'
                    """,
                    (run_id,),
                )

        response = build_application_response(
            request,
            ok=False,
            state="failed" if release_claim else "manual",
            action_sequence=action_sequence,
            event_sequence=event_sequence,
            error=error_code,
            run_id=run_id,
        )
        _, response_json = _parse_rpc_response(response, request=request)
        changed = connection.execute(
            """
            UPDATE application_rpc_requests
            SET state='completed', response_json=?, completed_at=?
            WHERE request_id=? AND state='pending'
              AND (
                  run_id IS NULL
                  OR EXISTS (
                      SELECT 1
                      FROM application_rpc_runs
                      WHERE run_id=application_rpc_requests.run_id
                        AND coordinator_id=?
                        AND coordinator_pid=? AND coordinator_pgid=? AND coordinator_birth=?
                  )
              )
            """,
            (
                response_json,
                now,
                request_id,
                coordinator_id,
                int(rpc_row["coordinator_pid"]) if rpc_row is not None else -1,
                int(rpc_row["coordinator_pgid"]) if rpc_row is not None else -1,
                str(rpc_row["coordinator_birth"]) if rpc_row is not None else "",
            ),
        ).rowcount
        if changed != 1:
            raise RuntimeError("request abort completion CAS failed")
        completed = _rpc_request_row(connection, request_id)
        if completed is None:
            raise RuntimeError("aborted request disappeared")
    except Exception:
        connection.rollback()
        raise
    connection.commit()
    return _rpc_request_info(completed)


def abort_rpc_run_for_shutdown(
    connection: sqlite3.Connection,
    *,
    run_id: int,
    coordinator_id: str,
) -> bool:
    """Exact-clean an owned active run before marking it failed/retry."""
    run_id = _require_rpc_positive_int(run_id, "run_id")
    coordinator_id = _require_rpc_coordinator_id(coordinator_id)
    connection.execute("BEGIN IMMEDIATE")
    try:
        rpc_row = _rpc_run_row(connection, run_id)
        application_row = connection.execute(
            "SELECT * FROM application_runs WHERE id=?", (run_id,)
        ).fetchone()
        if (
            rpc_row is None
            or application_row is None
            or not _rpc_owner_matches(rpc_row, coordinator_id)
            or str(rpc_row["state"]) not in {"starting", "running"}
            or bool(rpc_row["handoff_committed"])
            or str(application_row["status"]) != "running"
            or application_row["outcome"] is not None
            or application_row["reviewed_at"] is not None
        ):
            connection.rollback()
            return False
        observation = _decode_run_json(application_row["observation_json"])
        if observation.get("_omp_spawn_attempted") is True:
            connection.rollback()
            return False
        identities = _registered_shutdown_process_identities(
            rpc_row, application_row
        )
        pending_rows = connection.execute(
            """
            SELECT * FROM application_rpc_requests
            WHERE run_id=? AND state='pending'
            ORDER BY created_at, request_id
            """,
            (run_id,),
        ).fetchall()
        all_rows = connection.execute(
            """
            SELECT * FROM application_rpc_requests
            WHERE run_id=? ORDER BY created_at, request_id
            """,
            (run_id,),
        ).fetchall()
        if (
            observation.get("_spawn_attempted") is True
            and application_row["owner_pid"] is None
        ):
            connection.rollback()
            return False
        event_request = pending_rows[0] if pending_rows else (
            all_rows[0] if all_rows else None
        )
        if event_request is None:
            connection.rollback()
            return False
        requests = [_stored_rpc_request(row) for row in pending_rows]
        stored_event_request = _stored_rpc_request(event_request)
        states = tuple(_exact_process_identity_state(identity) for identity in identities)
        if any(state not in {"absent", "live"} for state in states):
            connection.rollback()
            return False
        for identity, state in zip(identities, states):
            if state == "live":
                _cleanup_exact_process_identity(identity)
        if any(
            _exact_process_identity_state(identity) != "absent"
            for identity in identities
        ):
            connection.rollback()
            return False
        now = utc_now()
        action_sequence = int(rpc_row["action_sequence"]) + 1
        changed = connection.execute(
            """
            UPDATE application_rpc_runs
            SET state='failed', cancellation_requested=1,
                action_sequence=?, version=version+1, updated_at=?
            WHERE run_id=? AND coordinator_id=?
              AND coordinator_pid=? AND coordinator_pgid=? AND coordinator_birth=?
              AND state IN ('starting', 'running')
              AND handoff_committed=0 AND version=?
            """,
            (
                action_sequence,
                now,
                run_id,
                coordinator_id,
                int(rpc_row["coordinator_pid"]),
                int(rpc_row["coordinator_pgid"]),
                str(rpc_row["coordinator_birth"]),
                int(rpc_row["version"]),
            ),
        ).rowcount
        if changed != 1:
            connection.rollback()
            return False
        changed = connection.execute(
            """
            UPDATE application_runs
            SET status='failed', reason_code='abandoned_running_attempt',
                finished_at=?, outcome='retry', reviewed_at=?
            WHERE id=? AND status='running' AND outcome IS NULL AND reviewed_at IS NULL
            """,
            (now, now, run_id),
        ).rowcount
        if changed != 1:
            connection.rollback()
            return False
        event_sequence = _append_rpc_event_locked(
            connection,
            run_id=run_id,
            request_id=str(stored_event_request.request_id),
            event_type="run_failed",
            summary_code="failed",
            action_sequence=action_sequence,
            observation_sha256=None,
            allow_terminal=True,
            coordinator_id=coordinator_id,
            check_owner=False,
        )
        connection.execute(
            """
            UPDATE jobs SET status='queued'
            WHERE id=(SELECT job_id FROM application_runs WHERE id=?)
              AND status='in_progress'
            """,
            (run_id,),
        )
        for request in requests:
            _, response_json = _rpc_reconciliation_response(
                request,
                run_id=run_id,
                action_sequence=action_sequence,
                event_sequence=event_sequence,
            )
            changed = connection.execute(
                """
                UPDATE application_rpc_requests
                SET state='completed', response_json=?, completed_at=?
                WHERE request_id=? AND state='pending' AND run_id=?
                """,
                (response_json, now, request.request_id, run_id),
            ).rowcount
            if changed != 1:
                connection.rollback()
                return False
    except Exception:
        connection.rollback()
        return False
    connection.commit()
    return True


def release_quarantined_rpc_start(
    connection: sqlite3.Connection,
    *,
    run_id: int,
    coordinator_id: str,
) -> bool:
    """Release a manual start quarantine after late process cleanup completed."""
    run_id = _require_rpc_positive_int(run_id, "run_id")
    coordinator_id = _require_rpc_coordinator_id(coordinator_id)
    connection.execute("BEGIN IMMEDIATE")
    try:
        rpc_row = _rpc_run_row(connection, run_id)
        application_row = connection.execute(
            "SELECT * FROM application_runs WHERE id=?",
            (run_id,),
        ).fetchone()
        observation = (
            _decode_run_json(application_row["observation_json"])
            if application_row is not None
            else {}
        )
        if (
            rpc_row is None
            or application_row is None
            or not _rpc_owner_matches(rpc_row, coordinator_id)
            or str(rpc_row["state"]) != "manual"
            or str(application_row["status"]) != "manual"
            or str(application_row["reason_code"]) != "page_not_stable"
            or application_row["outcome"] is not None
            or application_row["reviewed_at"] is not None
            or not isinstance(observation.get("_launch_cleanup_quarantine"), Mapping)
        ):
            connection.rollback()
            return False
        observation.pop("_launch_cleanup_quarantine", None)
        observation.pop("_omp_spawn_attempted", None)
        now = utc_now()
        action_sequence = int(rpc_row["action_sequence"]) + 1
        changed = connection.execute(
            """
            UPDATE application_runs
            SET status='failed', reason_code='abandoned_running_attempt',
                observation_json=?, outcome='retry', reviewed_at=?
            WHERE id=? AND status='manual' AND reason_code='page_not_stable'
              AND outcome IS NULL AND reviewed_at IS NULL
            """,
            (encode_json(observation), now, run_id),
        ).rowcount
        if changed != 1:
            raise RuntimeError("quarantined application release CAS failed")
        changed = connection.execute(
            """
            UPDATE application_rpc_runs
            SET state='failed', action_sequence=?, version=version+1, updated_at=?
            WHERE run_id=? AND coordinator_id=?
              AND coordinator_pid=? AND coordinator_pgid=? AND coordinator_birth=?
              AND state='manual' AND handoff_committed=0 AND version=?
            """,
            (
                action_sequence,
                now,
                run_id,
                coordinator_id,
                int(rpc_row["coordinator_pid"]),
                int(rpc_row["coordinator_pgid"]),
                str(rpc_row["coordinator_birth"]),
                int(rpc_row["version"]),
            ),
        ).rowcount
        if changed != 1:
            raise RuntimeError("quarantined RPC release CAS failed")
        request_row = connection.execute(
            """
            SELECT * FROM application_rpc_requests
            WHERE run_id=? AND operation='run.start'
            ORDER BY created_at, request_id
            """,
            (run_id,),
        ).fetchall()
        if len(request_row) != 1:
            raise RuntimeError("quarantined start request provenance mismatch")
        _append_rpc_event_locked(
            connection,
            run_id=run_id,
            request_id=str(request_row[0]["request_id"]),
            event_type="run_failed",
            summary_code="failed",
            action_sequence=action_sequence,
            observation_sha256=None,
            allow_terminal=True,
            coordinator_id=coordinator_id,
        )
        changed = connection.execute(
            """
            UPDATE jobs SET status='queued'
            WHERE id=? AND status='in_progress'
            """,
            (int(application_row["job_id"]),),
        ).rowcount
        if changed != 1:
            raise RuntimeError("quarantined job release CAS failed")
    except Exception:
        connection.rollback()
        raise
    connection.commit()
    return True
def get_rpc_request(connection: sqlite3.Connection, request_id: str) -> RpcRequestInfo | None:
    request_id = _require_rpc_request_id(request_id)
    row = _rpc_request_row(connection, request_id)
    return _rpc_request_info(row) if row is not None else None


def _rpc_mutation_allowed(row: sqlite3.Row) -> bool:
    return str(row["state"]) not in _RPC_TERMINAL_STATES and not bool(row["handoff_committed"])


def _rpc_state_transition_allowed(
    row: sqlite3.Row,
    *,
    state: str,
    action_sequence: int,
    human_review_ready: bool,
    handoff_committed: bool,
) -> bool:
    current_state = str(row["state"])
    if not _rpc_mutation_allowed(row) or action_sequence <= int(row["action_sequence"]):
        return False
    allowed = {
        "starting": {"starting", "running", "failed"},
        "running": {"running", "manual", "blocked", "review_ready", "failed"},
        "manual": {"running", "failed"},
        "blocked": {"running", "failed"},
    }
    if state not in allowed.get(current_state, set()):
        return False
    if type(human_review_ready) is not bool or type(handoff_committed) is not bool:
        return False
    if state == "review_ready" and (not human_review_ready or not handoff_committed):
        return False
    if state in {"manual", "blocked"} and human_review_ready:
        return False
    if state in {"starting", "running", "failed"} and (human_review_ready or handoff_committed):
        return False
    if handoff_committed and state not in {"manual", "blocked", "review_ready"}:
        return False
    return True


def update_rpc_run_state(
    connection: sqlite3.Connection,
    *,
    run_id: int,
    coordinator_id: str,
    state: str,
    action_sequence: int,
    ats_policy: str | None = None,
    current_form_step: str | None = None,
    human_review_ready: bool = False,
    handoff_committed: bool = False,
) -> bool:
    run_id = _require_rpc_positive_int(run_id, "run_id")
    coordinator_id = _require_rpc_coordinator_id(coordinator_id)
    if state not in RPC_RUN_STATES:
        raise ValueError(f"state must be one of {RPC_RUN_STATES}")
    action_sequence = _require_rpc_action_sequence(action_sequence)
    ats_policy = _require_rpc_ats_policy(ats_policy)
    current_form_step = _require_rpc_form_step(current_form_step)
    connection.execute("BEGIN IMMEDIATE")
    try:
        row = _rpc_run_row(connection, run_id)
        if row is None or not _rpc_owner_matches(row, coordinator_id):
            connection.rollback()
            return False
        if not _rpc_state_transition_allowed(
            row,
            state=state,
            action_sequence=action_sequence,
            human_review_ready=human_review_ready,
            handoff_committed=handoff_committed,
        ):
            connection.rollback()
            return False
        if row["ats_policy"] is not None and ats_policy is not None and row["ats_policy"] != ats_policy:
            connection.rollback()
            return False
        now = utc_now()
        changed = connection.execute(
            """
            UPDATE application_rpc_runs
            SET state=?, ats_policy=COALESCE(?, ats_policy), action_sequence=?,
                current_form_step=COALESCE(?, current_form_step),
                human_review_ready=?, handoff_committed=?, version=version+1, updated_at=?
            WHERE run_id=? AND coordinator_id=?
              AND coordinator_pid=? AND coordinator_pgid=? AND coordinator_birth=?
            """,
            (
                state, ats_policy, action_sequence, current_form_step,
                int(human_review_ready), int(handoff_committed), now,
                run_id,
                coordinator_id,
                int(row["coordinator_pid"]),
                int(row["coordinator_pgid"]),
                str(row["coordinator_birth"]),
            ),
        ).rowcount
        if changed != 1:
            connection.rollback()
            return False
    except Exception:
        connection.rollback()
        raise
    connection.commit()
    return True


def update_rpc_run_process(
    connection: sqlite3.Connection,
    *,
    run_id: int,
    coordinator_id: str,
    pid: int,
    session_sha256: str,
    process_identity: Mapping[str, Any] | None = None,
) -> bool:
    run_id = _require_rpc_positive_int(run_id, "run_id")
    coordinator_id = _require_rpc_coordinator_id(coordinator_id)
    pid = _require_rpc_positive_int(pid, "pid")
    session_sha256 = _require_rpc_sha256(session_sha256, "session_sha256")
    connection.execute("BEGIN IMMEDIATE")
    try:
        row = _rpc_run_row(connection, run_id)
        if row is None or not _rpc_owner_matches(row, coordinator_id) or not _rpc_mutation_allowed(row):
            connection.rollback()
            return False
        application_row = connection.execute(
            "SELECT observation_json FROM application_runs WHERE id=?", (run_id,)
        ).fetchone()
        if application_row is None:
            connection.rollback()
            return False

        def resolve_spawn_marker() -> None:
            observation = _decode_run_json(application_row["observation_json"])
            if observation.get("_omp_spawn_attempted") is not True:
                return
            observation.pop("_omp_spawn_attempted", None)
            changed = connection.execute(
                """
                UPDATE application_runs
                SET observation_json=?
                WHERE id=? AND status='running' AND reviewed_at IS NULL AND outcome IS NULL
                """,
                (encode_json(observation), run_id),
            ).rowcount
            if changed != 1:
                raise RuntimeError("OMP spawn marker resolution CAS failed")
        if process_identity is None:
            captured = _identity_payload(None, pid, require_leader=True)
        else:
            try:
                captured = _identity_payload(
                    dict(process_identity), pid, require_leader=True
                )
            except (RuntimeError, TypeError, ValueError):
                connection.rollback()
                return False
        observed_identity = (
            pid,
            int(captured["pgid"]),
            str(captured["birth"]),
            session_sha256,
        )
        existing = (
            row["omp_process_pid"],
            row["omp_process_pgid"],
            row["omp_process_birth"],
            row["omp_session_sha256"],
        )
        if any(item is None for item in existing) and not all(item is None for item in existing):
            connection.rollback()
            return False
        if all(item is not None for item in existing):
            if tuple(existing) == observed_identity:
                resolve_spawn_marker()
                connection.commit()
                return True
            provisional = rpc_provisional_session_sha256(
                pid, int(captured["pgid"]), str(captured["birth"])
            )
            if not (
                str(row["state"]) == "starting"
                and tuple(existing[:3]) == tuple(observed_identity[:3])
                and existing[3] == provisional
                and session_sha256 != provisional
            ):
                connection.rollback()
                return False
            changed = connection.execute(
                """
                UPDATE application_rpc_runs
                SET omp_session_sha256=?, version=version+1, updated_at=?
                WHERE run_id=? AND coordinator_id=?
                  AND coordinator_pid=? AND coordinator_pgid=? AND coordinator_birth=?
                  AND state='starting' AND handoff_committed=0
                  AND omp_process_pid=? AND omp_process_pgid=?
                  AND omp_process_birth=? AND omp_session_sha256=? AND version=?
                """,
                (
                    session_sha256,
                    utc_now(),
                    run_id,
                    coordinator_id,
                    int(row["coordinator_pid"]),
                    int(row["coordinator_pgid"]),
                    str(row["coordinator_birth"]),
                    pid,
                    int(captured["pgid"]),
                    str(captured["birth"]),
                    provisional,
                    int(row["version"]),
                ),
            ).rowcount
            if changed != 1:
                connection.rollback()
                return False
            resolve_spawn_marker()
            connection.commit()
            return True
        now = utc_now()
        changed = connection.execute(
            """
            UPDATE application_rpc_runs
            SET omp_process_pid=?, omp_process_pgid=?, omp_process_birth=?,
                omp_session_sha256=?, version=version+1, updated_at=?
            WHERE run_id=? AND coordinator_id=?
              AND coordinator_pid=? AND coordinator_pgid=? AND coordinator_birth=?
            """,
            (
                *observed_identity,
                now,
                run_id,
                coordinator_id,
                int(row["coordinator_pid"]),
                int(row["coordinator_pgid"]),
                str(row["coordinator_birth"]),
            ),
        ).rowcount
        if changed != 1:
            connection.rollback()
            return False
        resolve_spawn_marker()
    except Exception:
        connection.rollback()
        raise
    connection.commit()
    return True


def update_rpc_run_observation(
    connection: sqlite3.Connection,
    *,
    run_id: int,
    coordinator_id: str,
    observation_sha256: str,
    action_sequence: int,
    current_form_step: str | None = None,
) -> bool:
    run_id = _require_rpc_positive_int(run_id, "run_id")
    coordinator_id = _require_rpc_coordinator_id(coordinator_id)
    observation_sha256 = _require_rpc_sha256(observation_sha256, "observation_sha256")
    action_sequence = _require_rpc_action_sequence(action_sequence)
    current_form_step = _require_rpc_form_step(current_form_step)
    connection.execute("BEGIN IMMEDIATE")
    try:
        row = _rpc_run_row(connection, run_id)
        if row is None or not _rpc_owner_matches(row, coordinator_id) or not _rpc_mutation_allowed(row):
            connection.rollback()
            return False
        if action_sequence <= int(row["action_sequence"]):
            connection.rollback()
            return False
        now = utc_now()
        changed = connection.execute(
            """
            UPDATE application_rpc_runs
            SET last_observation_sha256=?, action_sequence=?, current_form_step=COALESCE(?, current_form_step),
                version=version+1, updated_at=?
            WHERE run_id=? AND coordinator_id=?
              AND coordinator_pid=? AND coordinator_pgid=? AND coordinator_birth=?
            """,
            (
                observation_sha256,
                action_sequence,
                current_form_step,
                now,
                run_id,
                coordinator_id,
                int(row["coordinator_pid"]),
                int(row["coordinator_pgid"]),
                str(row["coordinator_birth"]),
            ),
        ).rowcount
        if changed != 1:
            connection.rollback()
            return False
    except Exception:
        connection.rollback()
        raise
    connection.commit()
    return True


def update_rpc_run_artifact_manifest(
    connection: sqlite3.Connection,
    *,
    run_id: int,
    coordinator_id: str,
    manifest_sha256: str,
    action_sequence: int,
) -> bool:
    run_id = _require_rpc_positive_int(run_id, "run_id")
    coordinator_id = _require_rpc_coordinator_id(coordinator_id)
    manifest_sha256 = _require_rpc_sha256(manifest_sha256, "manifest_sha256")
    action_sequence = _require_rpc_action_sequence(action_sequence)
    connection.execute("BEGIN IMMEDIATE")
    try:
        row = _rpc_run_row(connection, run_id)
        if row is None or not _rpc_owner_matches(row, coordinator_id) or not _rpc_mutation_allowed(row):
            connection.rollback()
            return False
        if action_sequence <= int(row["action_sequence"]):
            connection.rollback()
            return False
        now = utc_now()
        changed = connection.execute(
            """
            UPDATE application_rpc_runs
            SET artifact_manifest_sha256=?, action_sequence=?, version=version+1, updated_at=?
            WHERE run_id=? AND coordinator_id=?
              AND coordinator_pid=? AND coordinator_pgid=? AND coordinator_birth=?
            """,


            (
                manifest_sha256,
                action_sequence,
                now,
                run_id,
                coordinator_id,
                int(row["coordinator_pid"]),
                int(row["coordinator_pgid"]),
                str(row["coordinator_birth"]),
            ),
        ).rowcount
        if changed != 1:
            connection.rollback()
            return False
    except Exception:
        connection.rollback()
        raise
    connection.commit()
    return True
def _handoff_intent_present(raw_observation: Any) -> bool:
    observation = _decode_run_json(raw_observation)
    return "_handoff_intent" in observation


def request_rpc_cancellation(
    connection: sqlite3.Connection,
    *,
    run_id: int,
    coordinator_id: str,
    deadline_unix_ms: int | None = None,
) -> bool:
    run_id = _require_rpc_positive_int(run_id, "run_id")
    coordinator_id = _require_rpc_coordinator_id(coordinator_id)
    if deadline_unix_ms is not None and (
        type(deadline_unix_ms) is not int or deadline_unix_ms <= 0
    ):
        raise TypeError("deadline_unix_ms must be a positive integer")
    connection.execute("BEGIN IMMEDIATE")
    try:
        _require_rpc_deadline_live(deadline_unix_ms)
        row = _rpc_run_row(connection, run_id)
        if row is None or not _rpc_owner_matches(row, coordinator_id) or not _rpc_mutation_allowed(row):
            connection.rollback()
            return False
        application_row = connection.execute(
            "SELECT * FROM application_runs WHERE id=?",
            (run_id,),
        ).fetchone()
        if application_row is None:
            connection.rollback()
            return False
        if _handoff_intent_present(application_row["observation_json"]):
            connection.commit()
            return False
        processless_state = (
            str(application_row["status"]) in {"manual", "blocked"}
            or (
                str(application_row["status"]) == "running"
                and str(row["state"]) in {"manual", "blocked"}
            )
        )
        if processless_state:
            if (
                application_row["outcome"] is not None
                or application_row["reviewed_at"] is not None
                or str(row["state"]) in _RPC_TERMINAL_STATES
            ):
                connection.rollback()
                return False
            next_action_sequence = int(row["action_sequence"]) + 1
            now = utc_now()
            changed = connection.execute(
                """
                UPDATE application_rpc_runs
                SET state='failed', action_sequence=?, cancellation_requested=1,
                    version=version+1, updated_at=?
                WHERE run_id=? AND coordinator_id=? AND cancellation_requested=0
                  AND coordinator_pid=? AND coordinator_pgid=? AND coordinator_birth=?
                """,
                (
                    next_action_sequence,
                    now,
                    run_id,
                    coordinator_id,
                    int(row["coordinator_pid"]),
                    int(row["coordinator_pgid"]),
                    str(row["coordinator_birth"]),
                ),
            ).rowcount
            if changed != 1:
                connection.rollback()
                return False
            changed = connection.execute(
                """
                UPDATE application_runs
                SET status='failed', reason_code='abandoned_running_attempt',
                    finished_at=COALESCE(finished_at, ?),
                    outcome='retry', reviewed_at=?
                WHERE id=? AND status IN ('running', 'manual', 'blocked')
                  AND outcome IS NULL AND reviewed_at IS NULL
                """,
                (now, now, run_id),
            ).rowcount
            if changed != 1:
                raise RuntimeError("processless cancellation application CAS failed")
            changed = connection.execute(
                """
                UPDATE jobs SET status='queued'
                WHERE id=(SELECT job_id FROM application_runs WHERE id=?)
                  AND status='in_progress'
                """,
                (run_id,),
            ).rowcount
            if changed != 1:
                raise RuntimeError("processless cancellation job CAS failed")
            event_request = connection.execute(
                """
                SELECT request_id FROM application_progress_events
                WHERE run_id=? ORDER BY sequence DESC LIMIT 1
                """,
                (run_id,),
            ).fetchone()
            if event_request is None:
                event_request = connection.execute(
                    """
                    SELECT request_id FROM application_rpc_requests
                    WHERE run_id=? ORDER BY created_at ASC, request_id ASC LIMIT 1
                    """,
                    (run_id,),
                ).fetchone()
            if event_request is None:
                raise RuntimeError("processless cancellation request provenance missing")
            _append_rpc_event_locked(
                connection,
                run_id=run_id,
                request_id=str(event_request["request_id"]),
                event_type="run_failed",
                summary_code="failed",
                action_sequence=next_action_sequence,
                observation_sha256=None,
                allow_terminal=True,
                coordinator_id=coordinator_id,
            )
            _require_rpc_deadline_live(deadline_unix_ms)
            connection.commit()
            return True
        if str(application_row["status"]) != "running":
            connection.rollback()
            return False
        if bool(row["cancellation_requested"]):
            connection.commit()
            return True
        now = utc_now()
        changed = connection.execute(
            """
            UPDATE application_rpc_runs
            SET cancellation_requested=1, version=version+1, updated_at=?
            WHERE run_id=? AND coordinator_id=? AND cancellation_requested=0
              AND coordinator_pid=? AND coordinator_pgid=? AND coordinator_birth=?
            """,
            (
                now,
                run_id,
                coordinator_id,
                int(row["coordinator_pid"]),
                int(row["coordinator_pgid"]),
                str(row["coordinator_birth"]),
            ),
        ).rowcount
        if changed != 1:
            connection.rollback()
            return False
        _require_rpc_deadline_live(deadline_unix_ms)
    except Exception:
        connection.rollback()
        raise
    connection.commit()
    return True


def read_rpc_cancellation(connection: sqlite3.Connection, run_id: int) -> bool:
    run_id = _require_rpc_positive_int(run_id, "run_id")
    row = _rpc_run_row(connection, run_id)
    return bool(row["cancellation_requested"]) if row is not None else False
def _latest_rpc_event_row(connection: sqlite3.Connection, run_id: int) -> sqlite3.Row | None:
    return connection.execute(
        "SELECT * FROM application_progress_events WHERE run_id=? ORDER BY sequence DESC LIMIT 1",
        (run_id,),
    ).fetchone()


def _rpc_job_url_from_bound_start_request(
    connection: sqlite3.Connection,
    run_id: int,
) -> str:
    request_rows = connection.execute(
        """
        SELECT *
        FROM application_rpc_requests
        WHERE run_id=? AND operation='run.start'
        ORDER BY created_at, request_id
        """,
        (run_id,),
    ).fetchall()
    if len(request_rows) != 1:
        raise RuntimeError("rpc run.start provenance is missing or ambiguous")
    request_row = request_rows[0]
    try:
        request = _stored_rpc_request(request_row)
        if (
            request.request_id != str(request_row["request_id"])
            or request.operation != "run.start"
            or request.run_id is not None
            or int(request_row["run_id"]) != run_id
        ):
            raise RuntimeError("rpc run.start provenance mismatch")
        job_url = request.payload.get("job_url")
        if type(job_url) is not str or not job_url:
            raise RuntimeError("rpc run.start provenance is missing job_url")
    except (KeyError, TypeError, ValueError, RuntimeError, AttributeError) as exc:
        raise RuntimeError("rpc run.start provenance integrity failure") from exc
    return job_url


def get_rpc_run_status(connection: sqlite3.Connection, run_id: int) -> RpcRunStatus | None:
    run_id = _require_rpc_positive_int(run_id, "run_id")
    row = connection.execute(
        """
        SELECT r.*, a.reason_code, a.apply_url
        FROM application_rpc_runs r
        JOIN application_runs a ON a.id = r.run_id
        WHERE r.run_id=?
        """,
        (run_id,),
    ).fetchone()
    if row is None:
        return None
    job_url = _rpc_job_url_from_bound_start_request(connection, run_id)
    event = _latest_rpc_event_row(connection, run_id)
    resume_eligible = (
        str(row["state"]) in {"manual", "blocked"}
        and event is not None
        and str(event["event_type"]) == "awaiting_resume"
        and not bool(row["handoff_committed"])
        and not bool(row["cancellation_requested"])
    )
    return RpcRunStatus(
        run_id=int(row["run_id"]),
        coordinator_id=str(row["coordinator_id"]),
        state=str(row["state"]),
        reason_code=str(row["reason_code"]) if row["reason_code"] is not None else None,
        ats_policy=str(row["ats_policy"]) if row["ats_policy"] is not None else None,
        apply_url=str(row["apply_url"]),
        job_url=job_url,
        last_observation_sha256=(
            str(row["last_observation_sha256"]) if row["last_observation_sha256"] is not None else None
        ),
        artifact_manifest_sha256=(
            str(row["artifact_manifest_sha256"]) if row["artifact_manifest_sha256"] is not None else None
        ),
        action_sequence=int(row["action_sequence"]),
        current_form_step=str(row["current_form_step"]) if row["current_form_step"] is not None else None,
        human_review_ready=bool(row["human_review_ready"]),
        handoff_committed=bool(row["handoff_committed"]),
        cancellation_requested=bool(row["cancellation_requested"]),
        automated_submission=bool(row["automated_submission"]),
        version=int(row["version"]),
        created_at=str(row["created_at"]),
        updated_at=str(row["updated_at"]),
        latest_event_sequence=int(event["sequence"]) if event is not None else 0,
        resume_eligible=resume_eligible,
    )


def latest_rpc_event(connection: sqlite3.Connection, run_id: int) -> RpcEventInfo | None:
    run_id = _require_rpc_positive_int(run_id, "run_id")
    row = _latest_rpc_event_row(connection, run_id)
    if row is None:
        return None
    return RpcEventInfo(
        run_id=int(row["run_id"]),
        sequence=int(row["sequence"]),
        request_id=str(row["request_id"]),
        action_sequence=int(row["action_sequence"]),
        timestamp=str(row["timestamp"]),
        event_type=str(row["event_type"]),
        summary_code=str(row["summary_code"]),
        observation_sha256=str(row["observation_sha256"]) if row["observation_sha256"] is not None else None,
    )


def rpc_resume_eligibility(connection: sqlite3.Connection, run_id: int) -> bool:
    status = get_rpc_run_status(connection, run_id)
    return bool(status and status.resume_eligible)


def _append_rpc_event_locked(
    connection: sqlite3.Connection,
    *,
    run_id: int,
    request_id: str,
    event_type: str,
    summary_code: str,
    action_sequence: int,
    observation_sha256: str | None,
    allow_terminal: bool = False,
    coordinator_id: str | None = None,
    check_owner: bool = True,
) -> int:
    row = _rpc_run_row(connection, run_id)
    if row is None:
        raise RuntimeError("rpc request/run provenance mismatch")
    if check_owner:
        owner_id = coordinator_id or str(row["coordinator_id"])
        if not _rpc_owner_matches(row, owner_id):
            raise RuntimeError("rpc run coordinator ownership mismatch")
    if not allow_terminal and not _rpc_mutation_allowed(row):
        raise RuntimeError("rpc run is terminal or handed off")
    request = _rpc_request_row(connection, request_id)
    if request is None or request["run_id"] is None or int(request["run_id"]) != run_id:
        raise RuntimeError("rpc request/run provenance mismatch")
    if action_sequence > int(row["action_sequence"]):
        raise RuntimeError("event action_sequence is ahead of rpc run")
    previous = connection.execute(
        "SELECT sequence, action_sequence FROM application_progress_events WHERE run_id=? ORDER BY sequence DESC LIMIT 1",
        (run_id,),
    ).fetchone()
    if previous is not None and action_sequence < int(previous["action_sequence"]):
        raise RuntimeError("event action_sequence is out of order")
    sequence = int(previous["sequence"]) + 1 if previous is not None else 1
    now = utc_now()
    connection.execute(
        """
        INSERT INTO application_progress_events
            (run_id, sequence, request_id, action_sequence, timestamp, event_type, summary_code, observation_sha256)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (run_id, sequence, request_id, action_sequence, now, event_type, summary_code, observation_sha256),
    )
    return sequence


def commit_rpc_run_transition(
    connection: sqlite3.Connection,
    transition: RpcRunTransition,
    *,
    deadline_unix_ms: int | None = None,
) -> RpcEventInfo:
    """Atomically apply one non-proposal run transition and append its event."""
    if not isinstance(transition, RpcRunTransition):
        raise TypeError("transition must be a RpcRunTransition")
    run_id = _require_rpc_positive_int(transition.run_id, "run_id")
    coordinator_id = _require_rpc_coordinator_id(transition.coordinator_id)
    request_id = _require_rpc_request_id(transition.request_id)
    event_type = _require_rpc_event_type(transition.event_type)
    summary_code = _require_rpc_summary_code(transition.summary_code)
    action_sequence = _require_rpc_action_sequence(transition.action_sequence)
    observation_sha256 = _require_rpc_sha256_or_none(
        transition.observation_sha256, "observation_sha256"
    )
    manifest_sha256 = _require_rpc_sha256_or_none(transition.manifest_sha256, "manifest_sha256")
    ats_policy = _require_rpc_ats_policy(transition.ats_policy)
    current_form_step = _require_rpc_form_step(transition.current_form_step)
    if transition.state is not None and transition.state not in RPC_RUN_STATES:
        raise ValueError(f"state must be one of {RPC_RUN_STATES}")
    if transition.human_review_ready is not None and type(transition.human_review_ready) is not bool:
        raise TypeError("human_review_ready must be a bool")
    if transition.handoff_committed is not None and type(transition.handoff_committed) is not bool:
        raise TypeError("handoff_committed must be a bool")
    if transition.state is None and (
        transition.human_review_ready is not None or transition.handoff_committed is not None
    ):
        raise ValueError("state is required when updating handoff flags")
    if deadline_unix_ms is not None and (
        type(deadline_unix_ms) is not int or deadline_unix_ms <= 0
    ):
        raise TypeError("deadline_unix_ms must be a positive integer")
    if not connection.in_transaction:
        connection.execute("BEGIN IMMEDIATE")

    try:
        _require_rpc_deadline_live(deadline_unix_ms)
        row = _rpc_run_row(connection, run_id)
        request = _rpc_request_row(connection, request_id)
        if row is None or request is None or request["run_id"] is None or int(request["run_id"]) != run_id:
            raise RuntimeError("rpc request/run provenance mismatch")
        if not _rpc_owner_matches(row, coordinator_id) or not _rpc_mutation_allowed(row):
            raise RuntimeError("rpc run is terminal or handed off")
        if action_sequence <= int(row["action_sequence"]):
            raise RuntimeError("rpc action_sequence is not monotonic")
        next_state = str(row["state"]) if transition.state is None else transition.state
        next_human_review_ready = (
            bool(row["human_review_ready"])
            if transition.human_review_ready is None
            else transition.human_review_ready
        )
        next_handoff_committed = (
            bool(row["handoff_committed"])
            if transition.handoff_committed is None
            else transition.handoff_committed
        )
        if transition.state is not None and not _rpc_state_transition_allowed(
            row,
            state=next_state,
            action_sequence=action_sequence,
            human_review_ready=next_human_review_ready,
            handoff_committed=next_handoff_committed,
        ):
            raise RuntimeError("invalid rpc run transition")
        if row["ats_policy"] is not None and ats_policy is not None and row["ats_policy"] != ats_policy:
            raise RuntimeError("ats policy is immutable")
        now = utc_now()
        _require_rpc_deadline_live(deadline_unix_ms)
        changed = connection.execute(
            """
            UPDATE application_rpc_runs
            SET state=?, ats_policy=COALESCE(?, ats_policy), action_sequence=?,
                current_form_step=COALESCE(?, current_form_step),
                last_observation_sha256=COALESCE(?, last_observation_sha256),
                artifact_manifest_sha256=COALESCE(?, artifact_manifest_sha256),
                human_review_ready=?, handoff_committed=?, version=version+1, updated_at=?
            WHERE run_id=? AND coordinator_id=?
              AND coordinator_pid=? AND coordinator_pgid=? AND coordinator_birth=?
            """,
            (
                next_state,
                ats_policy,
                action_sequence,
                current_form_step,
                observation_sha256,
                manifest_sha256,
                int(next_human_review_ready),
                int(next_handoff_committed),
                now,
                run_id,
                coordinator_id,
                int(row["coordinator_pid"]),
                int(row["coordinator_pgid"]),
                str(row["coordinator_birth"]),
            ),
        ).rowcount
        if changed != 1:
            raise RuntimeError("rpc run transition CAS failed")
        sequence = _append_rpc_event_locked(
            connection,
            run_id=run_id,
            request_id=request_id,
            event_type=event_type,
            summary_code=summary_code,
            action_sequence=action_sequence,
            observation_sha256=observation_sha256,
            allow_terminal=next_state in _RPC_TERMINAL_STATES,
            coordinator_id=coordinator_id,
        )
        event_row = connection.execute(
            "SELECT * FROM application_progress_events WHERE run_id=? AND sequence=?",
            (run_id, sequence),
        ).fetchone()
        if event_row is None:
            raise RuntimeError("rpc event disappeared")
        event = RpcEventInfo(
            run_id=int(event_row["run_id"]),
            sequence=int(event_row["sequence"]),
            request_id=str(event_row["request_id"]),
            action_sequence=int(event_row["action_sequence"]),
            timestamp=str(event_row["timestamp"]),
            event_type=str(event_row["event_type"]),
            summary_code=str(event_row["summary_code"]),
            observation_sha256=(
                str(event_row["observation_sha256"])
                if event_row["observation_sha256"] is not None
                else None
            ),
        )
        _require_rpc_deadline_live(deadline_unix_ms)
    except Exception:
        connection.rollback()
        raise
    connection.commit()
    return event


def append_rpc_event(
    connection: sqlite3.Connection,
    *,
    run_id: int,
    event_type: str,
    summary_code: str,
    request_id: str,
    action_sequence: int = 0,
    observation_sha256: str | None = None,
    coordinator_id: str | None = None,
) -> int:
    run_id = _require_rpc_positive_int(run_id, "run_id")
    request_id = _require_rpc_request_id(request_id)
    event_type = _require_rpc_event_type(event_type)
    summary_code = _require_rpc_summary_code(summary_code)
    action_sequence = _require_rpc_action_sequence(action_sequence)
    observation_sha256 = _require_rpc_sha256_or_none(observation_sha256, "observation_sha256")
    connection.execute("BEGIN IMMEDIATE")
    try:
        sequence = _append_rpc_event_locked(
            connection,
            run_id=run_id,
            request_id=request_id,
            event_type=event_type,
            summary_code=summary_code,
            action_sequence=action_sequence,
            observation_sha256=observation_sha256,
            coordinator_id=coordinator_id,
        )
    except Exception:
        connection.rollback()
        raise
    connection.commit()
    return sequence


def replay_rpc_events(
    connection: sqlite3.Connection,
    run_id: int,
    *,
    after_sequence: int = 0,
) -> list[RpcEventInfo]:
    run_id = _require_rpc_positive_int(run_id, "run_id")
    if type(after_sequence) is not int or after_sequence < 0:
        raise TypeError("after_sequence must be a non-negative integer")
    rows = connection.execute(
        """
        SELECT * FROM application_progress_events
        WHERE run_id=? AND sequence > ?
        ORDER BY sequence ASC
        """,
        (run_id, after_sequence),
    ).fetchall()
    return [
        RpcEventInfo(
            run_id=int(row["run_id"]),
            sequence=int(row["sequence"]),
            request_id=str(row["request_id"]),
            action_sequence=int(row["action_sequence"]),
            timestamp=str(row["timestamp"]),
            event_type=str(row["event_type"]),
            summary_code=str(row["summary_code"]),
            observation_sha256=str(row["observation_sha256"]) if row["observation_sha256"] is not None else None,
        )
        for row in rows
    ]
_HANDOFF_FINALIZATION_ARTIFACT_KEYS = frozenset(
    {
        "automated_submission",
        "child_request_id",
        "commit_token_sha256",
        "job_id",
        "observation_sha256",
        "operation",
        "parent_request_id",
        "reason_code",
        "run_id",
        "session_id",
        "status",
        "unresolved_required_count",
        "version",
    }
)


def _validate_handoff_finalization_artifact(
    value: Any,
    *,
    run_id: int,
    job_id: int,
) -> dict[str, Any]:
    if not isinstance(value, Mapping) or set(value) != _HANDOFF_FINALIZATION_ARTIFACT_KEYS:
        raise RuntimeError("handoff finalization artifact schema mismatch")
    if type(value["version"]) is not int or value["version"] != 1:
        raise RuntimeError("handoff finalization artifact schema mismatch")
    if type(value["run_id"]) is not int or value["run_id"] != run_id:
        raise RuntimeError("handoff finalization artifact provenance mismatch")
    if type(value["job_id"]) is not int or value["job_id"] != job_id:
        raise RuntimeError("handoff finalization artifact provenance mismatch")
    if value["operation"] != "browser.prepare_human_handoff":
        raise RuntimeError("handoff finalization artifact operation mismatch")
    for name in ("child_request_id", "parent_request_id"):
        try:
            _require_rpc_request_id(value[name])
        except (TypeError, ValueError):
            raise RuntimeError("handoff finalization artifact request mismatch") from None
    if (
        type(value["session_id"]) is not str
        or not value["session_id"]
        or len(value["session_id"]) > 256
        or any(ord(char) < 0x20 or ord(char) > 0x7E for char in value["session_id"])
    ):
        raise RuntimeError("handoff finalization artifact session mismatch")
    try:
        _require_rpc_sha256(value["commit_token_sha256"], "commit_token_sha256")
        _require_rpc_sha256(value["observation_sha256"], "observation_sha256")
    except (TypeError, ValueError):
        raise RuntimeError("handoff finalization artifact hash mismatch") from None
    if type(value["automated_submission"]) is not bool or value["automated_submission"] is not False:
        raise RuntimeError("handoff finalization artifact submission mismatch")
    status = value["status"]
    reason_code = value["reason_code"]
    try:
        _require_public_code(status, "status", ("review_ready", "manual", "blocked"))
        _require_public_code(reason_code, "reason_code", PUBLIC_REASON_CODES)
        _require_reason_status(status, reason_code)
    except (TypeError, ValueError):
        raise RuntimeError("handoff finalization artifact status mismatch") from None
    if (
        type(value["unresolved_required_count"]) is not int
        or value["unresolved_required_count"] < 0
        or value["unresolved_required_count"] > 10000
    ):
        raise RuntimeError("handoff finalization artifact count mismatch")
    return dict(value)


def _validate_handoff_finalization(
    value: Any,
    *,
    run_id: int,
    job_id: int,
) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError("application_finalization must be an object")
    required = {"artifact_dir", "observation_summary", "plan_summary", "reason_code", "status"}
    if set(value) != required:
        raise ValueError("application_finalization schema mismatch")
    status = value["status"]
    reason_code = value["reason_code"]
    _require_public_code(status, "status", ("review_ready", "manual", "blocked"))
    _require_public_code(reason_code, "reason_code", PUBLIC_REASON_CODES)
    _require_reason_status(status, reason_code)
    artifact_dir = value["artifact_dir"]
    if artifact_dir is not None:
        _require_run_artifact_ref(artifact_dir, run_id)
    return {
        "artifact_dir": artifact_dir,
        "observation_summary": value["observation_summary"],
        "plan_summary": value["plan_summary"],
        "reason_code": reason_code,
        "status": status,
    }


def _validate_handoff_proposal_result(
    value: Any,
    *,
    expected_reason_code: str | None = None,
    expected_observation_sha256: str | None = None,
    expected_unresolved_required_count: int | None = None,
) -> dict[str, Any]:
    """Validate the exact public result durably bound before browser commit."""
    required = {
        "outcome",
        "reason_code",
        "observation_sha256",
        "unresolved_required_count",
        "automated_submission",
    }
    if not isinstance(value, Mapping) or set(value) != required:
        raise RuntimeError("handoff proposal result schema mismatch")
    if value["outcome"] != "committed":
        raise RuntimeError("handoff proposal result outcome mismatch")
    reason_code = value["reason_code"]
    try:
        _require_public_code(reason_code, "reason_code", PUBLIC_REASON_CODES)
    except (TypeError, ValueError):
        raise RuntimeError("handoff proposal result reason mismatch") from None
    try:
        observation_sha256 = _require_rpc_sha256(
            value["observation_sha256"], "observation_sha256"
        )
    except (TypeError, ValueError):
        raise RuntimeError("handoff proposal result observation mismatch") from None
    unresolved = value["unresolved_required_count"]
    if (
        type(unresolved) is not int
        or unresolved < 0
        or unresolved > 10000
    ):
        raise RuntimeError("handoff proposal result count mismatch")
    if value["automated_submission"] is not False:
        raise RuntimeError("handoff proposal result submission mismatch")
    if expected_reason_code is not None and reason_code != expected_reason_code:
        raise RuntimeError("handoff proposal result reason provenance mismatch")
    if (
        expected_observation_sha256 is not None
        and observation_sha256 != expected_observation_sha256
    ):
        raise RuntimeError("handoff proposal result observation provenance mismatch")
    if (
        expected_unresolved_required_count is not None
        and unresolved != expected_unresolved_required_count
    ):
        raise RuntimeError("handoff proposal result count provenance mismatch")
    return {
        "outcome": "committed",
        "reason_code": reason_code,
        "observation_sha256": observation_sha256,
        "unresolved_required_count": unresolved,
        "automated_submission": False,
    }


def bind_rpc_handoff_intent(
    connection: sqlite3.Connection,
    *,
    request: ApplicationRpcRequest,
    coordinator_id: str,
    intent: Mapping[str, Any],
) -> Mapping[str, Any]:
    """Durably bind the exact handoff artifact before browser commit."""
    if not isinstance(request, ApplicationRpcRequest) or request.operation != "browser.prepare_human_handoff":
        raise ValueError("handoff intent requires browser.prepare_human_handoff")
    coordinator_id = _require_rpc_coordinator_id(coordinator_id)
    if not isinstance(intent, Mapping):
        raise TypeError("intent must be an object")
    required = {
        "application_finalization",
        "artifact_manifest_sha256",
        "artifact_sha256",
        "child_request_id",
        "commit_token_sha256",
        "job_id",
        "observation_sha256",
        "parent_request_id",
        "session_id",
        "proposal_result",
    }
    if set(intent) != required:
        raise ValueError("handoff intent schema mismatch")
    if intent["child_request_id"] != request.request_id:
        raise RuntimeError("handoff intent child request mismatch")
    request_parent = intent["parent_request_id"]
    if not isinstance(request_parent, str):
        raise RuntimeError("handoff intent parent request mismatch")
    _require_rpc_request_id(request_parent)
    _require_rpc_sha256(intent["artifact_sha256"], "artifact_sha256")
    _require_rpc_sha256(intent["artifact_manifest_sha256"], "artifact_manifest_sha256")
    _require_rpc_sha256(intent["commit_token_sha256"], "commit_token_sha256")
    _require_rpc_sha256(intent["observation_sha256"], "observation_sha256")
    if type(intent["job_id"]) is not int or intent["job_id"] <= 0:
        raise ValueError("handoff intent job_id mismatch")
    session_id = _require_exact_text(intent["session_id"], "session_id")
    connection.execute("BEGIN IMMEDIATE")
    try:
        rpc_row = _rpc_run_row(connection, request.run_id or 0)
        stored = _rpc_request_row(connection, request.request_id)
        application_row = connection.execute(
            "SELECT * FROM application_runs WHERE id=?",
            (request.run_id,),
        ).fetchone()
        if (
            rpc_row is None
            or application_row is None
            or not _rpc_owner_matches(rpc_row, coordinator_id)
            or not _rpc_mutation_allowed(rpc_row)
            or bool(rpc_row["cancellation_requested"])
            or stored is None
            or stored["state"] != "pending"
            or int(stored["run_id"]) != request.run_id
            or str(stored["operation"]) != request.operation
            or str(stored["semantic_sha256"]) != semantic_request_sha256(request)
            or str(stored["parent_request_id"]) != request_parent
            or application_row["status"] != "running"
            or application_row["session_id"] != session_id
            or int(application_row["job_id"]) != int(intent["job_id"])
        ):
            raise RuntimeError("handoff intent provenance mismatch")
        finalization = _validate_handoff_finalization(
            intent["application_finalization"],
            run_id=request.run_id or 0,
            job_id=int(application_row["job_id"]),
        )
        _validate_handoff_proposal_result(
            intent["proposal_result"],
            expected_reason_code=finalization["reason_code"],
            expected_observation_sha256=intent["observation_sha256"],
        )
        if request.payload.get("observation_sha256") != intent["observation_sha256"]:
            raise RuntimeError("handoff intent observation mismatch")
        if finalization["reason_code"] != intent["application_finalization"]["reason_code"]:
            raise RuntimeError("handoff intent finalization mismatch")
        current_json = _decode_run_json(application_row["observation_json"])
        existing = current_json.get("_handoff_intent")
        if isinstance(existing, Mapping):
            if any(existing.get(key) != intent.get(key) for key in required):
                raise RuntimeError("handoff intent conflict")
            if (
                type(existing.get("expected_rpc_version")) is not int
                or type(existing.get("expected_rpc_action_sequence")) is not int
                or not isinstance(existing.get("coordinator_identity"), Mapping)
                or not isinstance(existing.get("omp_identity"), Mapping)
            ):
                raise RuntimeError("handoff intent provenance mismatch")
            connection.commit()
            return dict(existing)
        expected_version = int(rpc_row["version"]) + 1
        bound = dict(intent)
        bound["expected_rpc_version"] = expected_version
        bound["expected_rpc_action_sequence"] = int(rpc_row["action_sequence"])
        bound["coordinator_identity"] = {
            "pid": int(rpc_row["coordinator_pid"]),
            "pgid": int(rpc_row["coordinator_pgid"]),
            "birth": str(rpc_row["coordinator_birth"]),
        }
        process_values = (
            rpc_row["omp_process_pid"],
            rpc_row["omp_process_pgid"],
            rpc_row["omp_process_birth"],
            rpc_row["omp_session_sha256"],
        )
        if any(item is None for item in process_values) or not all(item is not None for item in process_values):
            raise RuntimeError("handoff intent process provenance mismatch")
        bound["omp_identity"] = {
            "pid": int(rpc_row["omp_process_pid"]),
            "pgid": int(rpc_row["omp_process_pgid"]),
            "birth": str(rpc_row["omp_process_birth"]),
            "session_sha256": str(rpc_row["omp_session_sha256"]),
        }
        current_json["_handoff_intent"] = bound
        changed = connection.execute(
            """
            UPDATE application_runs SET observation_json=?
            WHERE id=? AND status='running' AND session_id=?
            """,
            (encode_json(current_json), request.run_id, session_id),
        ).rowcount
        if changed != 1:
            raise RuntimeError("handoff intent application CAS failed")
        changed = connection.execute(
            """
            UPDATE application_rpc_runs
            SET artifact_manifest_sha256=?, version=version+1, updated_at=?
            WHERE run_id=? AND coordinator_id=?
              AND coordinator_pid=? AND coordinator_pgid=? AND coordinator_birth=?
              AND version=? AND state IN ('starting', 'running', 'manual', 'blocked')
              AND handoff_committed=0
              AND cancellation_requested=0
            """,
            (
                intent["artifact_manifest_sha256"],
                utc_now(),
                request.run_id,
                coordinator_id,
                int(rpc_row["coordinator_pid"]),
                int(rpc_row["coordinator_pgid"]),
                str(rpc_row["coordinator_birth"]),
                int(rpc_row["version"]),
            ),
        ).rowcount
        if changed != 1:
            raise RuntimeError("handoff intent RPC CAS failed")
        connection.commit()
        return bound
    except Exception:
        connection.rollback()
        raise


def commit_rpc_proposal_result(
    connection: sqlite3.Connection,
    *,
    request: ApplicationRpcRequest,
    response: Mapping[str, Any] | str | bytes | bytearray,
    coordinator_id: str,
    action_sequence: int,
    event_type: str,
    summary_code: str,
    observation_sha256: str | None = None,
    manifest_sha256: str | None = None,
    run_state: str | None = None,
    ats_policy: str | None = None,
    current_form_step: str | None = None,
    parent_request_id: str | None = None,
    human_review_ready: bool = False,
    handoff_committed: bool = False,
    application_finalization: Mapping[str, Any] | None = None,
    recovery: bool = False,
    recovery_override: bool = False,
) -> RpcRequestInfo:
    """Atomically complete a browser proposal, update run state, and append its event."""
    if not isinstance(request, ApplicationRpcRequest):
        raise TypeError("request must be an ApplicationRpcRequest")
    if request.run_id is None or request.operation not in BROWSER_OPERATIONS:
        raise ValueError("proposal result requires a bound browser request")
    if type(recovery) is not bool or type(recovery_override) is not bool:
        raise TypeError("recovery flags must be booleans")
    if type(human_review_ready) is not bool or type(handoff_committed) is not bool:
        raise TypeError("handoff flags must be booleans")
    if recovery_override and (not recovery or not handoff_committed):
        raise ValueError("recovery override requires recovery handoff commit")
    coordinator_id = _require_rpc_coordinator_id(coordinator_id)
    action_sequence = _require_rpc_action_sequence(action_sequence)
    event_type = _require_rpc_event_type(event_type)
    summary_code = _require_rpc_summary_code(summary_code)
    observation_sha256 = _require_rpc_sha256_or_none(observation_sha256, "observation_sha256")
    manifest_sha256 = _require_rpc_sha256_or_none(manifest_sha256, "manifest_sha256")
    ats_policy = _require_rpc_ats_policy(ats_policy)
    current_form_step = _require_rpc_form_step(current_form_step)
    parsed, response_json = _parse_rpc_response(response, request=request)
    if parsed["run_id"] != request.run_id or parsed["action_sequence"] != action_sequence:
        raise ValueError("response sequence/run binding does not match proposal")
    connection.execute("BEGIN IMMEDIATE")
    try:
        row = _rpc_run_row(connection, request.run_id)
        application_row = connection.execute(
            "SELECT * FROM application_runs WHERE id=?",
            (request.run_id,),
        ).fetchone()
        stored = _rpc_request_row(connection, request.request_id)
        owner_ok = (
            row is not None
            and (
                _rpc_owner_matches(row, coordinator_id)
                if not recovery
                else (
                    str(row["coordinator_id"]) == coordinator_id
                    and (
                        _coordinator_identity_state(row) == "absent"
                        or _rpc_owner_matches(row, coordinator_id)
                    )
                )
            )
        )
        if (
            row is None
            or application_row is None
            or not owner_ok
            or not _rpc_mutation_allowed(row)
            or stored is None
            or stored["state"] != "pending"
            or int(stored["run_id"]) != request.run_id
            or str(stored["operation"]) != request.operation
            or str(stored["semantic_sha256"]) != semantic_request_sha256(request)
            or (str(stored["parent_request_id"]) if stored["parent_request_id"] is not None else None)
            != parent_request_id
        ):
            connection.rollback()
            raise RuntimeError("proposal request/run binding conflict")
        if handoff_committed != (application_finalization is not None):
            connection.rollback()
            raise RuntimeError("handoff application finalization is required")
        if run_state is None:
            next_state = str(row["state"])
        else:
            if run_state not in RPC_RUN_STATES:
                raise ValueError(f"state must be one of {RPC_RUN_STATES}")
            next_state = run_state
        finalization: dict[str, Any] | None = None
        intent: Mapping[str, Any] | None = None
        bound_finalization: dict[str, Any] | None = None
        bound_result: dict[str, Any] | None = None
        if handoff_committed:
            if request.operation != "browser.prepare_human_handoff" or not isinstance(application_row, sqlite3.Row):
                connection.rollback()
                raise RuntimeError("handoff application finalization is invalid")
            finalization = _validate_handoff_finalization(
                application_finalization,
                run_id=request.run_id,
                job_id=int(application_row["job_id"]),
            )
            if finalization["status"] != next_state or application_row["status"] != "running":
                connection.rollback()
                raise RuntimeError("handoff application state mismatch")
            intent_value = _decode_run_json(application_row["observation_json"]).get("_handoff_intent")
            if not isinstance(intent_value, Mapping):
                connection.rollback()
                raise RuntimeError("handoff intent is missing")
            intent = intent_value
            bound_finalization = _validate_handoff_finalization(
                intent.get("application_finalization"),
                run_id=request.run_id,
                job_id=int(application_row["job_id"]),
            )
            bound_result = _validate_handoff_proposal_result(
                intent.get("proposal_result"),
                expected_reason_code=bound_finalization["reason_code"],
                expected_observation_sha256=str(intent.get("observation_sha256")),
            )
            if (
                intent.get("child_request_id") != request.request_id
                or intent.get("parent_request_id") != parent_request_id
                or intent.get("job_id") != int(application_row["job_id"])
                or intent.get("observation_sha256") != observation_sha256
                or intent.get("artifact_manifest_sha256") != manifest_sha256
                or int(intent.get("expected_rpc_version", -1)) != int(row["version"])
                or int(intent.get("expected_rpc_action_sequence", -1)) != int(row["action_sequence"])
            ):
                connection.rollback()
                raise RuntimeError("handoff intent does not match proposal")
            if recovery_override:
                if (
                    finalization["status"] != "manual"
                    or finalization["reason_code"] != "page_not_stable"
                    or finalization["artifact_dir"] != bound_finalization["artifact_dir"]
                    or finalization["observation_summary"] != bound_finalization["observation_summary"]
                    or finalization["plan_summary"] != bound_finalization["plan_summary"]
                ):
                    connection.rollback()
                    raise RuntimeError("handoff recovery override mismatch")
            elif bound_finalization != finalization:
                connection.rollback()
                raise RuntimeError("handoff intent does not match proposal")
            result_value = parsed.get("result")
            validated_result = _validate_handoff_proposal_result(
                result_value,
                expected_reason_code=finalization["reason_code"],
                expected_observation_sha256=observation_sha256,
            )
            if recovery_override:
                if (
                    bound_result is None
                    or validated_result["observation_sha256"] != bound_result["observation_sha256"]
                    or validated_result["unresolved_required_count"]
                    != bound_result["unresolved_required_count"]
                ):
                    connection.rollback()
                    raise RuntimeError("handoff recovery result mismatch")
            elif bound_result != validated_result:
                connection.rollback()
                raise RuntimeError("handoff response does not match bound result")
        expected_event_sequence = int(
            connection.execute(
                "SELECT COALESCE(MAX(sequence), 0) + 1 FROM application_progress_events WHERE run_id=?",
                (request.run_id,),
            ).fetchone()[0]
        )
        if int(parsed["event_sequence"]) != expected_event_sequence:
            connection.rollback()
            raise RuntimeError("response event_sequence does not match appended event")
        if not _rpc_state_transition_allowed(
            row,
            state=next_state,
            action_sequence=action_sequence,
            human_review_ready=human_review_ready,
            handoff_committed=handoff_committed,
        ):
            connection.rollback()
            raise RuntimeError("invalid rpc run transition")
        if row["ats_policy"] is not None and ats_policy is not None and row["ats_policy"] != ats_policy:
            connection.rollback()
            raise RuntimeError("ats policy is immutable")
        if parsed["state"] != next_state:
            connection.rollback()
            raise RuntimeError("response state does not match run transition")
        if handoff_committed and finalization is not None:
            _finish_application_run_locked(
                connection,
                run_id=request.run_id,
                status=finalization["status"],
                reason_code=finalization["reason_code"],
                observation_summary=finalization["observation_summary"],
                plan_summary=finalization["plan_summary"],
                artifact_dir=finalization["artifact_dir"],
            )
        now = utc_now()
        changed = connection.execute(
            """
            UPDATE application_rpc_runs
            SET state=?, ats_policy=COALESCE(?, ats_policy), action_sequence=?,
                current_form_step=COALESCE(?, current_form_step),
                last_observation_sha256=COALESCE(?, last_observation_sha256),
                artifact_manifest_sha256=COALESCE(?, artifact_manifest_sha256),
                human_review_ready=?, handoff_committed=?, version=version+1, updated_at=?
            WHERE run_id=? AND coordinator_id=?
              AND coordinator_pid=? AND coordinator_pgid=? AND coordinator_birth=?
            """,
            (
                next_state,
                ats_policy,
                action_sequence,
                current_form_step,
                observation_sha256,
                manifest_sha256,
                int(human_review_ready),
                int(handoff_committed),
                now,
                request.run_id,
                coordinator_id,
                int(row["coordinator_pid"]),
                int(row["coordinator_pgid"]),
                str(row["coordinator_birth"]),
            ),
        ).rowcount
        if changed != 1:
            raise RuntimeError("rpc run transition CAS failed")
        _append_rpc_event_locked(
            connection,
            run_id=request.run_id,
            request_id=request.request_id,
            event_type=event_type,
            summary_code=summary_code,
            action_sequence=action_sequence,
            observation_sha256=observation_sha256,
            allow_terminal=handoff_committed,
            coordinator_id=coordinator_id,
            check_owner=not recovery,
        )
        changed = connection.execute(
            """
            UPDATE application_rpc_requests
            SET state='completed', response_json=?, completed_at=?
            WHERE request_id=? AND state='pending' AND run_id=?
              AND EXISTS (
                  SELECT 1
                  FROM application_rpc_runs
                  WHERE run_id=?
                    AND coordinator_id=?
                    AND coordinator_pid=? AND coordinator_pgid=? AND coordinator_birth=?
              )
            """,
            (
                response_json,
                now,
                request.request_id,
                request.run_id,
                request.run_id,
                coordinator_id,
                int(row["coordinator_pid"]),
                int(row["coordinator_pgid"]),
                str(row["coordinator_birth"]),
            ),
        ).rowcount
        if changed != 1:
            raise RuntimeError("proposal request completion CAS failed")
        completed = connection.execute(
            "SELECT * FROM application_rpc_requests WHERE request_id=?", (request.request_id,)
        ).fetchone()
        if completed is None:
            raise RuntimeError("completed proposal disappeared")
    except Exception:
        connection.rollback()
        raise
    try:
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    return _rpc_request_info(completed)
def _validate_rpc_failure_finalization(
    value: Mapping[str, Any],
    *,
    run_id: int,
) -> dict[str, Any]:
    """Validate the application terminal payload used by RPC failure commits."""
    if not isinstance(value, Mapping):
        raise TypeError("application_finalization must be an object")
    required = {
        "status",
        "reason_code",
        "observation_summary",
        "plan_summary",
        "artifact_dir",
    }
    if set(value) != required:
        raise RuntimeError("application failure finalization schema mismatch")
    status = _require_public_code(value["status"], "status", ("failed",))
    reason_code = _require_public_code(value["reason_code"], "reason_code", PUBLIC_REASON_CODES)
    _require_reason_status(status, reason_code)
    artifact_dir = value["artifact_dir"]
    if artifact_dir is not None:
        _require_run_artifact_ref(artifact_dir, run_id)
    return {
        "status": status,
        "reason_code": reason_code,
        "observation_summary": value["observation_summary"],
        "plan_summary": value["plan_summary"],
        "artifact_dir": artifact_dir,
    }


def _rpc_event_info_from_row(row: sqlite3.Row) -> RpcEventInfo:
    return RpcEventInfo(
        run_id=int(row["run_id"]),
        sequence=int(row["sequence"]),
        request_id=str(row["request_id"]),
        action_sequence=int(row["action_sequence"]),
        timestamp=str(row["timestamp"]),
        event_type=str(row["event_type"]),
        summary_code=str(row["summary_code"]),
        observation_sha256=(
            str(row["observation_sha256"])
            if row["observation_sha256"] is not None
            else None
        ),
    )


def _commit_rpc_failure_transaction(
    connection: sqlite3.Connection,
    *,
    run_id: int,
    coordinator_id: str,
    request_id: str,
    action_sequence: int,
    application_finalization: Mapping[str, Any],
    observation_sha256: str | None = None,
    manifest_sha256: str | None = None,
    ats_policy: str | None = None,
    current_form_step: str | None = None,
    response_request: ApplicationRpcRequest | None = None,
    response: Mapping[str, Any] | str | bytes | bytearray | None = None,
    parent_request_id: str | None = None,
) -> tuple[RpcEventInfo, RpcRequestInfo | None]:
    """Commit application/RPC failure and, optionally, a pending child response.

    The caller must provide a response request only for an incomplete browser
    proposal.  All writes, including the child completion, intentionally share
    one SQLite transaction.
    """
    run_id = _require_rpc_positive_int(run_id, "run_id")
    coordinator_id = _require_rpc_coordinator_id(coordinator_id)
    request_id = _require_rpc_request_id(request_id)
    action_sequence = _require_rpc_action_sequence(action_sequence)
    observation_sha256 = _require_rpc_sha256_or_none(
        observation_sha256, "observation_sha256"
    )
    manifest_sha256 = _require_rpc_sha256_or_none(manifest_sha256, "manifest_sha256")
    ats_policy = _require_rpc_ats_policy(ats_policy)
    current_form_step = _require_rpc_form_step(current_form_step)
    if parent_request_id is not None:
        parent_request_id = _require_rpc_request_id(parent_request_id)
    finalization = _validate_rpc_failure_finalization(
        application_finalization,
        run_id=run_id,
    )
    if response_request is None:
        if response is not None:
            raise ValueError("failure response requires a browser proposal")
        if parent_request_id is not None:
            raise ValueError("parent_request_id requires a browser proposal")
    else:
        if not isinstance(response_request, ApplicationRpcRequest):
            raise TypeError("response_request must be an ApplicationRpcRequest")
        if (
            response_request.run_id != run_id
            or response_request.operation not in BROWSER_OPERATIONS
            or response_request.request_id != request_id
            or response is None
        ):
            raise RuntimeError("failure proposal binding mismatch")
    parsed_response: Mapping[str, Any] | None = None
    response_json: str | None = None
    try:
        connection.execute("BEGIN IMMEDIATE")
        row = _rpc_run_row(connection, run_id)
        stored_event_request = _rpc_request_row(connection, request_id)
        if (
            row is None
            or stored_event_request is None
            or stored_event_request["run_id"] is None
            or int(stored_event_request["run_id"]) != run_id
            or not _rpc_owner_matches(row, coordinator_id)
        ):
            raise RuntimeError("rpc failure ownership or provenance mismatch")
        if str(row["state"]) == "failed":
            app_row = connection.execute(
                "SELECT * FROM application_runs WHERE id=?",
                (run_id,),
            ).fetchone()
            job_row = connection.execute(
                """
                SELECT j.status AS job_status
                FROM jobs AS j JOIN application_runs AS a ON a.job_id=j.id
                WHERE a.id=?
                """,
                (run_id,),
            ).fetchone()
            event_rows = connection.execute(
                """
                SELECT * FROM application_progress_events
                WHERE run_id=? AND event_type='run_failed'
                ORDER BY sequence ASC
                """,
                (run_id,),
            ).fetchall()
            expected_observation = _decode_run_json(
                _redacted_summary(finalization["observation_summary"])
            )
            expected_plan = _decode_run_json(
                _redacted_summary(finalization["plan_summary"])
            )
            if (
                app_row is None
                or str(app_row["status"]) != "failed"
                or str(app_row["reason_code"]) != finalization["reason_code"]
                or app_row["artifact_dir"] != finalization["artifact_dir"]
                or job_row is None
                or str(job_row["job_status"]) != "queued"
                or len(event_rows) != 1
                or any(
                    _decode_run_json(app_row["observation_json"]).get(key) != value
                    for key, value in expected_observation.items()
                )
                or _decode_run_json(app_row["plan_json"]) != expected_plan
                or int(row["action_sequence"]) not in {action_sequence, action_sequence - 1}
                or int(row["human_review_ready"]) != 0
                or int(row["handoff_committed"]) != 0
                or (
                    ats_policy is not None
                    and row["ats_policy"] != ats_policy
                )
                or (
                    current_form_step is not None
                    and row["current_form_step"] != current_form_step
                )
                or (
                    observation_sha256 is not None
                    and row["last_observation_sha256"] != observation_sha256
                )
                or (
                    manifest_sha256 is not None
                    and row["artifact_manifest_sha256"] != manifest_sha256
                )
            ):
                raise RuntimeError("rpc failure terminal recovery mismatch")
            event_row = event_rows[0]
            if (
                str(event_row["request_id"]) != request_id
                or int(event_row["action_sequence"]) != int(row["action_sequence"])
                or (
                    observation_sha256 is not None
                    and event_row["observation_sha256"] != observation_sha256
                )
            ):
                raise RuntimeError("rpc failure terminal event mismatch")
            completed: RpcRequestInfo | None = None
            if response_request is not None:
                if (
                    str(stored_event_request["state"]) != "completed"
                    or str(stored_event_request["operation"]) != response_request.operation
                    or str(stored_event_request["semantic_sha256"])
                    != semantic_request_sha256(response_request)
                    or (
                        str(stored_event_request["parent_request_id"])
                        if stored_event_request["parent_request_id"] is not None
                        else None
                    )
                    != parent_request_id
                    or stored_event_request["response_json"] is None
                ):
                    raise RuntimeError("rpc failure terminal child mismatch")
                stored_response, _ = _parse_rpc_response(
                    stored_event_request["response_json"],
                    request=response_request,
                )
                incoming_response, _ = _parse_rpc_response(
                    response,
                    request=response_request,
                )
                for key in ("ok", "state", "error"):
                    if stored_response.get(key) != incoming_response.get(key):
                        raise RuntimeError("rpc failure terminal response mismatch")
                if (
                    stored_response.get("run_id") != run_id
                    or stored_response.get("action_sequence")
                    != int(row["action_sequence"])
                    or stored_response.get("event_sequence")
                    != int(event_row["sequence"])
                ):
                    raise RuntimeError("rpc failure terminal response sequence mismatch")
                completed = _rpc_request_info(stored_event_request)
            elif str(stored_event_request["state"]) not in {"pending", "completed"}:
                raise RuntimeError("rpc failure terminal request mismatch")
            connection.commit()
            return _rpc_event_info_from_row(event_row), completed
        if not _rpc_mutation_allowed(row):
            raise RuntimeError("rpc failure ownership or provenance mismatch")
        if action_sequence <= int(row["action_sequence"]):
            raise RuntimeError("rpc failure action_sequence is not monotonic")
        if row["ats_policy"] is not None and ats_policy is not None and row["ats_policy"] != ats_policy:
            raise RuntimeError("ats policy is immutable")
        if not _rpc_state_transition_allowed(
            row,
            state="failed",
            action_sequence=action_sequence,
            human_review_ready=False,
            handoff_committed=False,
        ):
            raise RuntimeError("invalid rpc failure transition")

        completed = None
        if response_request is not None:
            stored_request = _stored_rpc_request(stored_event_request)
            if (
                stored_event_request["state"] != "pending"
                or str(stored_event_request["operation"]) != response_request.operation
                or str(stored_event_request["semantic_sha256"])
                != semantic_request_sha256(response_request)
                or (
                    str(stored_event_request["parent_request_id"])
                    if stored_event_request["parent_request_id"] is not None
                    else None
                )
                != parent_request_id
            ):
                raise RuntimeError("failure proposal request binding conflict")
            if stored_request.run_id != run_id:
                raise RuntimeError("failure proposal run binding conflict")
            parsed_response, response_json = _parse_rpc_response(
                response,
                request=response_request,
            )
            expected_event_sequence = int(
                connection.execute(
                    "SELECT COALESCE(MAX(sequence), 0) + 1 "
                    "FROM application_progress_events WHERE run_id=?",
                    (run_id,),
                ).fetchone()[0]
            )
            if (
                parsed_response["run_id"] != run_id
                or parsed_response["state"] != "failed"
                or parsed_response["action_sequence"] != action_sequence
                or parsed_response["event_sequence"] != expected_event_sequence
            ):
                raise RuntimeError("failure response sequence/state mismatch")
        elif str(stored_event_request["state"]) not in {"pending", "completed"}:
            raise RuntimeError("failure event request state is invalid")

        _finish_application_run_locked(
            connection,
            run_id=run_id,
            status=finalization["status"],
            reason_code=finalization["reason_code"],
            observation_summary=finalization["observation_summary"],
            plan_summary=finalization["plan_summary"],
            artifact_dir=finalization["artifact_dir"],
        )
        now = utc_now()
        if finalization["status"] == "failed":
            changed = connection.execute(
                """
                UPDATE application_runs
                SET outcome='retry', reviewed_at=?
                WHERE id=? AND status='failed'
                  AND outcome IS NULL AND reviewed_at IS NULL
                """,
                (now, run_id),
            ).rowcount
            if changed != 1:
                raise RuntimeError("RPC failure application retry CAS failed")
            changed = connection.execute(
                """
                UPDATE jobs SET status='queued'
                WHERE id=(SELECT job_id FROM application_runs WHERE id=?)
                  AND status='in_progress'
                """,
                (run_id,),
            ).rowcount
            if changed != 1:
                raise RuntimeError("RPC failure job requeue CAS failed")
        changed = connection.execute(
            """
            UPDATE application_rpc_runs
            SET state='failed', ats_policy=COALESCE(?, ats_policy),
                action_sequence=?, current_form_step=COALESCE(?, current_form_step),
                last_observation_sha256=COALESCE(?, last_observation_sha256),
                artifact_manifest_sha256=COALESCE(?, artifact_manifest_sha256),
                human_review_ready=0, handoff_committed=0, version=version+1,
                updated_at=?
            WHERE run_id=? AND coordinator_id=?
              AND coordinator_pid=? AND coordinator_pgid=? AND coordinator_birth=?
            """,
            (
                ats_policy,
                action_sequence,
                current_form_step,
                observation_sha256,
                manifest_sha256,
                now,
                run_id,
                coordinator_id,
                int(row["coordinator_pid"]),
                int(row["coordinator_pgid"]),
                str(row["coordinator_birth"]),
            ),
        ).rowcount
        if changed != 1:
            raise RuntimeError("rpc failure transition CAS failed")
        sequence = _append_rpc_event_locked(
            connection,
            run_id=run_id,
            request_id=request_id,
            event_type="run_failed",
            summary_code="failed",
            action_sequence=action_sequence,
            observation_sha256=observation_sha256,
            allow_terminal=True,
            coordinator_id=coordinator_id,
        )
        event_row = connection.execute(
            "SELECT * FROM application_progress_events WHERE run_id=? AND sequence=?",
            (run_id, sequence),
        ).fetchone()
        if event_row is None:
            raise RuntimeError("rpc failure event disappeared")
        event = _rpc_event_info_from_row(event_row)
        if response_request is not None and response_json is not None:
            changed = connection.execute(
                """
                UPDATE application_rpc_requests
                SET state='completed', response_json=?, completed_at=?
                WHERE request_id=? AND state='pending' AND run_id=?
                  AND parent_request_id IS ?
                  AND EXISTS (
                      SELECT 1 FROM application_rpc_runs
                      WHERE run_id=? AND coordinator_id=?
                        AND coordinator_pid=? AND coordinator_pgid=? AND coordinator_birth=?
                  )
                """,
                (
                    response_json,
                    now,
                    request_id,
                    run_id,
                    parent_request_id,
                    run_id,
                    coordinator_id,
                    int(row["coordinator_pid"]),
                    int(row["coordinator_pgid"]),
                    str(row["coordinator_birth"]),
                ),
            ).rowcount
            if changed != 1:
                raise RuntimeError("failure proposal completion CAS failed")
            completed_row = _rpc_request_row(connection, request_id)
            if completed_row is None:
                raise RuntimeError("completed failure proposal disappeared")
            completed = _rpc_request_info(completed_row)
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    return event, completed


def commit_rpc_failure(
    connection: sqlite3.Connection,
    *,
    run_id: int,
    coordinator_id: str,
    request_id: str,
    action_sequence: int,
    application_finalization: Mapping[str, Any],
    observation_sha256: str | None = None,
    manifest_sha256: str | None = None,
    ats_policy: str | None = None,
    current_form_step: str | None = None,
) -> RpcEventInfo:
    """Atomically finalize an owned RPC run and its application as failed."""
    event, completed = _commit_rpc_failure_transaction(
        connection,
        run_id=run_id,
        coordinator_id=coordinator_id,
        request_id=request_id,
        action_sequence=action_sequence,
        application_finalization=application_finalization,
        observation_sha256=observation_sha256,
        manifest_sha256=manifest_sha256,
        ats_policy=ats_policy,
        current_form_step=current_form_step,
    )
    if completed is not None:
        raise RuntimeError("unexpected failure proposal completion")
    return event


def commit_rpc_proposal_failure(
    connection: sqlite3.Connection,
    *,
    request: ApplicationRpcRequest,
    response: Mapping[str, Any] | str | bytes | bytearray,
    coordinator_id: str,
    action_sequence: int,
    application_finalization: Mapping[str, Any],
    observation_sha256: str | None = None,
    manifest_sha256: str | None = None,
    ats_policy: str | None = None,
    current_form_step: str | None = None,
    parent_request_id: str | None = None,
) -> RpcRequestInfo:
    """Atomically fail an RPC run, finish its application, and complete a child."""
    if not isinstance(request, ApplicationRpcRequest):
        raise TypeError("request must be an ApplicationRpcRequest")
    event, completed = _commit_rpc_failure_transaction(
        connection,
        run_id=request.run_id or 0,
        coordinator_id=coordinator_id,
        request_id=request.request_id,
        action_sequence=action_sequence,
        application_finalization=application_finalization,
        observation_sha256=observation_sha256,
        manifest_sha256=manifest_sha256,
        ats_policy=ats_policy,
        current_form_step=current_form_step,
        response_request=request,
        response=response,
        parent_request_id=parent_request_id,
    )
    if completed is None:
        raise RuntimeError("failure proposal completion is missing")
    return completed
def _exact_process_identity_state(identity: Mapping[str, Any] | None) -> str:
    if not isinstance(identity, Mapping):
        return "unknown"
    try:
        pid = int(identity["pid"])
        pgid = int(identity["pgid"])
        birth = str(identity["birth"])
    except (KeyError, TypeError, ValueError):
        return "unknown"
    if pid <= 0 or pgid <= 0 or not birth:
        return "unknown"
    try:
        return _process_group_state(
            pid,
            expected={"pid": pid, "pgid": pgid, "birth": birth},
        )
    except Exception:
        return "unknown"


_RPC_HANDOFF_TERM_TIMEOUT_SECONDS = 1.0
_RPC_HANDOFF_KILL_TIMEOUT_SECONDS = 1.0
_RPC_HANDOFF_PROBE_INTERVAL_SECONDS = 0.05


def _wait_exact_process_absent(identity: Mapping[str, Any], timeout: float) -> str:
    """Bound exact-identity probes while a supervised process exits."""
    interval = max(0.0, float(_RPC_HANDOFF_PROBE_INTERVAL_SECONDS))
    timeout = max(0.0, float(timeout))
    deadline = time.monotonic() + timeout
    max_probes = max(1, int(timeout / interval) + 2) if interval else 1
    for _ in range(max_probes):
        state = _exact_process_identity_state(identity)
        if state != "live":
            return state
        if timeout <= 0 or time.monotonic() >= deadline:
            return "live"
        time.sleep(min(interval, max(0.0, deadline - time.monotonic())))
    return _exact_process_identity_state(identity)


def _signal_exact_process_group(identity: Mapping[str, Any], signum: signal.Signals) -> None:
    """Signal only a freshly re-proven, foreign exact process group."""
    if _exact_process_identity_state(identity) != "live":
        raise RuntimeError("process identity is not conclusively live")
    try:
        pid = int(identity["pid"])
        pgid = int(identity["pgid"])
    except (KeyError, TypeError, ValueError) as exc:
        raise RuntimeError("process identity is unknown") from exc
    try:
        own_pid = os.getpid()
        own_pgid = os.getpgrp()
    except OSError as exc:
        raise RuntimeError("process identity safety probe failed") from exc
    if pid == own_pid or pgid == own_pgid:
        raise RuntimeError("refusing to signal current process group")
    try:
        os.killpg(pgid, signum)
    except ProcessLookupError:
        # The exact process may have exited between proof and signal.  The
        # caller must re-probe before deciding whether cleanup succeeded.
        return
    except (PermissionError, OSError) as exc:
        raise RuntimeError("exact process group signal failed") from exc


def _cleanup_exact_process_identity(identity: Mapping[str, Any]) -> None:
    """Terminate one exact process group with bounded TERM-then-KILL cleanup."""
    state = _exact_process_identity_state(identity)
    if state == "absent":
        return
    if state != "live":
        raise RuntimeError("process identity is unknown")
    _signal_exact_process_group(identity, signal.SIGTERM)
    state = _wait_exact_process_absent(identity, _RPC_HANDOFF_TERM_TIMEOUT_SECONDS)
    if state == "absent":
        return
    if state != "live":
        raise RuntimeError("process identity became unknown during cleanup")
    _signal_exact_process_group(identity, signal.SIGKILL)
    state = _wait_exact_process_absent(identity, _RPC_HANDOFF_KILL_TIMEOUT_SECONDS)
    if state == "absent":
        return
    if state == "unknown":
        raise RuntimeError("process identity became unknown during cleanup")
    raise RuntimeError("exact process group did not exit")


def _bound_handoff_process_identities(
    application_row: sqlite3.Row,
    observation: Mapping[str, Any],
    review: Mapping[str, Any],
) -> dict[str, dict[str, Any]]:
    """Require review, row, and observation process identities to agree."""
    process = observation.get("_process")
    if not isinstance(process, Mapping):
        raise RuntimeError("application process provenance missing")
    bound: dict[str, dict[str, Any]] = {}
    for column, kind in (("owner_pid", "owner"), ("browser_pid", "browser")):
        identity = _manifest_identity(dict(review), kind)
        if not isinstance(identity, Mapping):
            raise RuntimeError("review process provenance missing")
        row_pid = application_row[column]
        process_identity = process.get(kind)
        if (
            type(row_pid) is not int
            or row_pid != identity.get("pid")
            or not isinstance(process_identity, Mapping)
            or dict(process_identity) != dict(identity)
        ):
            raise RuntimeError("review process provenance mismatch")
        bound[kind] = dict(identity)
    return bound


def _bound_observed_handoff_process_identities(
    application_row: sqlite3.Row,
    observation: Mapping[str, Any],
) -> dict[str, dict[str, Any]]:
    """Recover owner/browser identities without trusting a damaged manifest."""
    process = observation.get("_process")
    if not isinstance(process, Mapping):
        raise RuntimeError("application process provenance missing")
    bound: dict[str, dict[str, Any]] = {}
    for column, kind in (("owner_pid", "owner"), ("browser_pid", "browser")):
        row_pid = application_row[column]
        identity = process.get(kind)
        if (
            type(row_pid) is not int
            or not isinstance(identity, Mapping)
            or identity.get("pid") != row_pid
        ):
            raise RuntimeError("application process provenance mismatch")
        bound[kind] = dict(identity)
    return bound


def _registered_shutdown_process_identities(
    rpc_row: sqlite3.Row, application_row: sqlite3.Row
) -> tuple[dict[str, Any], ...]:
    """Return only process identities durably registered for shutdown cleanup."""
    omp_values = (
        rpc_row["omp_process_pid"],
        rpc_row["omp_process_pgid"],
        rpc_row["omp_process_birth"],
        rpc_row["omp_session_sha256"],
    )
    if any(value is None for value in omp_values):
        if not all(value is None for value in omp_values):
            raise RuntimeError("OMP process provenance is incomplete")
        omp_identity: dict[str, Any] | None = None
    else:
        omp_identity = {
            "pid": int(rpc_row["omp_process_pid"]),
            "pgid": int(rpc_row["omp_process_pgid"]),
            "birth": str(rpc_row["omp_process_birth"]),
        }
    observation = _decode_run_json(application_row["observation_json"])
    process = observation.get("_process")
    if not isinstance(process, Mapping):
        process = {}
    identities: list[dict[str, Any]] = []
    if omp_identity is not None:
        identities.append(omp_identity)
    for column, kind in (("owner_pid", "owner"), ("browser_pid", "browser")):
        row_pid = application_row[column]
        identity = process.get(kind)
        if row_pid is None:
            if identity is not None:
                raise RuntimeError("application process provenance mismatch")
            continue
        if (
            type(row_pid) is not int
            or not isinstance(identity, Mapping)
            or identity.get("pid") != row_pid
        ):
            raise RuntimeError("application process provenance mismatch")
        identities.append(dict(identity))
    return tuple(identities)


def _bound_omp_process_identity(
    rpc_row: sqlite3.Row, intent: Mapping[str, Any]
) -> dict[str, Any]:
    identity = intent.get("omp_identity")
    if not isinstance(identity, Mapping):
        raise RuntimeError("OMP process provenance is missing")
    expected = {
        "pid": rpc_row["omp_process_pid"],
        "pgid": rpc_row["omp_process_pgid"],
        "birth": rpc_row["omp_process_birth"],
        "session_sha256": rpc_row["omp_session_sha256"],
    }
    if any(value is None for value in expected.values()) or any(
        identity.get(key) != value for key, value in expected.items()
    ):
        raise RuntimeError("OMP process provenance mismatch")
    return {
        "pid": int(expected["pid"]),
        "pgid": int(expected["pgid"]),
        "birth": str(expected["birth"]),
    }


def _supervise_partial_handoff_processes(
    identities: Mapping[str, Mapping[str, Any]],
    *,
    cleanup_both_live: bool = False,
) -> str:
    """Clean an exact partial (or explicitly unsafe live) handoff window."""
    owner = identities.get("owner")
    browser = identities.get("browser")
    if not isinstance(owner, Mapping) or not isinstance(browser, Mapping):
        raise RuntimeError("review process provenance missing")
    ordered = (owner, browser)
    states = tuple(_exact_process_identity_state(identity) for identity in ordered)
    if any(state == "unknown" for state in states):
        raise RuntimeError("review process state is unknown")
    if states == ("live", "live"):
        if not cleanup_both_live:
            return "healthy"
        for identity in ordered:
            _cleanup_exact_process_identity(identity)
        final_states = tuple(_exact_process_identity_state(identity) for identity in ordered)
        if final_states != ("absent", "absent"):
            raise RuntimeError("review process cleanup is incomplete")
        return "cleaned"
    if states == ("absent", "absent"):
        return "absent"
    if states in {("absent", "live"), ("live", "absent")}:
        live_identity = owner if states[0] == "live" else browser
        _cleanup_exact_process_identity(live_identity)
        final_states = tuple(_exact_process_identity_state(identity) for identity in ordered)
        if final_states != ("absent", "absent"):
            raise RuntimeError("review process cleanup is incomplete")
        return "partial"
    raise RuntimeError("review process state is unknown")
def _validate_indexed_handoff_artifacts(
    run: Any,
    manifest: Mapping[str, Any],
    *,
    run_id: int,
    job_id: int,
    intent: Mapping[str, Any],
) -> dict[str, Any]:
    artifacts = manifest.get("artifacts")
    if not isinstance(artifacts, Mapping):
        raise RuntimeError("handoff artifact index mismatch")
    descriptor = artifacts.get("handoff_finalization")
    if not isinstance(descriptor, Mapping):
        raise RuntimeError("handoff finalization artifact missing")
    if (
        descriptor.get("path") != "handoff_finalization.json"
        or descriptor.get("sha256") != intent.get("artifact_sha256")
        or type(descriptor.get("iteration")) is not int
        or descriptor.get("iteration") < 0
        or type(descriptor.get("stage")) is not str
    ):
        raise RuntimeError("handoff finalization artifact index mismatch")
    screenshot_total = 0
    screenshot_paths: set[str] = set()
    for item in artifacts.values():
        if not isinstance(item, Mapping):
            raise RuntimeError("handoff artifact index mismatch")
        path = item.get("path")
        digest = item.get("sha256")
        if (
            type(path) is not str
            or not path
            or type(digest) is not str
            or not _SHA256_RE.fullmatch(digest)
        ):
            raise RuntimeError("handoff artifact index mismatch")
        max_bytes = (
            20 * 1024 * 1024
            if path.startswith("screenshots/")
            else (10 * 1024 * 1024 if path.startswith("input/") else 8 * 1024 * 1024)
        )
        try:
            _artifacts._validate_relative_artifact_path(path)
            data = run.read_bytes(path, max_bytes=max_bytes, expected_sha256=digest)
        except Exception:
            raise RuntimeError("handoff artifact hash mismatch") from None
        if path.startswith("screenshots/") and path not in screenshot_paths:
            screenshot_paths.add(path)
            screenshot_total += len(data)
    if len(screenshot_paths) > 10 or screenshot_total > 50 * 1024 * 1024:
        raise RuntimeError("handoff artifact budget mismatch")
    try:
        raw = run.read_bytes(
            "handoff_finalization.json",
            max_bytes=_MAX_REVIEW_ARTIFACT_BYTES,
            expected_sha256=str(intent["artifact_sha256"]),
        )
        payload = json.loads(raw.decode("utf-8"))
    except Exception:
        raise RuntimeError("handoff finalization artifact unreadable") from None
    return _validate_handoff_finalization_artifact(payload, run_id=run_id, job_id=job_id)


def _reconcile_precommit_handoff(
    connection: sqlite3.Connection,
    root: ArtifactRoot,
    rpc_row: sqlite3.Row,
    application_row: sqlite3.Row,
    *,
    allow_current_owner: bool = False,
) -> bool:
    """Release a bound intent only when strict prepared-state proof exists."""
    if rpc_row["handoff_committed"] or str(rpc_row["state"]) not in {"starting", "running", "manual", "blocked"}:
        return False
    observation = _decode_run_json(application_row["observation_json"])
    intent = observation.get("_handoff_intent")
    if not isinstance(intent, Mapping):
        return False
    finalization = _validate_handoff_finalization(
        intent.get("application_finalization"),
        run_id=int(rpc_row["run_id"]),
        job_id=int(application_row["job_id"]),
    )
    _validate_handoff_proposal_result(
        intent.get("proposal_result"),
        expected_reason_code=finalization["reason_code"],
        expected_observation_sha256=str(intent.get("observation_sha256")),
    )
    run_id = int(rpc_row["run_id"])
    if (
        application_row["artifact_dir"] != root.ref_for_run(run_id)
        or application_row["session_id"] != intent.get("session_id")
        or int(application_row["job_id"]) != int(intent.get("job_id", 0))
    ):
        raise RuntimeError("precommit application provenance mismatch")
    if not _recovery_owner_is_authorized(
        rpc_row, allow_current_owner=allow_current_owner
    ):
        raise RuntimeError("precommit coordinator ownership is not authorized")
    omp_identity = _bound_omp_process_identity(rpc_row, intent)
    process = observation.get("_process")
    if not isinstance(process, Mapping):
        raise RuntimeError("precommit application process provenance missing")
    with root.open_run_dir(int(rpc_row["run_id"])) as run:
        review = _read_review_manifest(root, application_row)
        run_raw = run.read_bytes("run.json", max_bytes=_MAX_REVIEW_MANIFEST_BYTES)
        run_manifest = json.loads(run_raw.decode("utf-8"))
        review_state = review.get("state")
        precommit_closed = (
            review_state == "closed"
            and review.get("cleanup") is True
            and review.get("commit_token_sha256") is None
            and review.get("cleanup_trigger")
            in {
                "release_manifest_failed",
                "stdin_eof",
                "browser_exit",
                "browser_disconnected",
                "browser_disconnect",
                "page_close",
            }
        )
        if (
            not isinstance(run_manifest, Mapping)
            or (review_state != "prepared" and not precommit_closed)
            or review.get("commit_token_sha256") is not None
            or run_manifest.get("commit_token_sha256") != intent.get("commit_token_sha256")
            or run_manifest.get("ats_policy") != rpc_row["ats_policy"]
            or hashlib.sha256(run_raw).hexdigest() != intent.get("artifact_manifest_sha256")
            or intent.get("artifact_manifest_sha256") != rpc_row["artifact_manifest_sha256"]
            or run_manifest.get("run_id") != run_id
            or run_manifest.get("job_id") != int(application_row["job_id"])
        ):
            raise RuntimeError("precommit evidence is not conclusive")
        handoff_identities = _bound_handoff_process_identities(
            application_row, observation, review
        )
    omp_state = _exact_process_identity_state(omp_identity)
    if omp_state == "unknown":
        raise RuntimeError("precommit OMP provenance is unknown")
    if omp_state == "live":
        _cleanup_exact_process_identity(omp_identity)
    if _exact_process_identity_state(omp_identity) != "absent":
        raise RuntimeError("precommit OMP provenance is not conclusively absent")
    _supervise_partial_handoff_processes(
        handoff_identities,
        cleanup_both_live=True,
    )
    if any(
        _exact_process_identity_state(identity) != "absent"
        for identity in handoff_identities.values()
    ):
        raise RuntimeError("precommit review process is not conclusively absent")
    observation["_handoff_precommit_intent"] = dict(intent)
    if allow_current_owner:
        observation["_handoff_precommit_recovery"] = {
            "coordinator_id": str(rpc_row["coordinator_id"]),
            "coordinator_pid": int(rpc_row["coordinator_pid"]),
            "coordinator_pgid": int(rpc_row["coordinator_pgid"]),
            "coordinator_birth": str(rpc_row["coordinator_birth"]),
            "version": int(rpc_row["version"]),
        }
    observation.pop("_handoff_intent", None)
    changed = connection.execute(
        "UPDATE application_runs SET observation_json=? WHERE id=? AND status='running'",
        (encode_json(observation), int(rpc_row["run_id"])),
    ).rowcount
    if changed != 1:
        raise RuntimeError("precommit application CAS failed")
    return True


def _recover_one_rpc_handoff(
    connection: sqlite3.Connection,
    root: ArtifactRoot,
    rpc_row: sqlite3.Row,
    application_row: sqlite3.Row,
    *,
    allow_current_owner: bool = False,
) -> bool:
    run_id = int(rpc_row["run_id"])
    if rpc_row["handoff_committed"] or str(rpc_row["state"]) not in {"starting", "running", "manual", "blocked"}:
        return False
    observation = _decode_run_json(application_row["observation_json"])
    intent = observation.get("_handoff_intent")
    if not isinstance(intent, Mapping):
        return False
    if application_row["artifact_dir"] != root.ref_for_run(run_id):
        raise RuntimeError("handoff artifact run binding mismatch")
    if application_row["session_id"] != intent.get("session_id"):
        raise RuntimeError("handoff session binding mismatch")
    if not _recovery_owner_is_authorized(
        rpc_row, allow_current_owner=allow_current_owner
    ):
        raise RuntimeError("coordinator ownership is not authorized")
    omp_identity = _bound_omp_process_identity(rpc_row, intent)
    if _exact_process_identity_state(omp_identity) != "absent":
        raise RuntimeError("OMP process provenance is not conclusively absent")
    if intent.get("artifact_manifest_sha256") != rpc_row["artifact_manifest_sha256"]:
        raise RuntimeError("handoff artifact manifest binding mismatch")
    recovery_override = False
    artifact: dict[str, Any]
    bound_finalization: dict[str, Any]
    bound_result: dict[str, Any]
    with root.open_run_dir(run_id) as run:
        run_raw = run.read_bytes("run.json", max_bytes=_MAX_REVIEW_MANIFEST_BYTES)
        run_manifest = json.loads(run_raw.decode("utf-8"))
        if not isinstance(run_manifest, Mapping):
            raise RuntimeError("run manifest mismatch")
        if (
            run_manifest.get("run_id") != run_id
            or run_manifest.get("job_id") != int(application_row["job_id"])
            or run_manifest.get("ats_policy") != rpc_row["ats_policy"]
            or hashlib.sha256(run_raw).hexdigest() != intent.get("artifact_manifest_sha256")
            or run_manifest.get("commit_token_sha256") != intent.get("commit_token_sha256")
        ):
            raise RuntimeError("run manifest provenance mismatch")
        artifact = _validate_indexed_handoff_artifacts(
            run,
            run_manifest,
            run_id=run_id,
            job_id=int(application_row["job_id"]),
            intent=intent,
        )
        if (
            artifact.get("session_id") != application_row["session_id"]
            or artifact.get("commit_token_sha256") != intent.get("commit_token_sha256")
            or artifact.get("observation_sha256") != intent.get("observation_sha256")
            or artifact.get("child_request_id") != intent.get("child_request_id")
            or artifact.get("parent_request_id") != intent.get("parent_request_id")
        ):
            raise RuntimeError("handoff finalization provenance mismatch")
        bound_finalization = _validate_handoff_finalization(
            intent.get("application_finalization"),
            run_id=run_id,
            job_id=int(application_row["job_id"]),
        )
        bound_result = _validate_handoff_proposal_result(
            intent.get("proposal_result"),
            expected_reason_code=bound_finalization["reason_code"],
            expected_observation_sha256=artifact["observation_sha256"],
            expected_unresolved_required_count=artifact["unresolved_required_count"],
        )
        if (
            bound_finalization["status"] != artifact["status"]
            or bound_finalization["reason_code"] != artifact["reason_code"]
        ):
            raise RuntimeError("handoff finalization status mismatch")
        review = _read_review_manifest(root, application_row)
        state = review.get("state")
        if state not in {"open_guarded", "closed"}:
            raise RuntimeError("handoff release evidence is absent")
        if review.get("commit_token_sha256") != intent.get("commit_token_sha256"):
            raise RuntimeError("review token mismatch")
        if state == "closed" and not _window_cleanup_value(review.get("cleanup")):
            raise RuntimeError("closed review window is not safely completed")
        detached = review.get("detached")
        if detached is not None and type(detached) is not bool:
            raise RuntimeError("review detached marker mismatch")
        handoff_identities = _bound_handoff_process_identities(
            application_row, observation, review
        )
        states = tuple(
            _exact_process_identity_state(identity)
            for identity in handoff_identities.values()
        )
        if any(item == "unknown" for item in states):
            raise RuntimeError("review process state is unknown")
    stored = _rpc_request_row(connection, str(intent.get("child_request_id")))
    if stored is None or stored["state"] != "pending":
        raise RuntimeError("handoff child request is not pending")
    request = _stored_rpc_request(stored)
    if (
        request.operation != "browser.prepare_human_handoff"
        or request.run_id != run_id
        or stored["parent_request_id"] != intent.get("parent_request_id")
    ):
        raise RuntimeError("handoff child request provenance mismatch")
    parent_row = _rpc_request_row(connection, str(intent.get("parent_request_id")))
    if parent_row is None or parent_row["run_id"] != run_id or not str(parent_row["operation"]).startswith("run."):
        raise RuntimeError("handoff parent request provenance mismatch")
    cleanup_mode = _supervise_partial_handoff_processes(
        handoff_identities,
        cleanup_both_live=state == "closed"
        or (state == "open_guarded" and detached is not True),
    )
    post_states = tuple(
        _exact_process_identity_state(identity)
        for identity in handoff_identities.values()
    )
    if any(item == "unknown" for item in post_states):
        raise RuntimeError("review process state is unknown")
    if (
        state == "open_guarded"
        and detached is not True
        and post_states != ("absent", "absent")
    ):
        raise RuntimeError("detached review ownership proof is missing")
    if state == "closed" or any(item == "absent" for item in post_states) or cleanup_mode != "healthy":
        recovery_override = True
    action_sequence = int(rpc_row["action_sequence"]) + 1
    event_sequence = int(
        connection.execute(
            "SELECT COALESCE(MAX(sequence), 0) + 1 FROM application_progress_events WHERE run_id=?",
            (run_id,),
        ).fetchone()[0]
    )
    finalization = dict(bound_finalization)
    result = dict(bound_result)
    if recovery_override:
        finalization["status"] = "manual"
        finalization["reason_code"] = "page_not_stable"
        result["reason_code"] = "page_not_stable"
    response = build_application_response(
        request,
        ok=True,
        state=finalization["status"],
        action_sequence=action_sequence,
        event_sequence=event_sequence,
        result=result,
    )
    commit_rpc_proposal_result(
        connection,
        request=request,
        response=response,
        coordinator_id=str(rpc_row["coordinator_id"]),
        action_sequence=action_sequence,
        event_type="manual_intervention_required" if recovery_override else "browser_handed_off",
        summary_code="page_not_stable" if recovery_override else "handed_off",
        observation_sha256=artifact["observation_sha256"],
        manifest_sha256=str(intent["artifact_manifest_sha256"]),
        run_state=finalization["status"],
        ats_policy=str(rpc_row["ats_policy"]) if rpc_row["ats_policy"] is not None else None,
        human_review_ready=not recovery_override and artifact["status"] == "review_ready",
        handoff_committed=True,
        parent_request_id=str(intent["parent_request_id"]),
        application_finalization=finalization,
        recovery=True,
        recovery_override=recovery_override,
    )
    return True
def reconcile_committed_handoff_failure(
    connection: sqlite3.Connection,
    *,
    run_id: int,
    coordinator_id: str,
    artifact_root: ArtifactRoot,
    recovery: bool = False,
) -> bool:
    """Downgrade a committed handoff only after closed-window cleanup proof."""
    run_id = _require_rpc_positive_int(run_id, "run_id")
    coordinator_id = _require_rpc_coordinator_id(coordinator_id)
    if type(recovery) is not bool:
        raise TypeError("recovery must be a boolean")
    if not isinstance(artifact_root, ArtifactRoot):
        return False
    _bind_artifact_root(connection, artifact_root, create=False)
    connection.execute("BEGIN IMMEDIATE")
    try:
        rpc_row = _rpc_run_row(connection, run_id)
        application_row = connection.execute(
            "SELECT * FROM application_runs WHERE id=?", (run_id,)
        ).fetchone()
        owner_ok = (
            rpc_row is not None
            and (
                _rpc_owner_matches(rpc_row, coordinator_id)
                if not recovery
                else (
                    str(rpc_row["coordinator_id"]) == coordinator_id
                    and (
                        _coordinator_identity_state(rpc_row) == "absent"
                        or _rpc_owner_matches(rpc_row, coordinator_id)
                    )
                )
            )
        )
        if (
            not owner_ok
            or application_row is None
            or not bool(rpc_row["handoff_committed"])
            or str(rpc_row["state"]) not in {"review_ready", "manual", "blocked"}
            or str(application_row["status"]) not in {"review_ready", "manual", "blocked"}
            or application_row["reviewed_at"] is not None
            or application_row["outcome"] is not None
        ):
            connection.rollback()
            return False
        observation = _decode_run_json(application_row["observation_json"])
        intent = observation.get("_handoff_intent")
        if not isinstance(intent, Mapping):
            connection.rollback()
            return False
        if application_row["artifact_dir"] != artifact_root.ref_for_run(run_id):
            raise RuntimeError("handoff artifact run binding mismatch")
        if application_row["session_id"] != intent.get("session_id"):
            raise RuntimeError("handoff session binding mismatch")
        if int(intent.get("job_id", 0)) != int(application_row["job_id"]):
            raise RuntimeError("handoff job binding mismatch")
        bound_finalization = _validate_handoff_finalization(
            intent.get("application_finalization"),
            run_id=run_id,
            job_id=int(application_row["job_id"]),
        )
        bound_result = _validate_handoff_proposal_result(
            intent.get("proposal_result"),
            expected_reason_code=bound_finalization["reason_code"],
            expected_observation_sha256=str(intent.get("observation_sha256")),
        )
        with artifact_root.open_run_dir(run_id) as run:
            run_raw = run.read_bytes("run.json", max_bytes=_MAX_REVIEW_MANIFEST_BYTES)
            run_manifest = json.loads(run_raw.decode("utf-8"))
            if (
                not isinstance(run_manifest, Mapping)
                or hashlib.sha256(run_raw).hexdigest() != intent.get("artifact_manifest_sha256")
                or intent.get("artifact_manifest_sha256") != rpc_row["artifact_manifest_sha256"]
                or run_manifest.get("commit_token_sha256") != intent.get("commit_token_sha256")
                or run_manifest.get("run_id") != run_id
                or run_manifest.get("job_id") != int(application_row["job_id"])
                or run_manifest.get("ats_policy") != rpc_row["ats_policy"]
            ):
                raise RuntimeError("handoff run manifest provenance mismatch")
            artifact = _validate_indexed_handoff_artifacts(
                run,
                run_manifest,
                run_id=run_id,
                job_id=int(application_row["job_id"]),
                intent=intent,
            )
            if (
                artifact.get("session_id") != application_row["session_id"]
                or artifact.get("commit_token_sha256") != intent.get("commit_token_sha256")
                or artifact.get("observation_sha256") != intent.get("observation_sha256")
                or artifact.get("child_request_id") != intent.get("child_request_id")
                or artifact.get("parent_request_id") != intent.get("parent_request_id")
                or artifact.get("status") != bound_finalization["status"]
                or artifact.get("reason_code") != bound_finalization["reason_code"]
            ):
                raise RuntimeError("handoff finalization provenance mismatch")
            _validate_handoff_proposal_result(
                bound_result,
                expected_reason_code=artifact["reason_code"],
                expected_observation_sha256=artifact["observation_sha256"],
                expected_unresolved_required_count=artifact["unresolved_required_count"],
            )
            review = _read_review_manifest(artifact_root, application_row)
            state = review.get("state")
            detached = review.get("detached")
            cleanup_trigger = review.get("cleanup_trigger")
            cleanup_shape = (
                (
                    detached is False
                    and cleanup_trigger
                    in {
                        "release_manifest_failed",
                        "stdin_eof",
                        "browser_exit",
                        "browser_disconnected",
                        "browser_disconnect",
                        "page_close",
                    }
                )
                or (
                    detached is True
                    and cleanup_trigger
                    in {"browser_exit", "browser_disconnected", "browser_disconnect", "page_close"}
                    and (
                        cleanup_trigger not in {"browser_exit", "browser_disconnected", "browser_disconnect"}
                        or isinstance(review.get("browser_exit"), Mapping)
                    )
                )
            )
            if (
                state not in {"closed", "open_guarded"}
                or review.get("commit_token_sha256") != intent.get("commit_token_sha256")
            ):
                raise RuntimeError("closed handoff cleanup proof is missing")
            cleanup_unverified = not (
                state == "closed"
                and review.get("cleanup") is True
                and review.get("terminal_reason") in {"page_not_stable", None}
                and cleanup_shape
            )
            handoff_identities = _bound_handoff_process_identities(
                application_row, observation, review
            )
            process_states = tuple(
                _exact_process_identity_state(identity)
                for identity in handoff_identities.values()
            )
            if any(item == "unknown" for item in process_states):
                raise RuntimeError("handoff process state is unknown")
            if (
                str(rpc_row["state"]) in {"review_ready", "manual", "blocked"}
                and str(application_row["status"]) == str(rpc_row["state"])
                and state == "open_guarded"
                and detached is True
                and process_states == ("live", "live")
            ):
                omp_identity = _bound_omp_process_identity(rpc_row, intent)
                if _exact_process_identity_state(omp_identity) != "absent":
                    raise RuntimeError("healthy handoff OMP proof is not conclusive")
                if recovery:
                    if not _rpc_owner_matches(rpc_row, coordinator_id):
                        _rebind_rpc_coordinator_to_current(connection, rpc_row)
                    connection.commit()
                else:
                    connection.rollback()
                return False
            for identity in handoff_identities.values():
                if _exact_process_identity_state(identity) != "absent":
                    raise RuntimeError("closed handoff process proof is not conclusive")
        omp_identity = _bound_omp_process_identity(rpc_row, intent)
        if _exact_process_identity_state(omp_identity) != "absent":
            raise RuntimeError("closed handoff OMP proof is not conclusive")
        _require_process_groups_absent(connection, application_row)
        child_row = _rpc_request_row(connection, str(intent.get("child_request_id")))
        if child_row is None or child_row["state"] != "completed":
            raise RuntimeError("handoff child request is not completed")
        child = _stored_rpc_request(child_row)
        parent_row = _rpc_request_row(connection, str(intent.get("parent_request_id")))
        if (
            child.operation != "browser.prepare_human_handoff"
            or child.run_id != run_id
            or child_row["parent_request_id"] != intent.get("parent_request_id")
            or parent_row is None
            or parent_row["run_id"] != run_id
            or not str(parent_row["operation"]).startswith("run.")
        ):
            raise RuntimeError("handoff request provenance mismatch")
        marker = observation.get("_handoff_recovery_override")
        if (
            isinstance(marker, Mapping)
            and marker.get("reason_code") == "page_not_stable"
            and marker.get("state") == state
            and marker.get("cleanup_unverified") is cleanup_unverified
            and str(rpc_row["state"]) == "manual"
            and str(application_row["status"]) == "manual"
            and str(application_row["reason_code"]) == "page_not_stable"
        ):
            connection.commit()
            return False
        observation["_handoff_recovery_override"] = {
            "reason_code": "page_not_stable",
            "state": state,
            "cleanup_unverified": cleanup_unverified,
        }
        action_sequence = int(rpc_row["action_sequence"]) + 1
        changed = connection.execute(
            """
            UPDATE application_runs
            SET status='manual', reason_code='page_not_stable',
                observation_json=?
            WHERE id=? AND status IN ('review_ready', 'manual', 'blocked')
              AND reviewed_at IS NULL AND outcome IS NULL
            """,
            (encode_json(observation), run_id),
        ).rowcount
        if changed != 1:
            raise RuntimeError("handoff application recovery CAS failed")
        changed = connection.execute(
            """
            UPDATE application_rpc_runs
            SET state='manual', human_review_ready=0, handoff_committed=1,
                action_sequence=?, version=version+1, updated_at=?
            WHERE run_id=? AND coordinator_id=?
              AND coordinator_pid=? AND coordinator_pgid=? AND coordinator_birth=?
              AND state IN ('review_ready', 'manual', 'blocked')
              AND handoff_committed=1 AND version=?
            """,
            (
                action_sequence,
                utc_now(),
                run_id,
                coordinator_id,
                int(rpc_row["coordinator_pid"]),
                int(rpc_row["coordinator_pgid"]),
                str(rpc_row["coordinator_birth"]),
                int(rpc_row["version"]),
            ),
        ).rowcount
        if changed != 1:
            raise RuntimeError("handoff RPC recovery CAS failed")
        _append_rpc_event_locked(
            connection,
            run_id=run_id,
            request_id=child.request_id,
            event_type="manual_intervention_required",
            summary_code="page_not_stable",
            action_sequence=action_sequence,
            observation_sha256=str(intent["observation_sha256"]),
            allow_terminal=True,
            coordinator_id=coordinator_id,
            check_owner=False,
        )
    except Exception:
        connection.rollback()
        raise
    connection.commit()
    return True
def _quarantine_rpc_handoff(
    connection: sqlite3.Connection,
    *,
    rpc_row: sqlite3.Row,
    application_row: sqlite3.Row,
    reason: str = "page_not_stable",
    allow_current_owner: bool = False,
) -> bool:
    """Fail closed without requeueing when handoff provenance is unsafe."""
    run_id = int(rpc_row["run_id"])
    if reason != "page_not_stable":
        raise ValueError("unsupported handoff quarantine reason")
    connection.execute("BEGIN IMMEDIATE")
    try:
        current_rpc = _rpc_run_row(connection, run_id)
        current_app = connection.execute(
            "SELECT * FROM application_runs WHERE id=?", (run_id,)
        ).fetchone()
        if (
            current_rpc is None
            or current_app is None
            or str(current_rpc["coordinator_id"]) != str(rpc_row["coordinator_id"])
            or not _recovery_owner_is_authorized(
                current_rpc, allow_current_owner=allow_current_owner
            )
            or str(current_rpc["state"]) not in {
                "starting",
                "running",
                "review_ready",
                "manual",
                "blocked",
            }
            or str(current_app["status"]) not in {
                "running",
                "review_ready",
                "manual",
                "blocked",
            }
            or current_app["reviewed_at"] is not None
            or current_app["outcome"] is not None
        ):
            connection.rollback()
            return False
        observation = _decode_run_json(current_app["observation_json"])
        intent = observation.get("_handoff_intent")
        if not isinstance(intent, Mapping):
            connection.rollback()
            return False
        marker = observation.get("_handoff_recovery_quarantine")
        if (
            isinstance(marker, Mapping)
            and marker.get("reason_code") == reason
            and str(current_rpc["state"]) == "manual"
            and str(current_app["status"]) == "manual"
            and str(current_app["reason_code"]) == reason
        ):
            connection.commit()
            return False
        child_id = intent.get("child_request_id")
        parent_id = intent.get("parent_request_id")
        if type(child_id) is not str or type(parent_id) is not str:
            raise RuntimeError("handoff request provenance mismatch")
        child_row = _rpc_request_row(connection, child_id)
        parent_row = _rpc_request_row(connection, parent_id)
        if (
            child_row is None
            or parent_row is None
            or child_row["run_id"] is None
            or int(child_row["run_id"]) != run_id
            or parent_row["run_id"] is None
            or int(parent_row["run_id"]) != run_id
            or child_row["parent_request_id"] != parent_id
            or not str(parent_row["operation"]).startswith("run.")
        ):
            raise RuntimeError("handoff request provenance mismatch")
        child = _stored_rpc_request(child_row)
        if child.operation != "browser.prepare_human_handoff":
            raise RuntimeError("handoff child request provenance mismatch")
        child_pending = child_row["state"] == "pending"
        observation["_handoff_recovery_quarantine"] = {
            "reason_code": reason,
            "handoff_committed": bool(current_rpc["handoff_committed"]),
        }
        action_sequence = int(current_rpc["action_sequence"]) + 1
        changed = connection.execute(
            """
            UPDATE application_runs
            SET status='manual', reason_code='page_not_stable',
                finished_at=COALESCE(finished_at, ?),
                observation_json=?
            WHERE id=? AND status IN ('running', 'review_ready', 'manual', 'blocked')
              AND reviewed_at IS NULL AND outcome IS NULL
            """,
            (utc_now(), encode_json(observation), run_id),
        ).rowcount
        if changed != 1:
            raise RuntimeError("handoff quarantine application CAS failed")
        changed = connection.execute(
            """
            UPDATE application_rpc_runs
            SET state='manual', human_review_ready=0,
                action_sequence=?, version=version+1, updated_at=?
            WHERE run_id=? AND coordinator_id=?
              AND coordinator_pid=? AND coordinator_pgid=? AND coordinator_birth=?
              AND state IN ('starting', 'running', 'review_ready', 'manual', 'blocked')
              AND handoff_committed=? AND version=?
            """,
            (
                action_sequence,
                utc_now(),
                run_id,
                str(current_rpc["coordinator_id"]),
                int(current_rpc["coordinator_pid"]),
                int(current_rpc["coordinator_pgid"]),
                str(current_rpc["coordinator_birth"]),
                int(bool(current_rpc["handoff_committed"])),
                int(current_rpc["version"]),
            ),
        ).rowcount
        if changed != 1:
            raise RuntimeError("handoff quarantine RPC CAS failed")
        observation_sha256 = intent.get("observation_sha256")
        if not isinstance(observation_sha256, str) or not _SHA256_RE.fullmatch(observation_sha256):
            observation_sha256 = "0" * 64
        event_sequence = _append_rpc_event_locked(
            connection,
            run_id=run_id,
            request_id=child_id,
            event_type="manual_intervention_required",
            summary_code="page_not_stable",
            action_sequence=action_sequence,
            observation_sha256=observation_sha256,
            allow_terminal=bool(current_rpc["handoff_committed"]),
            coordinator_id=str(current_rpc["coordinator_id"]),
            check_owner=False,
        )
        if child_pending:
            response = build_application_response(
                child,
                ok=True,
                state="manual",
                action_sequence=action_sequence,
                event_sequence=event_sequence,
                result={
                    "outcome": "committed",
                    "reason_code": "page_not_stable",
                    "observation_sha256": observation_sha256,
                    "unresolved_required_count": 0,
                    "automated_submission": False,
                },
            )
            changed = connection.execute(
                """
                UPDATE application_rpc_requests
                SET state='completed', response_json=?, completed_at=?
                WHERE request_id=? AND state='pending' AND run_id=?
                """,
                (
                    _canonical_rpc_json(response),
                    utc_now(),
                    child_id,
                    run_id,
                ),
            ).rowcount
            if changed != 1:
                raise RuntimeError("handoff child quarantine completion CAS failed")
    except Exception:
        connection.rollback()
        raise
    connection.commit()
    return True
def _prepare_rpc_handoff_recovery(
    rpc_row: sqlite3.Row,
    application_row: sqlite3.Row,
    intent: Mapping[str, Any],
    review: Mapping[str, Any],
    *,
    committed: bool,
    allow_current_owner: bool = False,
) -> str:
    """Validate exact ownership, then clean only stale foreign groups."""
    if not _recovery_owner_is_authorized(
        rpc_row, allow_current_owner=allow_current_owner
    ):
        raise RuntimeError("coordinator ownership is not authorized")
    observation = _decode_run_json(application_row["observation_json"])
    identities = _bound_handoff_process_identities(application_row, observation, review)
    omp_identity = _bound_omp_process_identity(rpc_row, intent)
    omp_state = _exact_process_identity_state(omp_identity)
    if omp_state == "unknown":
        raise RuntimeError("OMP process state is unknown")
    if omp_state == "live":
        _cleanup_exact_process_identity(omp_identity)
    if _exact_process_identity_state(omp_identity) != "absent":
        raise RuntimeError("OMP process cleanup is incomplete")
    state = review.get("state")
    if state not in {"open_guarded", "closed", "prepared"}:
        raise RuntimeError("handoff review state is unknown")
    detached = review.get("detached")
    if detached is not None and type(detached) is not bool:
        raise RuntimeError("review detached marker mismatch")
    process_states = tuple(
        _exact_process_identity_state(identity) for identity in identities.values()
    )
    if any(item == "unknown" for item in process_states):
        raise RuntimeError("review process state is unknown")
    cleanup_both_live = state != "open_guarded" or detached is not True
    cleanup_mode = _supervise_partial_handoff_processes(
        identities,
        cleanup_both_live=cleanup_both_live,
    )
    if cleanup_mode != "healthy":
        post_states = tuple(
            _exact_process_identity_state(identity)
            for identity in identities.values()
        )
        if post_states != ("absent", "absent"):
            raise RuntimeError("review process cleanup is incomplete")
    return cleanup_mode


def _validate_committed_handoff_fast_path(
    root: ArtifactRoot,
    application_row: sqlite3.Row,
    rpc_row: sqlite3.Row,
    intent: Mapping[str, Any],
    review: Mapping[str, Any],
) -> None:
    """Validate the durable run/token binding before preserving a live window."""
    durable_state = str(rpc_row["state"])
    if (
        durable_state not in {"review_ready", "manual", "blocked"}
        or str(application_row["status"]) != durable_state
    ):
        raise RuntimeError("committed handoff durable state mismatch")
    if review.get("commit_token_sha256") != intent.get("commit_token_sha256"):
        raise RuntimeError("review token mismatch")
    if review.get("ats_policy") != rpc_row["ats_policy"]:
        raise RuntimeError("review ATS policy mismatch")
    run_id = int(rpc_row["run_id"])
    with root.open_run_dir(run_id) as run:
        raw = run.read_bytes("run.json", max_bytes=_MAX_REVIEW_MANIFEST_BYTES)
    manifest = json.loads(raw.decode("utf-8"))
    if (
        not isinstance(manifest, Mapping)
        or hashlib.sha256(raw).hexdigest() != intent.get("artifact_manifest_sha256")
        or intent.get("artifact_manifest_sha256") != rpc_row["artifact_manifest_sha256"]
        or manifest.get("run_id") != run_id
        or manifest.get("job_id") != int(application_row["job_id"])
        or manifest.get("ats_policy") != rpc_row["ats_policy"]
        or manifest.get("commit_token_sha256") != intent.get("commit_token_sha256")
    ):
        raise RuntimeError("committed handoff fast-path provenance mismatch")


def _drain_bound_omp_process(
    rpc_row: sqlite3.Row,
    intent: Mapping[str, Any],
    *,
    allow_current_owner: bool = False,
) -> None:
    """Drain the independently bound OMP group before manifest decisions."""
    if not _recovery_owner_is_authorized(
        rpc_row, allow_current_owner=allow_current_owner
    ):
        raise RuntimeError("coordinator ownership is not authorized")
    identity = _bound_omp_process_identity(rpc_row, intent)
    state = _exact_process_identity_state(identity)
    if state == "unknown":
        raise RuntimeError("OMP process state is unknown")
    if state == "live":
        _cleanup_exact_process_identity(identity)
    if _exact_process_identity_state(identity) != "absent":
        raise RuntimeError("OMP process cleanup is incomplete")
def recover_rpc_handoffs(
    connection: sqlite3.Connection,
    *,
    artifact_root: ArtifactRoot,
    expected_coordinator_id: str | None = None,
) -> RpcHandoffRecoveryResult:
    """Replay durable post-commit handoffs before abandoned-run cleanup."""
    if not isinstance(artifact_root, ArtifactRoot):
        raise TypeError("artifact_root must be an ArtifactRoot")
    if expected_coordinator_id is not None:
        expected_coordinator_id = _require_rpc_coordinator_id(expected_coordinator_id)
    _bind_artifact_root(connection, artifact_root, create=False)
    committed_rows = connection.execute(
        """
        SELECT r.*, a.*
        FROM application_rpc_runs r
        JOIN application_runs a ON a.id=r.run_id
        WHERE r.handoff_committed=1
          AND r.state IN ('review_ready', 'manual', 'blocked')
        ORDER BY r.run_id
        """
    ).fetchall()
    recovered: list[int] = []
    conflicts: list[int] = []
    for combined in committed_rows:
        run_id = int(combined["run_id"])
        rpc_row = _rpc_run_row(connection, run_id)
        application_row = connection.execute(
            "SELECT * FROM application_runs WHERE id=?", (run_id,)
        ).fetchone()
        if rpc_row is None or application_row is None:
            continue
        if (
            expected_coordinator_id is not None
            and str(rpc_row["coordinator_id"]) != expected_coordinator_id
        ):
            continue
        observation = _decode_run_json(application_row["observation_json"])
        intent = observation.get("_handoff_intent")
        if not isinstance(intent, Mapping):
            continue
        quarantine_marker = observation.get("_handoff_recovery_quarantine")
        if isinstance(quarantine_marker, Mapping):
            continue
        try:
            claimed_rpc = _claim_rpc_handoff_recovery(
                connection,
                rpc_row,
                expected_coordinator_id=expected_coordinator_id,
            )
        except Exception:
            conflicts.append(run_id)
            continue
        if claimed_rpc is None:
            continue
        rpc_row = claimed_rpc
        application_row = connection.execute(
            "SELECT * FROM application_runs WHERE id=?", (run_id,)
        ).fetchone()
        if application_row is None:
            conflicts.append(run_id)
            continue
        observation = _decode_run_json(application_row["observation_json"])
        omp_drained = False
        try:
            _drain_bound_omp_process(
                rpc_row, intent, allow_current_owner=True
            )
            omp_drained = True
            review = _read_review_manifest(artifact_root, application_row)
            cleanup_mode = _prepare_rpc_handoff_recovery(
                rpc_row,
                application_row,
                intent,
                review,
                committed=True,
                allow_current_owner=True,
            )
            _validate_committed_handoff_fast_path(
                artifact_root,
                application_row,
                rpc_row,
                intent,
                review,
            )
            state = review.get("state")
            detached = review.get("detached")
            if (
                state == "open_guarded"
                and detached is True
                and cleanup_mode == "healthy"
            ):
                # Ownership was claimed before cleanup; leave the healthy
                # handoff bound to this live coordinator.
                continue
            if state not in {"closed", "open_guarded"}:
                raise RuntimeError("handoff release evidence is absent")
            if reconcile_committed_handoff_failure(
                connection,
                run_id=run_id,
                coordinator_id=str(rpc_row["coordinator_id"]),
                artifact_root=artifact_root,
                recovery=True,
            ):
                recovered.append(run_id)
            else:
                conflicts.append(run_id)
        except Exception:
            try:
                if not omp_drained or not _recovery_owner_is_authorized(
                    rpc_row, allow_current_owner=True
                ):
                    raise RuntimeError("handoff process cleanup is not authorized")
                identities = _bound_observed_handoff_process_identities(
                    application_row, observation
                )
                _supervise_partial_handoff_processes(
                    identities, cleanup_both_live=True
                )
                if tuple(
                    _exact_process_identity_state(identity)
                    for identity in identities.values()
                ) != ("absent", "absent"):
                    raise RuntimeError("handoff process cleanup is incomplete")
                if _quarantine_rpc_handoff(
                    connection,
                    rpc_row=rpc_row,
                    application_row=application_row,
                    allow_current_owner=True,
                ):
                    recovered.append(run_id)
                else:
                    conflicts.append(run_id)
            except Exception:
                conflicts.append(run_id)
    rows = connection.execute(
        """
        SELECT r.*, a.*
        FROM application_rpc_runs r
        JOIN application_runs a ON a.id=r.run_id
        WHERE r.handoff_committed=0
          AND r.state IN ('starting', 'running', 'manual', 'blocked')
        ORDER BY r.run_id
        """
    ).fetchall()
    for combined in rows:
        run_id = int(combined["run_id"])
        rpc_row = _rpc_run_row(connection, run_id)
        application_row = connection.execute(
            "SELECT * FROM application_runs WHERE id=?", (run_id,)
        ).fetchone()
        if rpc_row is None or application_row is None:
            continue
        if (
            expected_coordinator_id is not None
            and str(rpc_row["coordinator_id"]) != expected_coordinator_id
        ):
            continue
        observation = _decode_run_json(application_row["observation_json"])
        intent = observation.get("_handoff_intent")
        if not isinstance(intent, Mapping):
            continue
        quarantine_marker = observation.get("_handoff_recovery_quarantine")
        if isinstance(quarantine_marker, Mapping):
            continue
        try:
            claimed_rpc = _claim_rpc_handoff_recovery(
                connection,
                rpc_row,
                expected_coordinator_id=expected_coordinator_id,
            )
        except Exception:
            conflicts.append(run_id)
            continue
        if claimed_rpc is None:
            continue
        rpc_row = claimed_rpc
        application_row = connection.execute(
            "SELECT * FROM application_runs WHERE id=?", (run_id,)
        ).fetchone()
        if application_row is None:
            conflicts.append(run_id)
            continue
        observation = _decode_run_json(application_row["observation_json"])
        omp_drained = False
        committed_recovery = False
        try:
            _drain_bound_omp_process(
                rpc_row, intent, allow_current_owner=True
            )
            omp_drained = True
            review = _read_review_manifest(artifact_root, application_row)
            _prepare_rpc_handoff_recovery(
                rpc_row,
                application_row,
                intent,
                review,
                committed=False,
                allow_current_owner=True,
            )
            try:
                if _recover_one_rpc_handoff(
                    connection, artifact_root, rpc_row, application_row,
                    allow_current_owner=True
                ):
                    committed_rpc = _rpc_run_row(connection, run_id)
                    committed_app = connection.execute(
                        "SELECT status FROM application_runs WHERE id=?",
                        (run_id,),
                    ).fetchone()
                    if (
                        committed_rpc is None
                        or not bool(committed_rpc["handoff_committed"])
                        or str(committed_rpc["state"])
                        not in {"review_ready", "manual", "blocked"}
                        or committed_app is None
                        or str(committed_app["status"]) != str(committed_rpc["state"])
                    ):
                        raise RuntimeError(
                            "recovered handoff durable state is incomplete"
                        )
                    committed_recovery = True
                    recovered.append(run_id)
                    continue
            except Exception:
                if committed_recovery:
                    raise
            try:
                if _reconcile_precommit_handoff(
                    connection,
                    artifact_root,
                    rpc_row,
                    application_row,
                    allow_current_owner=True,
                ):
                    connection.commit()
                    recovered.append(run_id)
                    continue
            except Exception:
                pass
            raise RuntimeError("handoff recovery did not make progress")
        except Exception:
            try:
                if not omp_drained or not _recovery_owner_is_authorized(
                    rpc_row, allow_current_owner=True
                ):
                    raise RuntimeError("handoff process cleanup is not authorized")
                identities = _bound_observed_handoff_process_identities(
                    application_row, observation
                )
                _supervise_partial_handoff_processes(
                    identities, cleanup_both_live=True
                )
                if tuple(
                    _exact_process_identity_state(identity)
                    for identity in identities.values()
                ) != ("absent", "absent"):
                    raise RuntimeError("handoff process cleanup is incomplete")
                if _quarantine_rpc_handoff(
                    connection,
                    rpc_row=rpc_row,
                    application_row=application_row,
                    allow_current_owner=True,
                ):
                    recovered.append(run_id)
                else:
                    conflicts.append(run_id)
            except Exception:
                conflicts.append(run_id)
    if not recovered and not conflicts:
        return RpcHandoffRecoveryResult(status="noop")
    return RpcHandoffRecoveryResult(
        status="partial" if recovered and conflicts else ("recovered" if recovered else "conflict"),
        run_ids=tuple(recovered),
        conflict_run_ids=tuple(conflicts),
    )
def _rpc_reconciliation_response(
    request: ApplicationRpcRequest,
    *,
    run_id: int | None,
    action_sequence: int,
    event_sequence: int,
) -> tuple[Mapping[str, Any], str]:
    response: dict[str, Any] = {
        "protocol_version": APPLICATION_RPC_PROTOCOL_VERSION,
        "request_id": request.request_id,
        "operation": request.operation,
        "ok": False,
        "run_id": run_id,
        "state": "failed",
        "action_sequence": action_sequence,
        "event_sequence": event_sequence,
        "result": None,
        "error": {
            "code": "internal_error",
            "message": PUBLIC_ERROR_MESSAGES["internal_error"],
        },
    }
    parsed = parse_application_response(response, request=request)
    return parsed, _canonical_rpc_json(parsed)


def _coordinator_identity_state(row: sqlite3.Row) -> str:
    """Probe only the recorded coordinator PID identity.

    A coordinator may share its process group with a shell or terminal.  The
    owner is therefore absent once the exact PID is gone; surviving members of
    the recorded PGID do not keep ownership alive.
    """
    try:
        pid = int(row["coordinator_pid"])
        pgid = int(row["coordinator_pgid"])
        birth = str(row["coordinator_birth"])
    except (KeyError, TypeError, ValueError):
        return "unknown"
    if pid <= 0 or pgid <= 0 or not birth:
        return "unknown"
    try:
        current = _capture_process_identity(pid)
    except Exception:
        return "unknown"
    if current is None:
        return "absent"
    if not isinstance(current, Mapping) or current.get("probe_error"):
        return "unknown"
    observed_pid = current.get("pid")
    observed_pgid = current.get("pgid")
    observed_birth = current.get("birth")
    if (
        type(observed_pid) is not int
        or observed_pid <= 0
        or type(observed_pgid) is not int
        or observed_pgid <= 0
        or type(observed_birth) is not str
        or not observed_birth
    ):
        return "unknown"
    if (
        observed_pid != pid
        or observed_pgid != pgid
        or observed_birth != birth
    ):
        return "absent"
    return "live"


def _recovery_owner_is_authorized(
    rpc_row: sqlite3.Row,
    *,
    allow_current_owner: bool = False,
) -> bool:
    """Authorize recovery from a fresh owner snapshot."""
    if allow_current_owner:
        return _rpc_owner_matches(rpc_row, str(rpc_row["coordinator_id"]))
    return _coordinator_identity_state(rpc_row) == "absent"


def _rebind_rpc_coordinator_to_current(
    connection: sqlite3.Connection,
    rpc_row: sqlite3.Row,
    *,
    increment_version: bool = True,
) -> dict[str, Any]:
    """Rebind recovery ownership only after proving the prior owner is gone."""
    if _coordinator_identity_state(rpc_row) != "absent":
        raise RuntimeError("prior coordinator ownership is not absent")
    identity = _capture_rpc_coordinator_identity()
    if not isinstance(identity, Mapping):
        raise RuntimeError("current coordinator identity is unavailable")
    pid = identity.get("pid")
    pgid = identity.get("pgid")
    birth = identity.get("birth")
    if (
        type(pid) is not int
        or pid <= 0
        or type(pgid) is not int
        or pgid <= 0
        or type(birth) is not str
        or not birth
    ):
        raise RuntimeError("current coordinator identity is invalid")
    version_sql = "version=version+1" if increment_version else "version=version"
    changed = connection.execute(
        f"""
        UPDATE application_rpc_runs
        SET coordinator_pid=?, coordinator_pgid=?, coordinator_birth=?,
            {version_sql}, updated_at=?
        WHERE run_id=? AND coordinator_id=?
          AND coordinator_pid=? AND coordinator_pgid=? AND coordinator_birth=?
          AND state IN ('starting', 'running', 'manual', 'blocked', 'review_ready')
          AND version=?
        """,
        (
            pid,
            pgid,
            birth,
            utc_now(),
            int(rpc_row["run_id"]),
            str(rpc_row["coordinator_id"]),
            int(rpc_row["coordinator_pid"]),
            int(rpc_row["coordinator_pgid"]),
            str(rpc_row["coordinator_birth"]),
            int(rpc_row["version"]),
        ),
    ).rowcount
    if changed != 1:
        raise RuntimeError("rpc coordinator rebind CAS failed")
    return {"pid": pid, "pgid": pgid, "birth": birth}


def _claim_rpc_handoff_recovery(
    connection: sqlite3.Connection,
    rpc_row: sqlite3.Row,
    *,
    expected_coordinator_id: str | None,
) -> sqlite3.Row | None:
    """Atomically claim one stale handoff before any process cleanup."""
    run_id = int(rpc_row["run_id"])
    connection.execute("BEGIN IMMEDIATE")
    try:
        current = _rpc_run_row(connection, run_id)
        if current is None:
            connection.rollback()
            return None
        if (
            expected_coordinator_id is not None
            and str(current["coordinator_id"]) != expected_coordinator_id
        ):
            connection.rollback()
            return None
        if any(
            current[column] != rpc_row[column]
            for column in (
                "coordinator_id",
                "coordinator_pid",
                "coordinator_pgid",
                "coordinator_birth",
                "version",
                "state",
                "handoff_committed",
            )
        ):
            connection.rollback()
            return None
        if (
            _coordinator_identity_state(current) != "absent"
            or _rpc_owner_matches(current, str(current["coordinator_id"]))
        ):
            connection.rollback()
            return None
        _rebind_rpc_coordinator_to_current(
            connection, current, increment_version=False
        )
        rebound = _rpc_run_row(connection, run_id)
        if rebound is None or not _rpc_owner_matches(
            rebound, str(rebound["coordinator_id"])
        ):
            raise RuntimeError("rpc coordinator recovery claim is not current")
    except Exception:
        connection.rollback()
        raise
    connection.commit()
    return rebound


def _precommit_recovery_owner_is_authorized(
    rpc_row: sqlite3.Row,
    application_row: sqlite3.Row,
) -> bool:
    """Allow generic requeue only for the just-cleared recovery lease."""
    observation = _decode_run_json(application_row["observation_json"])
    marker = observation.get("_handoff_precommit_recovery")
    if not isinstance(marker, Mapping):
        return False
    try:
        if (
            marker.get("coordinator_id") != str(rpc_row["coordinator_id"])
            or type(marker.get("coordinator_pid")) is not int
            or int(marker["coordinator_pid"]) != int(rpc_row["coordinator_pid"])
            or type(marker.get("coordinator_pgid")) is not int
            or int(marker["coordinator_pgid"]) != int(rpc_row["coordinator_pgid"])
            or marker.get("coordinator_birth") != str(rpc_row["coordinator_birth"])
            or type(marker.get("version")) is not int
            or int(marker["version"]) != int(rpc_row["version"])
        ):
            return False
    except (KeyError, TypeError, ValueError):
        return False
    owner_state = _coordinator_identity_state(rpc_row)
    if owner_state == "absent":
        return True
    if owner_state != "live":
        return False
    return _rpc_owner_matches(rpc_row, str(rpc_row["coordinator_id"]))
def _process_identity_conflict(
    *,
    pid: Any,
    pgid: Any,
    birth: Any,
) -> bool:
    """Return whether a persisted process identity cannot be proven absent."""
    if pid is None and pgid is None and birth is None:
        return False
    if (
        type(pid) is not int
        or pid <= 0
        or type(pgid) is not int
        or pgid <= 0
        or type(birth) is not str
        or not birth
    ):
        return True
    expected = {"pid": pid, "pgid": pgid, "birth": birth}
    try:
        return _process_group_state(pid, expected=expected) != "absent"
    except Exception:
        return True


def _application_process_conflict(application_row: sqlite3.Row) -> bool:
    observation = _decode_run_json(application_row["observation_json"])
    process = observation.get("_process")
    if observation.get("_spawn_attempted") is True:
        if not (
            type(application_row["owner_pid"]) is int
            and type(application_row["browser_pid"]) is int
        ):
            return True
        registered_process = process
        if not isinstance(registered_process, Mapping):
            return True
        if not isinstance(registered_process.get("owner"), Mapping) or not isinstance(
            registered_process.get("browser"), Mapping
        ):
            return True
    for column, kind in (("owner_pid", "owner"), ("browser_pid", "browser")):
        pid = application_row[column]
        if pid is None:
            continue
        if not isinstance(process, Mapping):
            return True
        identity = process.get(kind)
        if not isinstance(identity, Mapping):
            return True
        if _process_identity_conflict(
            pid=pid,
            pgid=identity.get("pgid"),
            birth=identity.get("birth"),
        ):
            return True
        if identity.get("pid") != pid:
            return True
    return False


def _rpc_omp_process_conflict(rpc_row: sqlite3.Row | None) -> bool:
    if rpc_row is None:
        return False
    values = (
        rpc_row["omp_process_pid"],
        rpc_row["omp_process_pgid"],
        rpc_row["omp_process_birth"],
        rpc_row["omp_session_sha256"],
    )
    if all(value is None for value in values):
        return False
    if (
        _process_identity_conflict(
            pid=values[0],
            pgid=values[1],
            birth=values[2],
        )
        or type(values[3]) is not str
        or _SHA256_RE.fullmatch(values[3]) is None
    ):
        return True
    return False


def _prior_attempt_process_conflict(
    connection: sqlite3.Connection,
    *,
    job_id: int,
) -> bool:
    """Fail closed while any prior application/OMP process is unproven absent."""
    rows = connection.execute(
        """
        SELECT a.*, r.omp_process_pid, r.omp_process_pgid,
               r.omp_process_birth, r.omp_session_sha256
        FROM application_runs AS a
        LEFT JOIN application_rpc_runs AS r ON r.run_id = a.id
        WHERE a.job_id=?
        ORDER BY a.id DESC
        """,
        (job_id,),
    ).fetchall()
    for row in rows:
        observation = _decode_run_json(row["observation_json"])
        if isinstance(observation.get("_launch_cleanup_quarantine"), Mapping) or isinstance(
            observation.get("_handoff_intent"), Mapping
        ):
            return True
        if observation.get("_omp_spawn_attempted") is True:
            if any(
                row[column] is None
                for column in (
                    "omp_process_pid",
                    "omp_process_pgid",
                    "omp_process_birth",
                    "omp_session_sha256",
                )
            ):
                return True
        if _application_process_conflict(row) or _rpc_omp_process_conflict(row):
            return True
    return False


def _rpc_ownership_conflict(
    rpc_row: sqlite3.Row,
    application_row: sqlite3.Row,
    *,
    allow_precommit_recovery: bool = False,
) -> bool:
    coordinator_values = (
        rpc_row["coordinator_pid"],
        rpc_row["coordinator_pgid"],
        rpc_row["coordinator_birth"],
    )
    if any(value is None for value in coordinator_values) or not all(
        value is not None for value in coordinator_values
    ):
        return True
    if (
        _coordinator_identity_state(rpc_row) != "absent"
        and not allow_precommit_recovery
    ):
        return True
    process_values = (
        rpc_row["omp_process_pid"],
        rpc_row["omp_process_pgid"],
        rpc_row["omp_process_birth"],
        rpc_row["omp_session_sha256"],
    )
    if any(value is None for value in process_values) and not all(value is None for value in process_values):
        return True
    if all(value is not None for value in process_values):
        expected = {
            "pid": int(rpc_row["omp_process_pid"]),
            "pgid": int(rpc_row["omp_process_pgid"]),
            "birth": str(rpc_row["omp_process_birth"]),
        }
        if _process_group_state(expected["pid"], expected=expected) != "absent":
            return True
    observation = _decode_run_json(application_row["observation_json"])
    if observation.get("_omp_spawn_attempted") is True and not allow_precommit_recovery:
        return True
    # A bound handoff intent is durable evidence that commit may already have
    # crossed the browser's irreversible open_guarded point.  Startup recovery
    # must decide it; generic abandoned reconciliation must never requeue it.
    if isinstance(observation.get("_handoff_intent"), dict):
        return True
    if isinstance(observation.get("_launch_cleanup_quarantine"), Mapping):
        return True
    process = observation.get("_process")
    if application_row["owner_pid"] is not None or application_row["browser_pid"] is not None:
        if not isinstance(process, dict):
            return True
    for column, kind in (("owner_pid", "owner"), ("browser_pid", "browser")):
        pid = application_row[column]
        if pid is None:
            continue
        identity = process.get(kind) if isinstance(process, dict) else None
        if not isinstance(identity, dict):
            return True
        if _process_group_state(int(pid), expected=identity) != "absent":
            return True
    return False
def _reconcile_abandoned_rpc_runs_locked(
    connection: sqlite3.Connection,
    *,
    expected_coordinator_id: str | None = None,
) -> RpcReconciliationResult:
    active_query = """
        SELECT r.run_id
        FROM application_rpc_runs r
        WHERE r.state IN ('starting', 'running', 'manual', 'blocked')
          AND r.handoff_committed=0
    """
    active_params: tuple[Any, ...] = ()
    if expected_coordinator_id is not None:
        active_query += " AND r.coordinator_id=?"
        active_params = (expected_coordinator_id,)
    active_query += " ORDER BY r.run_id"
    active_rows = connection.execute(active_query, active_params).fetchall()
    pending_null = connection.execute(
        """
        SELECT *
        FROM application_rpc_requests
        WHERE state='pending' AND run_id IS NULL
        ORDER BY created_at, request_id
        """
    ).fetchall()
    active_plans: list[tuple[sqlite3.Row, sqlite3.Row, list[sqlite3.Row], sqlite3.Row | None]] = []
    conflict_run_ids: list[int] = []
    for identity_row in active_rows:
        run_id = int(identity_row["run_id"])
        rpc_row = _rpc_run_row(connection, run_id)
        application_row = connection.execute(
            "SELECT * FROM application_runs WHERE id=?", (run_id,)
        ).fetchone()
        if rpc_row is None or application_row is None:
            conflict_run_ids.append(run_id)
            continue
        precommit_recovery_owner = _precommit_recovery_owner_is_authorized(
            rpc_row, application_row
        )
        owner_conflict = _rpc_ownership_conflict(
            rpc_row,
            application_row,
            allow_precommit_recovery=precommit_recovery_owner,
        )
        if owner_conflict:
            conflict_run_ids.append(run_id)
            continue
        outcome = application_row["outcome"]
        reviewed_at = application_row["reviewed_at"]
        if (
            (outcome is None) != (reviewed_at is None)
            or outcome is not None
            or application_row["status"] not in {"running", "manual", "blocked"}
        ):
            conflict_run_ids.append(run_id)
            continue
        requests = connection.execute(
            """
            SELECT * FROM application_rpc_requests
            WHERE run_id=? AND state='pending'
            ORDER BY created_at, request_id
            """,
            (run_id,),
        ).fetchall()
        all_requests = connection.execute(
            "SELECT * FROM application_rpc_requests WHERE run_id=? ORDER BY created_at, request_id",
            (run_id,),
        ).fetchall()
        event_request = requests[0] if requests else (all_requests[0] if all_requests else None)
        if event_request is None:
            conflict_run_ids.append(run_id)
            continue
        try:
            for request_row in (*requests, event_request):
                _stored_rpc_request(request_row)
        except RuntimeError:
            conflict_run_ids.append(run_id)
            continue
        active_plans.append((rpc_row, application_row, list(requests), event_request))
    try:
        for request_row in pending_null:
            _stored_rpc_request(request_row)
    except RuntimeError:
        return RpcReconciliationResult(
            status="conflict",
            conflict_run_ids=tuple(conflict_run_ids),
        )
    for request_row in pending_null:
        request = _stored_rpc_request(request_row)
        _, response_json = _rpc_reconciliation_response(
            request,
            run_id=request.run_id,
            action_sequence=0,
            event_sequence=0,
        )
        now = utc_now()
        changed = connection.execute(
            """
            UPDATE application_rpc_requests
            SET state='completed', response_json=?, completed_at=?
            WHERE request_id=? AND state='pending' AND run_id IS NULL
            """,
            (response_json, now, request.request_id),
        ).rowcount
        if changed != 1:
            raise RuntimeError("pending null RPC request reconciliation CAS failed")

    affected: list[int] = []
    events: list[tuple[int, int]] = []
    for rpc_row, application_row, pending, event_request in active_plans:
        run_id = int(rpc_row["run_id"])
        action_sequence = int(rpc_row["action_sequence"])
        source_version = int(rpc_row["version"])
        now = utc_now()
        changed = connection.execute(
            """
            UPDATE application_rpc_runs
            SET state='failed', version=version+1, updated_at=?
            WHERE run_id=?
              AND state IN ('starting', 'running', 'manual', 'blocked')
              AND handoff_committed=0
              AND version=?
            """,
            (now, run_id, source_version),
        ).rowcount
        if changed != 1:
            raise RuntimeError("RPC run reconciliation CAS failed")
        if application_row["status"] == "running":
            changed = connection.execute(
                """
                UPDATE application_runs
                SET status='failed', reason_code='abandoned_running_attempt',
                    finished_at=?, outcome='retry', reviewed_at=?
                WHERE id=? AND status='running' AND outcome IS NULL AND reviewed_at IS NULL
                """,
                (now, now, run_id),
            ).rowcount
        else:
            changed = connection.execute(
                """
                UPDATE application_runs
                SET outcome='retry', reviewed_at=?
                WHERE id=? AND status IN ('manual', 'blocked')
                  AND outcome IS NULL AND reviewed_at IS NULL
                """,
                (now, run_id),
            ).rowcount
        if changed != 1:
            raise RuntimeError("application run reconciliation CAS failed")
        event_sequence = _append_rpc_event_locked(
            connection,
            run_id=run_id,
            request_id=str(event_request["request_id"]),
            event_type="run_failed",
            summary_code="failed",
            action_sequence=action_sequence,
            observation_sha256=None,
            allow_terminal=True,
            check_owner=False,
        )
        connection.execute(
            "UPDATE jobs SET status='queued' WHERE id=(SELECT job_id FROM application_runs WHERE id=?) AND status='in_progress'",
            (run_id,),
        )
        for request_row in pending:
            request = _stored_rpc_request(request_row)
            _, response_json = _rpc_reconciliation_response(
                request,
                run_id=run_id,
                action_sequence=action_sequence,
                event_sequence=event_sequence,
            )
            changed = connection.execute(
                """
                UPDATE application_rpc_requests
                SET state='completed', response_json=?, completed_at=?
                WHERE request_id=? AND state='pending' AND run_id=?
                """,
                (response_json, now, request.request_id, run_id),
            ).rowcount
            if changed != 1:
                raise RuntimeError("pending RPC request reconciliation CAS failed")
        affected.append(run_id)
        events.append((run_id, event_sequence))
    if not affected and not pending_null:
        if conflict_run_ids:
            return RpcReconciliationResult(
                status="conflict",
                conflict_run_ids=tuple(conflict_run_ids),
            )
        return RpcReconciliationResult(status="noop")
    return RpcReconciliationResult(
        status="partial" if conflict_run_ids else "reconciled",
        run_ids=tuple(affected),
        event_sequences=tuple(events),
        conflict_run_ids=tuple(conflict_run_ids),
    )


def reconcile_abandoned_rpc_runs(
    connection: sqlite3.Connection,
    *,
    expected_coordinator_id: str | None = None,
) -> RpcReconciliationResult:
    """Terminalize only RPC runs whose registered owners are proven absent."""
    if expected_coordinator_id is not None:
        expected_coordinator_id = _require_rpc_coordinator_id(expected_coordinator_id)
    connection.execute("BEGIN IMMEDIATE")
    try:
        result = _reconcile_abandoned_rpc_runs_locked(
            connection,
            expected_coordinator_id=expected_coordinator_id,
        )
    except Exception:
        connection.rollback()
        raise
    connection.commit()
    return result
def _artifact_bytes(value: Any) -> bytes:
    if isinstance(value, bytes):
        return value
    return str(value if value is not None else "").encode("utf-8", "surrogatepass")


def _write_artifact(run: Any, relative_path: str, data: bytes) -> str:
    """Write through ArtifactRun, refusing a conflicting orphan payload."""
    digest = hashlib.sha256(data).hexdigest()
    parts = _artifacts._validate_relative_artifact_path(relative_path)
    parent_fd = run._require_fd()
    opened: list[int] = []
    try:
        for directory in parts[:-1]:
            next_fd = _artifacts._open_private_child_dir(parent_fd, directory)
            opened.append(next_fd)
            parent_fd = next_fd
        existing = _artifacts._existing_file_hash(parent_fd, parts[-1])
        if existing is not None and existing != digest:
            raise RuntimeError("conflicting orphan artifact")
    except FileNotFoundError:
        pass
    finally:
        for fd in reversed(opened):
            os.close(fd)
    result = run.write_bytes(relative_path, data)
    if result.sha256 != digest:
        raise RuntimeError("artifact hash mismatch")
    return result.sha256


def _host_class(value: str) -> str:
    try:
        parsed = urlsplit(value)
        host = parsed.hostname
    except ValueError:
        return "invalid"
    if not host:
        return "invalid"
    host = host.lower().rstrip(".")
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        address = None
    if address is not None:
        if address.is_private or address.is_loopback or address.is_link_local or address.is_reserved:
            return "private_or_local"
        return "unsupported_public"
    if host == "boards.greenhouse.io" or host == "job-boards.greenhouse.io" or host.endswith(".greenhouse.io"):
        return "approved_greenhouse"
    if host in {"jobs.lever.co", "jobs.eu.lever.co"}:
        return "approved_lever"
    if host in {"localhost", "localhost.localdomain"} or host.endswith(".local"):
        return "private_or_local"
    return "unsupported_public"


def _redacted_apply_url(url: str) -> str:
    if type(url) is not str:
        raise TypeError("apply_url must be a string")
    digest = hashlib.sha256(url.encode("utf-8", "surrogatepass")).hexdigest()
    host_class = _host_class(url)
    prefix = "gh_hash" if host_class == "approved_greenhouse" else "lever_hash" if host_class == "approved_lever" else "url_hash"
    return f"{prefix}:{digest} class={host_class}"


def _iter_values(value: Any):
    """Yield a value and all nested JSON-like descendants."""
    yield value
    if isinstance(value, dict):
        for child in value.values():
            yield from _iter_values(child)
    elif isinstance(value, (list, tuple)):
        for child in value:
            yield from _iter_values(child)


def _redacted_summary(value: Any) -> str:
    """Persist only aggregate counts, full hashes, and closed host classes."""
    encoded = encode_json(value)
    host_classes = sorted(
        {
            _host_class(item)
            for item in _iter_values(value)
            if type(item) is str and "://" in item
        }
    )
    count = sum(1 for _ in _iter_values(value))
    return encode_json(
        {
            "sha256": hashlib.sha256(encoded.encode("utf-8", "surrogatepass")).hexdigest(),
            "count": count,
            "host_classes": host_classes,
        }
    )


def _legacy_summary(payload: bytes) -> dict[str, Any]:
    summary: dict[str, Any] = {
        "sha256": hashlib.sha256(payload).hexdigest(),
        "bytes": len(payload),
    }
    try:
        decoded = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError, TypeError):
        summary["decoded"] = False
        return summary
    summary["decoded"] = True
    summary["type"] = "object" if isinstance(decoded, dict) else "array" if isinstance(decoded, list) else type(decoded).__name__
    summary["count"] = sum(1 for _ in _iter_values(decoded))
    return summary


def _migrate_legacy_application_runs(connection: sqlite3.Connection, root: ArtifactRoot) -> None:
    rows = [dict(row) for row in connection.execute(
        """
        SELECT id, job_id, status, started_at, finished_at,
               CAST(apply_url AS BLOB) AS apply_url_bytes,
               CAST(reason AS BLOB) AS reason_bytes,
               CAST(observation_json AS BLOB) AS observation_bytes,
               CAST(plan_json AS BLOB) AS plan_bytes
        FROM application_runs ORDER BY id
        """
    ).fetchall()]
    old_sequence_row = connection.execute("SELECT MAX(seq) AS seq FROM sqlite_sequence WHERE name='application_runs'").fetchone()
    old_sequence = int(old_sequence_row["seq"]) if old_sequence_row and old_sequence_row["seq"] is not None else 0
    refs: dict[int, str] = {}
    summaries: dict[int, dict[str, Any]] = {}
    for row in rows:
        run_id = int(row["id"])
        artifact_run = _legacy_artifact_run(root, run_id)
        reason_bytes = bytes(row["reason_bytes"] or b"")
        observation_bytes = bytes(row["observation_bytes"] or b"")
        plan_bytes = bytes(row["plan_bytes"] or b"")
        apply_url_bytes = bytes(row["apply_url_bytes"] or b"")
        try:
            refs[run_id] = _artifact_ref_for_run(root, run_id)
            hashes = {
                "legacy/reason.txt": _write_artifact(artifact_run, "legacy/reason.txt", reason_bytes),
                "legacy/observation.json": _write_artifact(artifact_run, "legacy/observation.json", observation_bytes),
                "legacy/plan.json": _write_artifact(artifact_run, "legacy/plan.json", plan_bytes),
            }
            manifest = {
                "run_id": run_id,
                "artifact_ref": refs[run_id],
                "legacy_hashes": hashes,
                "apply_url_sha256": hashlib.sha256(apply_url_bytes).hexdigest(),
            }
            _write_artifact(artifact_run, "run.json", encode_json(manifest).encode("utf-8"))
            summaries[run_id] = {
                "observation": _legacy_summary(observation_bytes),
                "plan": _legacy_summary(plan_bytes),
            }
        finally:
            artifact_run.close()
    if int(connection.execute("PRAGMA secure_delete").fetchone()[0]) != 1:
        raise RuntimeError("secure_delete is required for legacy migration")
    connection.execute("ALTER TABLE application_runs RENAME TO application_runs_legacy")
    for name in ("idx_application_runs_job_id", "idx_application_runs_status"):
        connection.execute(f"DROP INDEX IF EXISTS {name}")
    connection.execute(APPLICATION_SCHEMA_SQL)
    for statement in APPLICATION_INDEX_SQL:
        connection.execute(statement)
    for row in rows:
        run_id = int(row["id"])
        status = "review_ready" if row["status"] == "completed" else row["status"]
        observation_bytes = bytes(row["observation_bytes"] or b"")
        plan_bytes = bytes(row["plan_bytes"] or b"")
        apply_url = bytes(row["apply_url_bytes"] or b"").decode("utf-8", "replace")
        connection.execute(
            """
            INSERT INTO application_runs (
                id, job_id, apply_url, status, reason_code, owner, started_at, finished_at,
                observation_json, plan_json, artifact_dir
            ) VALUES (?, ?, ?, ?, 'legacy_run', 'legacy:migrated', ?, ?, ?, ?, ?)
            """,
            (
                run_id, int(row["job_id"]), _redacted_apply_url(apply_url), status,
                row["started_at"], row["finished_at"],
                encode_json(summaries[run_id]["observation"]),
                encode_json(summaries[run_id]["plan"]), refs[run_id],
            ),
        )
    max_id = max((int(row["id"]) for row in rows), default=0)
    high_water = max(old_sequence, max_id)
    connection.execute(
        """
        UPDATE jobs
        SET status='in_progress'
        WHERE status <> 'archived'
          AND id IN (
            SELECT job_id FROM application_runs r
            WHERE r.id = (SELECT max(r2.id) FROM application_runs r2 WHERE r2.job_id = r.job_id)
              AND r.status <> 'running'
          )
        """
    )
    connection.execute("DROP TABLE application_runs_legacy")
    if high_water:
        connection.execute("DELETE FROM sqlite_sequence WHERE name='application_runs'")
        connection.execute("INSERT INTO sqlite_sequence(name, seq) VALUES ('application_runs', ?)", (high_water,))


def _first_string(raw: dict[str, Any], *keys: str) -> str | None:
    for key in keys:
        value = raw.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
        if value is not None and not isinstance(value, (dict, list)):
            return str(value)
    return None


def _company_value(raw: dict[str, Any]) -> str:
    company = raw.get("company")
    if isinstance(company, dict):
        name = company.get("name")
        if name:
            return str(name)
    if isinstance(company, str) and company.strip():
        return company.strip()
    return _first_string(raw, "company_name") or "Unknown company"


def find_existing_job(connection: sqlite3.Connection, source_job_id: str | None, canonical_url: str | None, source: str) -> sqlite3.Row | None:
    if source_job_id:
        row = connection.execute("SELECT * FROM jobs WHERE source = ? AND source_job_id = ?", (source, source_job_id)).fetchone()
        if row is not None:
            return row
    if canonical_url:
        return connection.execute("SELECT * FROM jobs WHERE canonical_url = ?", (canonical_url,)).fetchone()
    return None


def upsert_raw_job(connection: sqlite3.Connection, raw: dict[str, Any], *, source: str = "theirstack") -> StoredJobInfo:
    source_job_id = _first_string(raw, "source_job_id", "id", "job_id", "theirstack_job_id", "external_id")
    url = _first_string(raw, "apply_url", "url", "job_url", "listing_url", "canonical_url")
    canonical_url = canonicalize_url(url)
    if not source_job_id and not canonical_url:
        raise ValueError("job needs source_job_id or url")
    title = _first_string(raw, "title", "job_title", "normalized_title") or "Untitled role"
    company = _company_value(raw)
    location = _first_string(raw, "location", "job_location", "city", "country_code")
    remote = raw.get("remote")
    remote_int = int(remote) if isinstance(remote, bool) else None
    posted_at = _first_string(raw, "posted_at", "date_posted")
    discovered_at = _first_string(raw, "discovered_at") or utc_now()
    description = _first_string(raw, "description", "job_description", "description_text")
    existing = find_existing_job(connection, source_job_id, canonical_url, source)
    now = utc_now()
    if existing is not None:
        connection.execute(
            """
            UPDATE jobs
            SET source_job_id = COALESCE(?, source_job_id), canonical_url = COALESCE(?, canonical_url),
                title = ?, company = ?, location = ?, remote = ?, posted_at = ?, description = ?,
                raw_json = ?, last_seen_at = ?
            WHERE id = ?
            """,
            (source_job_id, canonical_url, title, company, location, remote_int, posted_at, description, encode_json(raw), now, existing["id"]),
        )
        connection.commit()
        return StoredJobInfo("updated", str(existing["discovered_at"]))
    connection.execute(
        """
        INSERT INTO jobs (source, source_job_id, canonical_url, title, company, location, remote, posted_at,
                          discovered_at, description, raw_json, first_seen_at, last_seen_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (source, source_job_id, canonical_url, title, company, location, remote_int, posted_at, discovered_at, description, encode_json(raw), now, now),
    )
    connection.commit()
    return StoredJobInfo("inserted", discovered_at)


def record_sync_run(connection: sqlite3.Connection, source: str, mode: str, *, started_at: str | None = None, profile: str | None = None) -> int:
    cur = connection.execute("INSERT INTO sync_runs (source, profile, mode, started_at) VALUES (?, ?, ?, ?)", (source, profile, mode, started_at or utc_now()))
    if cur.lastrowid is None:
        raise RuntimeError("sync run id unavailable")
    connection.commit()
    return int(cur.lastrowid)


def update_sync_run(connection: sqlite3.Connection, run_id: int, **kwargs: Any) -> None:
    allowed = {"finished_at", "checkpoint", "success", "jobs_seen", "jobs_returned", "jobs_inserted", "jobs_updated", "error"}
    fields = {key: value for key, value in kwargs.items() if key in allowed}
    if not fields:
        return
    values = list(fields.values()) + [run_id]
    connection.execute(f"UPDATE sync_runs SET {', '.join(f'{key}=?' for key in fields)} WHERE id=?", values)
    connection.commit()


def latest_sync_checkpoint(connection: sqlite3.Connection, *, source: str | None = None, profile: str | None = None) -> str | None:
    clauses = ["success = 1"]
    values: list[Any] = []
    if source is not None:
        clauses.append("source = ?")
        values.append(source)
    if profile is not None:
        clauses.append("profile = ?")
        values.append(profile)
    where = " AND ".join(clauses)
    row = connection.execute(f"SELECT checkpoint FROM sync_runs WHERE {where} AND checkpoint IS NOT NULL ORDER BY finished_at DESC, started_at DESC, id DESC LIMIT 1", values).fetchone()
    if row and row["checkpoint"]:
        return str(row["checkpoint"])
    row = connection.execute(f"SELECT started_at AS checkpoint FROM sync_runs WHERE {where} ORDER BY started_at DESC, id DESC LIMIT 1", values).fetchone()
    return str(row["checkpoint"]) if row and row["checkpoint"] else None


def _claim_application_job_locked(
    connection: sqlite3.Connection,
    *,
    owner: str,
    row: sqlite3.Row,
) -> ApplicationClaim | None:
    changed = connection.execute(
        "UPDATE jobs SET status='in_progress' WHERE id=? AND status='queued'",
        (row["id"],),
    ).rowcount
    if changed != 1:
        return None
    now = utc_now()
    cur = connection.execute(
        """
        INSERT INTO application_runs (job_id, apply_url, status, reason_code, owner, started_at)
        VALUES (?, ?, 'running', NULL, ?, ?)
        """,
        (row["id"], _redacted_apply_url(str(row["canonical_url"])), owner, now),
    )
    if cur.lastrowid is None:
        raise RuntimeError("application run id unavailable")
    run_id = int(cur.lastrowid)
    selected = connection.execute("SELECT * FROM jobs WHERE id=?", (row["id"],)).fetchone()
    if selected is None:
        raise RuntimeError("claimed job disappeared")
    return ApplicationClaim(run_id=run_id, job=dict(selected))


def claim_next_application_job(connection: sqlite3.Connection, *, owner: str) -> ApplicationClaim | None:
    if type(owner) is not str or not owner.strip():
        raise TypeError("owner must be a non-empty string")
    connection.execute("BEGIN IMMEDIATE")
    try:
        row = None
        for candidate in connection.execute(
            """
            SELECT * FROM jobs
            WHERE status='queued' AND canonical_url IS NOT NULL
            ORDER BY posted_at DESC NULLS LAST, first_seen_at ASC, id ASC
            """
        ):
            if _prior_attempt_process_conflict(
                connection,
                job_id=int(candidate["id"]),
            ):
                continue
            row = candidate
            break
        if row is None:
            connection.commit()
            return None
        claim = _claim_application_job_locked(connection, owner=owner, row=row)
        if claim is None:
            connection.rollback()
            return None
    except Exception:
        connection.rollback()
        raise
    connection.commit()
    return claim


def claim_application_job(connection: sqlite3.Connection, *, owner: str, job_id: int | str) -> ApplicationClaim | None:
    if type(owner) is not str or not owner.strip():
        raise TypeError("owner must be a non-empty string")
    try:
        job_id_int = int(job_id)
    except (ValueError, TypeError):
        raise TypeError("job_id must be an integer or integer string") from None

    connection.execute("BEGIN IMMEDIATE")
    try:
        candidate = connection.execute(
            """
            SELECT * FROM jobs
            WHERE id=? AND status='queued' AND canonical_url IS NOT NULL
            """,
            (job_id_int,),
        ).fetchone()
        if candidate is None or _prior_attempt_process_conflict(connection, job_id=job_id_int):
            connection.commit()
            return None

        claim = _claim_application_job_locked(connection, owner=owner, row=candidate)
        if claim is None:
            connection.rollback()
            return None
    except Exception:
        connection.rollback()
        raise
    connection.commit()
    return claim


def claim_application_job_with_generated_resume(
    connection: sqlite3.Connection,
    *,
    owner: str,
    job_id: int | str,
    resume_id: str,
    expected_job_snapshot_sha256: str,
    description_override: str | None = None,
) -> ApplicationClaim | None:
    """Claim one queued job and pin its ready generated resume atomically."""
    if type(owner) is not str or not owner.strip():
        raise TypeError("owner must be a non-empty string")
    if type(job_id) is bool or not isinstance(job_id, (int, str)):
        raise TypeError("job_id must be an integer or integer string")
    try:
        job_id_int = int(job_id)
    except (ValueError, TypeError):
        raise TypeError("job_id must be an integer or integer string") from None
    if job_id_int <= 0:
        raise ValueError("job_id must be a positive integer")
    if type(resume_id) is not str or not resume_id.strip():
        raise TypeError("resume_id must be a non-empty string")
    expected_job_snapshot_sha256 = _require_rpc_sha256(
        expected_job_snapshot_sha256,
        "expected_job_snapshot_sha256",
    )
    description_override = _require_description_override(description_override)

    connection.execute("BEGIN IMMEDIATE")
    try:
        resume = connection.execute(
            "SELECT * FROM generated_resumes WHERE resume_id = ?",
            (resume_id,),
        ).fetchone()
        if resume is None or resume["state"] != "ready":
            connection.rollback()
            return None
        if int(resume["job_id"]) != job_id_int:
            connection.rollback()
            return None

        candidate = connection.execute(
            """
            SELECT * FROM jobs
            WHERE id=? AND status='queued' AND canonical_url IS NOT NULL
            """,
            (job_id_int,),
        ).fetchone()
        if candidate is None or _prior_attempt_process_conflict(connection, job_id=job_id_int):
            connection.rollback()
            return None

        current_snapshot = _build_resume_snapshot_from_job_row(
            candidate,
            description_override=description_override,
        )
        current_snapshot_sha256 = str(current_snapshot.job_snapshot_sha256)
        expected_matches = hmac.compare_digest(
            current_snapshot_sha256,
            expected_job_snapshot_sha256,
        )
        resume_matches = hmac.compare_digest(
            current_snapshot_sha256,
            str(resume["job_snapshot_sha256"]),
        )
        if not expected_matches or not resume_matches:
            connection.rollback()
            return None

        claim = _claim_application_job_locked(connection, owner=owner, row=candidate)
        if claim is None:
            connection.rollback()
            return None
        bound_at = utc_now()
        _insert_application_resume_binding_locked(
            connection,
            resume_id=resume_id,
            run_id=claim.run_id,
            bound_at=bound_at,
            replace_existing=False,
        )
    except Exception:
        connection.rollback()
        raise
    connection.commit()
    return claim


def _require_exact_text(value: Any, name: str) -> str:
    if type(value) is not str or not value:
        raise TypeError(f"{name} must be a non-empty string")
    return value
def _require_run_artifact_ref(value: Any, run_id: int, name: str = "artifact_dir") -> str:
    if type(run_id) is not int or run_id <= 0:
        raise TypeError("run_id must be a positive integer")
    value = _require_exact_text(value, name)
    if value != f"run-{run_id}":
        raise ValueError(f"{name} must match its application run")
    return value


def _require_existing_artifact_ref(
    value: Any,
    run_id: int,
    name: str = "artifact_dir",
) -> str:
    if type(run_id) is not int or run_id <= 0:
        raise TypeError("run_id must be a positive integer")
    value = _require_exact_text(value, name)
    if value not in {f"run-{run_id}", f"legacy-run-{run_id}"}:
        raise ValueError(f"{name} must match its application run")
    return value



def _require_public_code(value: Any, name: str, choices: tuple[str, ...]) -> str:
    value = _require_exact_text(value, name)
    if value not in choices:
        raise ValueError(f"{name} must be an exact public code")
    return value
def _require_reason_status(status: str, reason_code: str, *, allow_legacy: bool = False) -> None:
    if allow_legacy and reason_code == "legacy_run":
        return
    if REASON_STATUS.get(reason_code) != status:
        raise ValueError("reason_code does not match status")


def _cas_run_update(connection: sqlite3.Connection, run_id: int, sql: str, values: tuple[Any, ...]) -> bool:
    if type(run_id) is not int or run_id <= 0:
        raise TypeError("run_id must be a positive integer")
    connection.execute("BEGIN IMMEDIATE")
    try:
        changed = connection.execute(sql, (*values, run_id)).rowcount
    except Exception:
        connection.rollback()
        raise
    if changed == 1:
        connection.commit()
        return True
    connection.rollback()
    return False


def register_application_artifact(connection: sqlite3.Connection, *, run_id: int, artifact_dir: str) -> bool:
    artifact_dir = _require_run_artifact_ref(artifact_dir, run_id)
    return _cas_run_update(connection, run_id, "UPDATE application_runs SET artifact_dir=? WHERE status='running' AND (artifact_dir IS NULL OR artifact_dir=?) AND id=?", (artifact_dir, artifact_dir))


def register_application_session(connection: sqlite3.Connection, *, run_id: int, session_id: str, session_state: str | None = None) -> bool:
    session_id = _require_exact_text(session_id, "session_id")
    valid_states = ("starting", "prepared", "open", "open_guarded")
    if session_state is not None:
        session_state = _require_exact_text(session_state, "session_state")
        if session_state not in valid_states:
            raise ValueError("session_state must be starting, prepared, open, or open_guarded")
    connection.execute("BEGIN IMMEDIATE")
    try:
        row = connection.execute("SELECT status, session_id, observation_json, artifact_dir, reviewed_at FROM application_runs WHERE id=?", (run_id,)).fetchone()
        if row is None or row["status"] != "running" or row["artifact_dir"] is None:
            connection.rollback()
            return False
        if row["session_id"] is not None and row["session_id"] != session_id:
            connection.rollback()
            return False
        if connection.execute(
            "SELECT 1 FROM application_runs WHERE session_id=? AND id<>? LIMIT 1",
            (session_id, run_id),
        ).fetchone() is not None:
            connection.rollback()
            return False
        observation = _decode_run_json(row["observation_json"])
        current_state = observation.get("_session_state")
        if session_state is not None and current_state is not None:
            allowed_next = {
                "starting": {"starting", "prepared"},
                "prepared": {"prepared", "open", "open_guarded"},
                "open": {"open", "open_guarded"},
                "open_guarded": {"open_guarded"},
            }
            if session_state not in allowed_next.get(str(current_state), set()):
                connection.rollback()
                return False
        observation.setdefault("_spawn_attempted", False)
        if session_state is not None:
            observation["_session_state"] = session_state
            if session_state in {"open", "open_guarded"}:
                # Monotonic provenance survives every later summary replacement.
                observation["_ever_open_guarded"] = True
        changed = connection.execute(
            "UPDATE application_runs SET session_id=?, observation_json=? WHERE status='running' AND (session_id IS NULL OR session_id=?) AND id=?",
            (session_id, encode_json(observation), session_id, run_id),
        ).rowcount
        if changed != 1:
            connection.rollback()
            return False
    except Exception:
        connection.rollback()
        raise
    connection.commit()
    return True


def mark_application_spawn_attempted(
    connection: sqlite3.Connection,
    *,
    run_id: int,
    session_id: str,
) -> bool:
    """Atomically close pre-spawn recovery before launching the browser owner."""
    session_id = _require_exact_text(session_id, "session_id")
    connection.execute("BEGIN IMMEDIATE")
    try:
        row = connection.execute(
            "SELECT status, session_id, observation_json FROM application_runs WHERE id=?",
            (run_id,),
        ).fetchone()
        if row is None or row["status"] != "running" or row["session_id"] != session_id:
            connection.rollback()
            return False
        observation = _decode_run_json(row["observation_json"])
        if observation.get("_spawn_attempted") is not False:
            connection.rollback()
            return False
        observation["_spawn_attempted"] = True
        changed = connection.execute(
            """
            UPDATE application_runs
            SET observation_json=?
            WHERE id=? AND status='running' AND session_id=?
            """,
            (encode_json(observation), run_id, session_id),
        ).rowcount
        if changed != 1:
            connection.rollback()
            return False
    except Exception:
        connection.rollback()
        raise
    connection.commit()
    return True


def mark_rpc_omp_spawn_attempted(
    connection: sqlite3.Connection,
    *,
    run_id: int,
    coordinator_id: str,
) -> bool:
    """Durably mark an OMP spawn before subprocess creation."""
    run_id = _require_rpc_positive_int(run_id, "run_id")
    coordinator_id = _require_rpc_coordinator_id(coordinator_id)
    connection.execute("BEGIN IMMEDIATE")
    try:
        rpc_row = _rpc_run_row(connection, run_id)
        application_row = connection.execute(
            "SELECT * FROM application_runs WHERE id=?", (run_id,)
        ).fetchone()
        if (
            rpc_row is None
            or application_row is None
            or not _rpc_owner_matches(rpc_row, coordinator_id)
            or str(rpc_row["state"]) != "starting"
            or bool(rpc_row["handoff_committed"])
            or str(application_row["status"]) != "running"
            or application_row["outcome"] is not None
            or application_row["reviewed_at"] is not None
        ):
            connection.rollback()
            return False
        observation = _decode_run_json(application_row["observation_json"])
        if observation.get("_omp_spawn_attempted") is True:
            connection.rollback()
            return False
        observation["_omp_spawn_attempted"] = True
        changed = connection.execute(
            """
            UPDATE application_runs
            SET observation_json=?
            WHERE id=? AND status='running' AND reviewed_at IS NULL AND outcome IS NULL
            """,
            (encode_json(observation), run_id),
        ).rowcount
        if changed != 1:
            connection.rollback()
            return False
    except Exception:
        connection.rollback()
        raise
    connection.commit()
    return True
def _identity_payload(identity: dict[str, Any] | None, pid: int, *, require_leader: bool = True) -> dict[str, Any]:
    """Validate supplied identity against a fresh live OS capture."""
    observed = _capture_process_identity(pid)
    if not isinstance(observed, dict) or observed.get("probe_error"):
        raise RuntimeError("process identity unavailable")
    if type(observed.get("pid")) is not int or observed["pid"] != pid:
        raise RuntimeError("process identity pid mismatch")
    if type(observed.get("pgid")) is not int or observed["pgid"] <= 0:
        raise RuntimeError("process group identity is required")
    if require_leader and observed["pgid"] != pid:
        raise RuntimeError("process is not a process-group leader")
    birth = observed.get("birth")
    if (
        type(birth) is not str
        or not birth
        or len(birth) > 256
        or any(ord(char) < 0x20 or ord(char) > 0x7E for char in birth)
    ):
        raise RuntimeError("process birth identity is required")
    captured = {"pid": pid, "pgid": int(observed["pgid"]), "birth": birth}
    if identity is not None:
        if not isinstance(identity, dict):
            raise TypeError("process_identity must be an object")
        if {key: identity.get(key) for key in captured} != captured:
            raise ValueError("supplied process identity does not match live process")
    return captured


def _decode_run_json(raw: Any) -> dict[str, Any]:
    try:
        value = json.loads(str(raw or "{}"))
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _run_owner_is_active_or_unreviewed(row: sqlite3.Row) -> bool:
    """Return whether a run may still own a registered process identity."""
    return str(row["status"]) == "running" or row["reviewed_at"] is None


def _active_process_identity_conflict(
    connection: sqlite3.Connection,
    *,
    run_id: int,
    payload: dict[str, Any],
) -> bool:
    """Reject exact identities owned by active or still-live historical runs."""
    rows = connection.execute(
        """
        SELECT id, status, outcome, reviewed_at, owner_pid, browser_pid, observation_json
        FROM application_runs
        WHERE id<>? AND (owner_pid=? OR browser_pid=?)
        """,
        (run_id, payload["pid"], payload["pid"]),
    ).fetchall()
    for row in rows:
        observation = _decode_run_json(row["observation_json"])
        process = observation.get("_process")
        if not isinstance(process, dict):
            continue
        for column, kind in (("owner_pid", "owner"), ("browser_pid", "browser")):
            if row[column] != payload["pid"] or process.get(kind) != payload:
                continue
            if _run_owner_is_active_or_unreviewed(row):
                return True
            try:
                previous_state = _process_group_state(payload["pid"], expected=payload)
            except Exception:
                return True
            if previous_state != "absent":
                return True
    return False
def _register_process(
    connection: sqlite3.Connection,
    *,
    run_id: int,
    column: str,
    kind: str,
    pid: int,
    identity: dict[str, Any] | None,
    artifact_root: ArtifactRoot | None,
) -> bool:
    if column not in {"owner_pid", "browser_pid"}:
        raise ValueError("invalid process column")
    connection.execute("BEGIN IMMEDIATE")
    try:
        row = connection.execute(
            "SELECT status, owner_pid, browser_pid, session_id, artifact_dir, observation_json FROM application_runs WHERE id=?",
            (run_id,),
        ).fetchone()
        if row is None or row["status"] != "running" or row["session_id"] is None or row["artifact_dir"] is None:
            connection.rollback()
            return False
        if column == "browser_pid" and row["owner_pid"] is None:
            connection.rollback()
            return False
        if (
            (column == "browser_pid" and row["owner_pid"] == pid)
            or (column == "owner_pid" and row["browser_pid"] == pid)
        ):
            connection.rollback()
            return False
        observation = _decode_run_json(row["observation_json"])
        if observation.get("_spawn_attempted") is not True:
            connection.rollback()
            return False
        existing = observation.get("_process", {})
        if column == "browser_pid" and (not isinstance(existing, dict) or not isinstance(existing.get("owner"), dict)):
            connection.rollback()
            return False
        payload = _identity_payload(identity, pid, require_leader=True)
        if _active_process_identity_conflict(connection, run_id=run_id, payload=payload):
            connection.rollback()
            return False
        existing_identity = existing.get(kind) if isinstance(existing, dict) else None
        if int(payload["pid"]) != pid:
            connection.rollback()
            return False
        if row[column] is not None and int(row[column]) != pid:
            connection.rollback()
            return False
        if isinstance(existing_identity, dict) and existing_identity != payload:
            connection.rollback()
            return False
        observation = dict(observation)
        process = observation.setdefault("_process", {})
        if not isinstance(process, dict):
            process = {}
            observation["_process"] = process
        process[kind] = payload
        changed = connection.execute(
            f"UPDATE application_runs SET {column}=?, observation_json=? WHERE status='running' AND ({column} IS NULL OR {column}=?) AND id=?",
            (pid, encode_json(observation), pid, run_id),
        ).rowcount
        if changed != 1:
            connection.rollback()
            return False
    except Exception:
        connection.rollback()
        raise
    connection.commit()
    return True


def register_application_owner_process(
    connection: sqlite3.Connection,
    *,
    run_id: int,
    owner_pid: int,
    process_identity: dict[str, Any] | None = None,
    artifact_root: ArtifactRoot | None = None,
) -> bool:
    if type(owner_pid) is not int or owner_pid <= 0:
        raise TypeError("owner_pid must be a positive integer")
    return _register_process(connection, run_id=run_id, column="owner_pid", kind="owner", pid=owner_pid, identity=process_identity, artifact_root=artifact_root)


def register_application_browser_process(
    connection: sqlite3.Connection,
    *,
    run_id: int,
    browser_pid: int,
    process_identity: dict[str, Any] | None = None,
    artifact_root: ArtifactRoot | None = None,
) -> bool:
    if type(browser_pid) is not int or browser_pid <= 0:
        raise TypeError("browser_pid must be a positive integer")
    return _register_process(connection, run_id=run_id, column="browser_pid", kind="browser", pid=browser_pid, identity=process_identity, artifact_root=artifact_root)



def _process_birth_token(pid: int) -> str | None:
    if os.path.exists(f"/proc/{pid}/stat"):
        try:
            raw = Path(f"/proc/{pid}/stat").read_text()
            fields = raw.rsplit(") ", 1)[1].split()
            return fields[19]
        except (OSError, IndexError):
            return None
    try:
        result = subprocess.run(("ps", "-p", str(pid), "-o", "lstart="), check=False, capture_output=True, text=True, timeout=1)
    except (OSError, subprocess.SubprocessError):
        return None
    token = result.stdout.strip()
    return token or None


def _capture_process_identity(pid: int) -> dict[str, Any] | None:
    try:
        pgid = os.getpgid(pid)
    except ProcessLookupError:
        return None
    except PermissionError:
        return {"pid": pid, "probe_error": True}
    except OSError:
        return {"pid": pid, "probe_error": True}
    return {"pid": pid, "pgid": pgid, "birth": _process_birth_token(pid)}


def _group_members(pgid: int) -> set[int]:
    members: set[int] = set()
    proc = Path("/proc")
    if proc.is_dir():
        try:
            children = list(proc.iterdir())
        except OSError as exc:
            raise RuntimeError("process probe failed") from exc
        for child in children:
            if not child.name.isdigit():
                continue
            try:
                raw = (child / "stat").read_text()
                fields = raw.rsplit(") ", 1)[1].split()
                if len(fields) <= 2:
                    raise ValueError("invalid process stat")
                if int(fields[2]) == pgid:
                    members.add(int(child.name))
            except (OSError, ValueError, IndexError) as exc:
                raise RuntimeError("process probe failed") from exc
        return members
    try:
        result = subprocess.run(("ps", "-axo", "pid=,pgid="), check=False, capture_output=True, text=True, timeout=1)
    except (OSError, subprocess.SubprocessError) as exc:
        raise RuntimeError("process probe failed") from exc
    if result.returncode != 0:
        raise RuntimeError("process probe failed")
    for line in result.stdout.splitlines():
        if not line.strip():
            continue
        parts = line.split()
        if len(parts) != 2:
            raise RuntimeError("process probe failed")
        try:
            if int(parts[1]) == pgid:
                members.add(int(parts[0]))
        except ValueError as exc:
            raise RuntimeError("process probe failed") from exc
    return members
def _process_group_state(pid: int | None, *, expected: dict[str, Any] | None = None) -> str:
    """Probe leader birth and process-group membership without signalling."""
    if pid is None or type(pid) is not int or pid <= 0:
        return "absent" if pid is None else "unknown"
    current = _capture_process_identity(pid)
    if current and current.get("probe_error"):
        return "unknown"
    expected_pgid = int(expected["pgid"]) if expected and expected.get("pgid") is not None else (int(current["pgid"]) if current else None)
    if current is not None:
        if expected and expected.get("birth") is not None and current.get("birth") != expected.get("birth"):
            return "unknown"
        if expected and expected.get("pgid") is not None and int(current["pgid"]) != int(expected["pgid"]):
            return "unknown"
        try:
            members = _group_members(expected_pgid) if expected_pgid is not None else set()
        except RuntimeError:
            return "unknown"
        return "live" if pid in members else "unknown"
    if expected_pgid is not None:
        try:
            members = _group_members(expected_pgid)
        except RuntimeError:
            return "unknown"
        if members:
            return "live"
    return "absent"


def _finish_application_run_locked(
    connection: sqlite3.Connection,
    *,
    run_id: int,
    status: Literal["review_ready", "manual", "blocked", "failed"],
    reason_code: str,
    observation_summary: Any | None = None,
    plan_summary: Any | None = None,
    artifact_dir: str | None = None,
    finished_at: str | None = None,
) -> None:
    """Apply application-run terminalization inside an existing transaction."""
    _require_public_code(status, "status", ("review_ready", "manual", "blocked", "failed"))
    _require_public_code(reason_code, "reason_code", PUBLIC_REASON_CODES)
    _require_reason_status(status, reason_code)
    if artifact_dir is not None:
        _require_run_artifact_ref(artifact_dir, run_id)
    if finished_at is not None:
        _require_exact_text(finished_at, "finished_at")
    current = connection.execute(
        "SELECT observation_json, artifact_dir FROM application_runs WHERE id=?",
        (run_id,),
    ).fetchone()
    if current is None:
        raise RuntimeError("application run is not running")
    if current["artifact_dir"] is not None and artifact_dir is not None and current["artifact_dir"] != artifact_dir:
        raise RuntimeError("artifact registration conflict")
    current_json = _decode_run_json(current["observation_json"])
    process = current_json.get("_process", {})
    session_state = current_json.get("_session_state")
    spawn_attempted = current_json.get("_spawn_attempted")
    ever_open = bool(current_json.get("_ever_open_guarded")) or session_state in {"open", "open_guarded"}
    observation = _decode_run_json(_redacted_summary(observation_summary or {}))
    if isinstance(process, dict) and process:
        observation["_process"] = process
    if session_state in {"starting", "prepared", "open", "open_guarded"}:
        observation["_session_state"] = session_state
    if type(spawn_attempted) is bool:
        observation["_spawn_attempted"] = spawn_attempted
    if ever_open:
        observation["_ever_open_guarded"] = True
    handoff_intent = current_json.get("_handoff_intent")
    if isinstance(handoff_intent, dict):
        observation["_handoff_intent"] = handoff_intent
    changed = connection.execute(
        """
        UPDATE application_runs
        SET status=?, reason_code=?, finished_at=?, observation_json=?, plan_json=?,
            artifact_dir=COALESCE(?, artifact_dir)
        WHERE status='running' AND reason_code IS NULL AND outcome IS NULL AND reviewed_at IS NULL AND id=?
        """,
        (
            status,
            reason_code,
            finished_at or utc_now(),
            encode_json(observation),
            _redacted_summary(plan_summary or {}),
            artifact_dir,
            run_id,
        ),
    ).rowcount
    if changed != 1:
        raise RuntimeError("application run is not running")


def finish_application_run(
    connection: sqlite3.Connection,
    *,
    run_id: int,
    status: Literal["review_ready", "manual", "blocked", "failed"],
    reason_code: str,
    observation_summary: Any | None = None,
    plan_summary: Any | None = None,
    artifact_dir: str | None = None,
    finished_at: str | None = None,
) -> None:
    connection.execute("BEGIN IMMEDIATE")
    try:
        _finish_application_run_locked(
            connection,
            run_id=run_id,
            status=status,
            reason_code=reason_code,
            observation_summary=observation_summary,
            plan_summary=plan_summary,
            artifact_dir=artifact_dir,
            finished_at=finished_at,
        )
    except Exception:
        connection.rollback()
        raise
    connection.commit()

def _latest_run_id(connection: sqlite3.Connection, job_id: int) -> int | None:
    row = connection.execute("SELECT id FROM application_runs WHERE job_id=? ORDER BY id DESC LIMIT 1", (job_id,)).fetchone()
    return int(row["id"]) if row else None


def _require_process_groups_absent(connection: sqlite3.Connection, row: sqlite3.Row) -> None:
    process = _decode_run_json(row["observation_json"]).get("_process", {})
    for column, kind in (("owner_pid", "owner"), ("browser_pid", "browser")):
        pid = row[column]
        if pid is None:
            continue
        identity = process.get(kind) if isinstance(process, dict) else None
        if not isinstance(identity, dict):
            raise RuntimeError("window_state_unknown")
        identity_pgid = identity.get("pgid")
        if not isinstance(identity_pgid, int) or identity_pgid <= 0:
            raise RuntimeError("window_state_unknown")
        if (
            type(identity.get("pid")) is not int
            or identity.get("pid") != pid
            or (kind == "owner" and identity_pgid != pid)
            or type(identity.get("birth")) is not str
            or not identity["birth"]
        ):
            raise RuntimeError("window_state_unknown")
        state = _process_group_state(pid, expected=identity)
        if state == "live":
            raise RuntimeError("window_live")
        if state != "absent":
            raise RuntimeError("window_state_unknown")


def _remove_confined_tree(parent_fd: int, name: str) -> None:
    try:
        fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0), dir_fd=parent_fd)
    except FileNotFoundError:
        return
    try:
        st = os.fstat(fd)
        if not stat.S_ISDIR(st.st_mode) or st.st_uid != os.geteuid() or stat.S_IMODE(st.st_mode) != 0o700:
            raise RuntimeError("window_state_unknown")
        for child in os.listdir(fd):
            child_st = os.stat(child, dir_fd=fd, follow_symlinks=False)
            if stat.S_ISDIR(child_st.st_mode):
                _remove_confined_tree(fd, child)
            else:
                if not stat.S_ISREG(child_st.st_mode) or child_st.st_uid != os.geteuid():
                    raise RuntimeError("window_state_unknown")
                os.unlink(child, dir_fd=fd)
        os.fsync(fd)
    finally:
        os.close(fd)
    os.rmdir(name, dir_fd=parent_fd)


def _cleanup_review_ephemera(root: ArtifactRoot, run_id: int) -> None:
    try:
        root_fd = root._require_fd()
    except Exception as exc:
        raise RuntimeError("window_state_unknown") from exc
    run_fd = os.open(root.ref_for_run(run_id), os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0), dir_fd=root_fd)
    try:
        for child in ("browser-profile", "input"):
            _remove_confined_tree(run_fd, child)
        os.fsync(run_fd)
    finally:
        os.close(run_fd)

def _read_review_manifest(root: ArtifactRoot, row: sqlite3.Row) -> dict[str, Any]:
    run_id = int(row["id"])
    if row["artifact_dir"] != root.ref_for_run(run_id):
        raise RuntimeError("window_state_unknown")
    dir_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0)
    try:
        run_fd = os.open(root.ref_for_run(run_id), dir_flags, dir_fd=root._require_fd())
    except OSError as exc:
        raise RuntimeError("window_state_unknown") from exc
    try:
        run_st = os.fstat(run_fd)
        if not stat.S_ISDIR(run_st.st_mode) or run_st.st_uid != os.geteuid() or stat.S_IMODE(run_st.st_mode) != 0o700:
            raise RuntimeError("window_state_unknown")
        fd = os.open("review_session.json", os.O_RDONLY | os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0), dir_fd=run_fd)
        try:
            st = os.fstat(fd)
            if not stat.S_ISREG(st.st_mode) or st.st_uid != os.geteuid() or stat.S_IMODE(st.st_mode) != 0o600:
                raise RuntimeError("window_state_unknown")
            chunks: list[bytes] = []
            total = 0
            while True:
                chunk = os.read(fd, min(65536, 131072 - total + 1))
                if not chunk:
                    break
                chunks.append(chunk)
                total += len(chunk)
                if total > 131072:
                    raise RuntimeError("window_state_unknown")
            raw = b"".join(chunks)
        finally:
            os.close(fd)
    except (FileNotFoundError, OSError) as exc:
        raise RuntimeError("window_state_unknown") from exc
    finally:
        os.close(run_fd)
    try:
        manifest = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError("window_state_unknown") from exc
    if not isinstance(manifest, dict):
        raise RuntimeError("window_state_unknown")
    if type(manifest.get("version")) is not int or manifest["version"] != 1:
        raise RuntimeError("window_state_unknown")
    if type(manifest.get("run_id")) is not int or manifest["run_id"] <= 0 or manifest["run_id"] != run_id:
        raise RuntimeError("window_state_unknown")
    if type(manifest.get("job_id")) is not int or manifest["job_id"] <= 0 or manifest["job_id"] != int(row["job_id"]):
        raise RuntimeError("window_state_unknown")
    if manifest.get("session_id") != row["session_id"]:
        raise RuntimeError("window_state_unknown")
    for kind in ("owner", "browser"):
        identity = _manifest_identity(manifest, kind)
        if identity is not None:
            for suffix in ("pid", "pgid", "birth"):
                if manifest.get(f"{kind}_{suffix}") != identity[suffix]:
                    raise RuntimeError("window_state_unknown")
    token_hash = manifest.get("commit_token_sha256")
    if token_hash is not None and (not isinstance(token_hash, str) or len(token_hash) != 64 or any(char not in "0123456789abcdef" for char in token_hash)):
        raise RuntimeError("window_state_unknown")
    return manifest


def _read_run_manifest(root: ArtifactRoot, row: sqlite3.Row) -> dict[str, Any]:
    run_id = int(row["id"])
    if row["artifact_dir"] != root.ref_for_run(run_id):
        raise RuntimeError("window_state_unknown")
    run_fd = os.open(root.ref_for_run(run_id), os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0), dir_fd=root._require_fd())
    try:
        fd = os.open("run.json", os.O_RDONLY | os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0), dir_fd=run_fd)
        try:
            st = os.fstat(fd)
            if not stat.S_ISREG(st.st_mode) or st.st_uid != os.geteuid() or stat.S_IMODE(st.st_mode) != 0o600:
                raise RuntimeError("window_state_unknown")
            raw = os.read(fd, 131073)
        finally:
            os.close(fd)
    except (FileNotFoundError, OSError) as exc:
        raise RuntimeError("window_state_unknown") from exc
    finally:
        os.close(run_fd)
    if len(raw) > 131072:
        raise RuntimeError("window_state_unknown")
    try:
        manifest = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError("window_state_unknown") from exc
    if (
        not isinstance(manifest, dict)
        or type(manifest.get("run_id")) is not int
        or manifest["run_id"] <= 0
        or manifest["run_id"] != run_id
        or type(manifest.get("job_id")) is not int
        or manifest["job_id"] <= 0
        or manifest["job_id"] != int(row["job_id"])
    ):
        raise RuntimeError("window_state_unknown")
    token_hash = manifest.get("commit_token_sha256")
    if token_hash is not None and (not isinstance(token_hash, str) or len(token_hash) != 64 or any(char not in "0123456789abcdef" for char in token_hash)):
        raise RuntimeError("window_state_unknown")
    return manifest
def _review_window_token_matches_run(
    root: ArtifactRoot,
    row: sqlite3.Row,
    manifest: dict[str, Any],
    state: Any,
) -> bool:
    """Require an exact run-token binding for guarded (or token-bearing) windows."""
    review_token = manifest.get("commit_token_sha256")
    if review_token is None and state != "open_guarded":
        return True
    try:
        run_manifest = _read_run_manifest(root, row)
    except (OSError, RuntimeError, ValueError):
        return False
    expected_token = run_manifest.get("commit_token_sha256")
    return (
        isinstance(review_token, str)
        and isinstance(expected_token, str)
        and hmac.compare_digest(review_token, expected_token)
    )


def _manifest_identity(manifest: dict[str, Any], kind: str) -> dict[str, Any] | None:
    value = manifest.get(f"{kind}_identity")
    if value is None:
        return None
    if not isinstance(value, dict) or set(value) != {"pid", "pgid", "birth"}:
        raise RuntimeError("window_state_unknown")
    if (
        type(value["pid"]) is not int
        or type(value["pgid"]) is not int
        or value["pid"] <= 0
        or value["pgid"] <= 0
        or type(value["birth"]) is not str
        or not value["birth"]
        or len(value["birth"]) > 256
        or any(ord(char) < 0x20 or ord(char) > 0x7E for char in value["birth"])
    ):
        raise RuntimeError("window_state_unknown")
    return {"pid": value["pid"], "pgid": value["pgid"], "birth": value["birth"]}


def _terminal_manifest_reason(manifest: dict[str, Any], state: Any) -> str | None:
    """Return a terminal code only when its status mapping is fail-closed."""
    terminal_reason = manifest.get("terminal_reason")
    if type(terminal_reason) is not str or terminal_reason not in PUBLIC_REASON_CODES:
        return None
    if REASON_STATUS.get(terminal_reason) not in {"failed", "blocked"}:
        return None
    if state in {"starting", "prepared"}:
        return "handoff_failed" if terminal_reason == "handoff_failed" else None
    if state in {"open", "open_guarded", "failed", "closed"}:
        return terminal_reason
    return None


def _validate_review_window(
    connection: sqlite3.Connection,
    row: sqlite3.Row,
    root: ArtifactRoot,
    *,
    confirm_window_closed: bool,
) -> dict[str, Any] | None:
    _require_process_groups_absent(connection, row)
    if row["session_id"] is None:
        return None
    manifest = _read_review_manifest(root, row)
    state = manifest.get("state")
    review_token = manifest.get("commit_token_sha256")
    if review_token is not None:
        run_manifest = _read_run_manifest(root, row)
        expected_token = run_manifest.get("commit_token_sha256")
        if not isinstance(expected_token, str) or not hmac.compare_digest(review_token, expected_token):
            raise RuntimeError("window_state_unknown")
    elif state == "open_guarded":
        raise RuntimeError("window_state_unknown")
    owner_identity = _manifest_identity(manifest, "owner")
    browser_identity = _manifest_identity(manifest, "browser")
    observation = _decode_run_json(row["observation_json"])
    process = observation.get("_process", {})
    no_process_start = (
        state == "starting"
        and manifest.get("spawn_attempted") is False
        and observation.get("_spawn_attempted") is False
        and owner_identity is None
        and row["owner_pid"] is None
        and isinstance(process, dict)
        and process.get("owner") is None
    )
    if not no_process_start and (
        owner_identity is None
        or manifest.get("owner_pid") != owner_identity["pid"]
        or row["owner_pid"] != owner_identity["pid"]
        or not isinstance(process, dict)
        or process.get("owner") != owner_identity
    ):
        raise RuntimeError("window_state_unknown")
    if browser_identity is None:
        if row["browser_pid"] is not None or manifest.get("browser_pid") is not None:
            raise RuntimeError("window_state_unknown")
        if process.get("browser") is not None:
            raise RuntimeError("window_state_unknown")
    elif (
        manifest.get("browser_pid") != browser_identity["pid"]
        or row["browser_pid"] != browser_identity["pid"]
        or process.get("browser") != browser_identity
    ):
        raise RuntimeError("window_state_unknown")
    if state in {"prepared", "open", "open_guarded"} and browser_identity is None:
        raise RuntimeError("window_state_unknown")
    cleanup_value = manifest.get("cleanup")
    cleanup = cleanup_value is True or (type(cleanup_value) is str and cleanup_value in {"complete", "confirmed_stale"})
    terminal_reason = manifest.get("terminal_reason")
    if terminal_reason is not None:
        effective_reason = _terminal_manifest_reason(manifest, state)
        reconciled = observation.get("_terminal_reconciled")
        if (
            effective_reason is None
            or row["status"] != REASON_STATUS[effective_reason]
            or row["reason_code"] != effective_reason
            or not isinstance(reconciled, dict)
            or reconciled.get("session_id") != row["session_id"]
            or reconciled.get("reason_code") != effective_reason
        ):
            raise RuntimeError("terminal manifest requires reconciliation")
        if cleanup:
            return manifest
        raise RuntimeError("window_state_unknown")
    if state == "failed":
        raise RuntimeError("window_state_unknown")
    if state == "closed" and cleanup:
        return manifest
    if state not in {"starting", "prepared", "open", "open_guarded"}:
        raise RuntimeError("window_state_unknown")
    if not confirm_window_closed:
        raise RuntimeError("window_state_unknown")
    _cleanup_review_ephemera(root, int(row["id"]))
    updated = dict(manifest)
    updated["state"] = "closed"
    updated["cleanup"] = "confirmed_stale"
    with root.create_run_dir(int(row["id"])) as run:
        run.write_json("review_session.json", updated)
    return updated

def _load_review_row(
    connection: sqlite3.Connection,
    run_id: int,
    root: ArtifactRoot,
    *,
    confirm_window_closed: bool,
) -> sqlite3.Row:
    row = connection.execute("SELECT * FROM application_runs WHERE id=?", (run_id,)).fetchone()
    if row is None:
        raise RuntimeError("run_not_found")
    if row["reviewed_at"] is not None:
        raise RuntimeError("run_already_reviewed")
    latest = _latest_run_id(connection, int(row["job_id"]))
    if latest != run_id:
        raise RuntimeError("not latest run")
    if connection.execute(
        "SELECT 1 FROM application_runs WHERE job_id=? AND status='running' AND id<>? LIMIT 1",
        (int(row["job_id"]), run_id),
    ).fetchone() is not None:
        raise RuntimeError("state_conflict")
    _validate_review_window(connection, row, root, confirm_window_closed=confirm_window_closed)
    return row


def _manifest_reached_open_guarded(row: sqlite3.Row) -> bool:
    observation = _decode_run_json(row["observation_json"])
    return bool(
        observation.get("_ever_open_guarded")
        or observation.get("_session_state") in {"open", "open_guarded"}
    )


def _window_cleanup_value(value: Any) -> bool:
    return value is True or (type(value) is str and value in {"complete", "confirmed_stale"})


def _window_heartbeat_fresh(value: Any) -> bool:
    if type(value) is not str:
        return False
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return False
    if parsed.tzinfo is None:
        return False
    age = (datetime.now(timezone.utc) - parsed).total_seconds()
    return 0 <= age <= 15


def _window_state_for_row(connection: sqlite3.Connection, row: sqlite3.Row, root: ArtifactRoot) -> str:
    if row["session_id"] is None:
        return "none"
    try:
        manifest = _read_review_manifest(root, row)
    except (OSError, RuntimeError, ValueError):
        return "unknown"
    state = manifest.get("state")
    if type(state) is not str:
        return "unknown"
    if not _review_window_token_matches_run(root, row, manifest, state):
        return "unknown"
    observation = _decode_run_json(row["observation_json"])
    process = observation.get("_process", {})
    if not isinstance(process, dict):
        return "unknown"
    try:
        owner = _manifest_identity(manifest, "owner")
        browser = _manifest_identity(manifest, "browser")
    except RuntimeError:
        return "unknown"
    if row["owner_pid"] is None:
        if owner is not None or state != "starting" or manifest.get("spawn_attempted") is not False:
            return "unknown"
        return "starting"
    if owner is None or owner["pid"] != row["owner_pid"] or process.get("owner") != owner:
        return "unknown"
    try:
        owner_state = _process_group_state(row["owner_pid"], expected=owner)
    except RuntimeError:
        return "unknown"
    if owner_state == "unknown":
        return "unknown"
    browser_state = "absent"
    if row["browser_pid"] is None:
        if browser is not None or process.get("browser") is not None:
            return "unknown"
    else:
        if browser is None or browser["pid"] != row["browser_pid"] or process.get("browser") != browser:
            return "unknown"
        try:
            browser_state = _process_group_state(row["browser_pid"], expected=browser)
        except RuntimeError:
            return "unknown"
        if browser_state == "unknown":
            return "unknown"
    if state == "open_guarded":
        if owner_state == "live" and browser_state == "live" and _window_heartbeat_fresh(manifest.get("heartbeat")):
            return "open"
        if owner_state == "absent" and browser_state == "absent":
            return "stale"
        return "unknown"
    if state == "closed":
        if _window_cleanup_value(manifest.get("cleanup")) and owner_state == "absent" and browser_state == "absent":
            return "closed"
        return "unknown"
    if state == "failed":
        return "failed" if owner_state == "absent" and browser_state == "absent" else "unknown"
    if state in {"starting", "prepared"}:
        return state
    if state == "open":
        return "stale" if owner_state == "absent" and browser_state == "absent" else "unknown"
    return "unknown"




def _public_artifact_ref(value: Any, run_id: int) -> str | None:
    if value is None:
        return None
    try:
        return _require_existing_artifact_ref(value, run_id)
    except (TypeError, ValueError):
        return None
def review_window_state(
    connection: sqlite3.Connection,
    *,
    run_id: int,
    artifact_root: ArtifactRoot,
) -> str:
    if type(run_id) is not int or run_id <= 0 or not isinstance(artifact_root, ArtifactRoot):
        return "unknown"
    try:
        _bind_artifact_root(connection, artifact_root, create=False)
        row = connection.execute("SELECT * FROM application_runs WHERE id=?", (run_id,)).fetchone()
        if row is None:
            return "unknown"
        return _window_state_for_row(connection, row, artifact_root)
    except (OSError, RuntimeError, ValueError, sqlite3.DatabaseError):
        return "unknown"


def list_application_reviews(
    connection: sqlite3.Connection,
    *,
    limit: int = 10,
    artifact_root: ArtifactRoot,
) -> list[dict[str, Any]]:
    if type(limit) is not int or not 1 <= limit <= 100:
        raise ValueError("limit must be between 1 and 100")
    if not isinstance(artifact_root, ArtifactRoot):
        raise TypeError("artifact_root must be an ArtifactRoot")
    _bind_artifact_root(connection, artifact_root, create=False)
    rows = connection.execute(
        """
        SELECT r.*, r.id AS run_id, j.title, j.company
        FROM application_runs AS r
        JOIN jobs AS j ON j.id=r.job_id
        WHERE j.status='in_progress'
          AND r.reviewed_at IS NULL AND r.outcome IS NULL
          AND r.id=(SELECT MAX(r2.id) FROM application_runs AS r2 WHERE r2.job_id=r.job_id)
        ORDER BY r.started_at DESC, r.id DESC
        LIMIT ?
        """,
        (limit,),
    ).fetchall()
    return [
        {
            "run_id": int(row["run_id"]),
            "job_id": int(row["job_id"]),
            "status": str(row["status"]),
            "title": str(row["title"]),
            "company": str(row["company"]),
            "reason_code": None if row["reason_code"] is None else str(row["reason_code"]),
            "artifact_ref": _public_artifact_ref(row["artifact_dir"], int(row["run_id"])),
            "finished_at": row["finished_at"],
            "outcome": None,
            "window_state": _window_state_for_row(connection, row, artifact_root),
        }
        for row in rows
    ]


_REVIEW_STAGES = frozenset(
    {"claimed", "action_planned", "action_applied", "prepared", "finished", "failed"}
)
_REVIEW_EVIDENCE_REQUIRED_STAGES = frozenset({"prepared", "finished"})
_PUBLIC_BLOCKER_CODES = frozenset(
    {
        "captcha",
        "authentication_required",
        "assessment_required",
        "unsupported_frame",
        "page_validation_error",
        "observation_too_large",
    }
)
# Producer-valid (stage, operation) pairs derived from application.py callsites.
_BROWSER_FAILURE_PAIRS = frozenset(
    {
        ("startup", "start"),
        ("navigation", "goto"),
        ("observation", "observe"),
        ("observation", "route"),
        ("observation", "screenshot"),
        ("blocker", "screenshot"),
        ("final", "screenshot"),
        ("handoff", "screenshot"),
        ("mutation", "route"),
        ("mutation", "click_offline"),
        ("mutation", "upload"),
        ("mutation", "select"),
        ("mutation", "check"),
        ("mutation", "fill"),
        ("handoff", "prepare_handoff"),
        ("handoff", "commit_handoff"),
        ("cleanup", "close"),
    }
)
_REVIEW_ARTIFACT_PATHS = {
    "observation": "observation.json",
    "plan": "plan.json",
    "actions": "actions.json",
    "browser_failure": "browser_failure.json",
}
_JOB_STATUSES = ("queued", "in_progress", "archived")
_MAX_PUBLIC_TITLE_LENGTH = 512
_MAX_PUBLIC_COMPANY_LENGTH = 512
_MAX_PUBLIC_TIMESTAMP_LENGTH = 64
_MAX_REVIEW_MANIFEST_BYTES = 128 * 1024
_MAX_REVIEW_ARTIFACT_BYTES = 1024 * 1024
_MAX_REVIEW_ITERATION = 100


def _review_manifest_error() -> RuntimeError:
    return RuntimeError("manifest_error")


def _require_public_manifest_code(value: Any, choices: tuple[str, ...]) -> str:
    if type(value) is not str or value not in choices:
        raise _review_manifest_error()
    return value


def _require_public_manifest_optional_code(
    value: Any, choices: tuple[str, ...]
) -> str | None:
    if value is None:
        return None
    return _require_public_manifest_code(value, choices)


def _require_public_positive_int(value: Any) -> int:
    if type(value) is not int or value <= 0:
        raise _review_manifest_error()
    return value


def _require_public_text(value: Any, *, max_length: int) -> str:
    if (
        type(value) is not str
        or not value
        or len(value) > max_length
        or any(ord(c) < 32 for c in value)
    ):
        raise _review_manifest_error()
    return value


def _require_public_optional_text(value: Any, *, max_length: int) -> str | None:
    if value is None:
        return None
    return _require_public_text(value, max_length=max_length)


def _require_manifest_string(value: Any, *, max_length: int = 256) -> str:
    if type(value) is not str or not value or len(value) > max_length:
        raise _review_manifest_error()
    return value


def _require_manifest_int(value: Any, *, min_value: int = 0, max_value: int | None = None) -> int:
    if type(value) is not int or value < min_value or (max_value is not None and value > max_value):
        raise _review_manifest_error()
    return value


def _require_manifest_bool(value: Any) -> bool:
    if value is True:
        return True
    if value is False:
        return False
    raise _review_manifest_error()


def _read_review_run_manifest(run: Any, run_id: int, job_id: int) -> dict[str, Any]:
    try:
        raw = run.read_bytes("run.json", max_bytes=_MAX_REVIEW_MANIFEST_BYTES)
    except _artifacts.ArtifactSecurityError as exc:
        raise _review_manifest_error() from exc
    except (FileNotFoundError, OSError):
        raise _review_manifest_error() from None
    try:
        manifest = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise _review_manifest_error() from None
    if not isinstance(manifest, dict):
        raise _review_manifest_error()
    if (
        type(manifest.get("run_id")) is not int
        or manifest["run_id"] <= 0
        or manifest["run_id"] != run_id
    ):
        raise _review_manifest_error()
    if (
        type(manifest.get("job_id")) is not int
        or manifest["job_id"] <= 0
        or manifest["job_id"] != job_id
    ):
        raise _review_manifest_error()
    if type(manifest.get("ats_policy")) is not str or manifest["ats_policy"] not in SUPPORTED_ATS_POLICIES:
        raise _review_manifest_error()
    if manifest.get("no_final_submit") is not True:
        raise _review_manifest_error()
    stage = manifest.get("stage")
    if type(stage) is not str or stage not in _REVIEW_STAGES:
        raise _review_manifest_error()
    latest = manifest.get("latest")
    if not isinstance(latest, dict):
        raise _review_manifest_error()
    _require_manifest_int(
        latest.get("iteration"), min_value=0, max_value=_MAX_REVIEW_ITERATION
    )
    latest_stage = _require_manifest_string(latest.get("stage"), max_length=64)
    if latest_stage not in _REVIEW_STAGES:
        raise _review_manifest_error()
    artifacts = manifest.get("artifacts")
    if not isinstance(artifacts, dict):
        raise _review_manifest_error()
    return manifest


def _read_manifest_artifact(
    run: Any,
    manifest: dict[str, Any],
    key: str,
    *,
    required: bool = True,
) -> Any:
    artifacts = manifest.get("artifacts")
    if not isinstance(artifacts, dict):
        raise _review_manifest_error()
    descriptor = artifacts.get(key)
    if descriptor is None:
        if required:
            raise _review_manifest_error()
        return None
    if not isinstance(descriptor, dict):
        raise _review_manifest_error()
    path = descriptor.get("path")
    sha256 = descriptor.get("sha256")
    iteration = descriptor.get("iteration")
    stage = descriptor.get("stage")
    if type(path) is not str or not path or type(sha256) is not str or len(sha256) != 64:
        raise _review_manifest_error()
    if any(char not in "0123456789abcdef" for char in sha256):
        raise _review_manifest_error()
    _require_manifest_int(iteration, max_value=_MAX_REVIEW_ITERATION)
    _require_manifest_string(stage, max_length=64)
    if stage not in _REVIEW_STAGES:
        raise _review_manifest_error()
    expected_path = _REVIEW_ARTIFACT_PATHS.get(key)
    if expected_path is not None and path != expected_path:
        raise _review_manifest_error()
    try:
        _artifacts._validate_relative_artifact_path(path)
    except _artifacts.ArtifactSecurityError:
        raise _review_manifest_error() from None
    try:
        raw = run.read_bytes(path, max_bytes=_MAX_REVIEW_ARTIFACT_BYTES, expected_sha256=sha256)
    except _artifacts.ArtifactSecurityError as exc:
        raise _review_manifest_error() from exc
    except (FileNotFoundError, OSError):
        raise _review_manifest_error() from None
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise _review_manifest_error() from None


def _review_observation_summary(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise _review_manifest_error()
    required_keys = {"field_count", "button_count", "required_count", "final_marker_count", "error_count", "blocker_codes"}
    if not required_keys.issubset(value):
        raise _review_manifest_error()
    field_count = _require_manifest_int(value["field_count"], max_value=10_000)
    button_count = _require_manifest_int(value["button_count"], max_value=10_000)
    required_count = _require_manifest_int(value["required_count"], max_value=10_000)
    final_marker_count = _require_manifest_int(value["final_marker_count"], max_value=10_000)
    error_count = _require_manifest_int(value["error_count"], max_value=10_000)
    blocker_codes = value["blocker_codes"]
    if not isinstance(blocker_codes, list) or len(blocker_codes) > 100:
        raise _review_manifest_error()
    if any(
        type(code) is not str or code not in _PUBLIC_BLOCKER_CODES
        for code in blocker_codes
    ):
        raise _review_manifest_error()
    return {
        "field_count": field_count,
        "button_count": button_count,
        "required_count": required_count,
        "final_marker_count": final_marker_count,
        "error_count": error_count,
        "blocker_codes": list(blocker_codes),
    }


def _review_plan_summary(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise _review_manifest_error()
    required_keys = {"status", "reason_code", "answer_count", "skipped_target_count", "resume_upload", "safe_click"}
    if not required_keys.issubset(value):
        raise _review_manifest_error()
    status = _require_manifest_string(value["status"], max_length=64)
    if status not in {"ready", "manual"}:
        raise _review_manifest_error()
    reason_code = _require_manifest_string(value["reason_code"], max_length=64)
    if reason_code not in PUBLIC_REASON_CODES:
        raise _review_manifest_error()
    answer_count = _require_manifest_int(value["answer_count"], max_value=10_000)
    skipped_target_count = _require_manifest_int(value["skipped_target_count"], max_value=10_000)
    resume_upload = _require_manifest_bool(value["resume_upload"])
    safe_click = _require_manifest_bool(value["safe_click"])
    return {
        "status": status,
        "reason_code": reason_code,
        "answer_count": answer_count,
        "skipped_target_count": skipped_target_count,
        "resume_upload": resume_upload,
        "safe_click": safe_click,
    }


def _review_actions_summary(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise _review_manifest_error()
    required_keys = {"mutation_count", "actions", "final_submit_calls"}
    if not required_keys.issubset(value):
        raise _review_manifest_error()
    mutation_count = _require_manifest_int(value["mutation_count"], max_value=10_000)
    actions = value["actions"]
    final_submit_calls = _require_manifest_int(value["final_submit_calls"], min_value=0, max_value=0)
    if not isinstance(actions, list) or len(actions) > 10_000:
        raise _review_manifest_error()
    if any(not isinstance(item, dict) for item in actions):
        raise _review_manifest_error()
    return {
        "mutation_count": mutation_count,
        "action_count": len(actions),
        "final_submit_calls": final_submit_calls,
    }


def _review_browser_failure_summary(
    value: Any, *, manifest_ats_policy: str
) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise _review_manifest_error()
    required_keys = {"stage", "operation", "code", "iteration", "ats_policy", "no_final_submit"}
    if not required_keys.issubset(value):
        raise _review_manifest_error()
    stage = _require_manifest_string(value["stage"], max_length=64)
    operation = _require_manifest_string(value["operation"], max_length=64)
    if (stage, operation) not in _BROWSER_FAILURE_PAIRS:
        raise _review_manifest_error()
    code = _require_manifest_string(value["code"], max_length=128)
    if code not in SAFE_BROWSER_ERROR_CODES:
        raise _review_manifest_error()
    iteration = _require_manifest_int(value["iteration"], max_value=_MAX_REVIEW_ITERATION)
    ats_policy = _require_manifest_string(value["ats_policy"], max_length=64)
    if ats_policy != manifest_ats_policy:
        raise _review_manifest_error()
    no_final_submit = _require_manifest_bool(value["no_final_submit"])
    if not no_final_submit:
        raise _review_manifest_error()
    return {
        "stage": stage,
        "operation": operation,
        "code": code,
        "iteration": iteration,
        "ats": ats_policy,
        "no_final_submit": no_final_submit,
    }


def get_application_review_details(
    connection: sqlite3.Connection,
    *,
    run_id: int,
    artifact_root: ArtifactRoot,
) -> dict[str, Any]:
    """Return a fixed, redacted summary for one persisted application run.

    This is a read-only operation: it opens the existing DB-bound artifact
    root, validates the run manifest, and projects only bounded public fields
    from the manifest-indexed evidence artifacts.
    """
    if type(run_id) is not int or run_id <= 0:
        raise TypeError("run_id must be a positive integer")
    if not isinstance(artifact_root, ArtifactRoot):
        raise TypeError("artifact_root must be an ArtifactRoot")
    _bind_artifact_root(connection, artifact_root, create=False)
    row = connection.execute(
        """
        SELECT r.*, j.title, j.company, j.status AS job_status
        FROM application_runs AS r
        JOIN jobs AS j ON j.id = r.job_id
        WHERE r.id = ?
        """,
        (run_id,),
    ).fetchone()
    if row is None:
        raise RuntimeError("run_not_found")
    try:
        artifact_ref = _require_existing_artifact_ref(row["artifact_dir"], run_id)
    except (TypeError, ValueError):
        raise _review_manifest_error() from None

    job_id = _require_public_positive_int(row["job_id"])
    status = _require_public_manifest_code(row["status"], APPLICATION_STATUSES)
    job_status = _require_public_manifest_code(row["job_status"], _JOB_STATUSES)
    reason_code = _require_public_manifest_optional_code(
        row["reason_code"], PUBLIC_REASON_CODES
    )
    if reason_code is not None:
        try:
            _require_reason_status(status, reason_code, allow_legacy=True)
        except ValueError:
            raise _review_manifest_error() from None
    outcome = _require_public_manifest_optional_code(
        row["outcome"], APPLICATION_OUTCOMES
    )
    title = _require_public_text(
        row["title"], max_length=_MAX_PUBLIC_TITLE_LENGTH
    )
    company = _require_public_text(
        row["company"], max_length=_MAX_PUBLIC_COMPANY_LENGTH
    )
    started_at = _require_public_text(
        row["started_at"], max_length=_MAX_PUBLIC_TIMESTAMP_LENGTH
    )
    finished_at = _require_public_optional_text(
        row["finished_at"], max_length=_MAX_PUBLIC_TIMESTAMP_LENGTH
    )
    reviewed_at = _require_public_optional_text(
        row["reviewed_at"], max_length=_MAX_PUBLIC_TIMESTAMP_LENGTH
    )

    try:
        run = artifact_root.open_artifact_ref(artifact_ref, run_id=run_id)
    except OSError as exc:
        raise ArtifactSecurityError("artifact run is unavailable") from exc
    with run:
        manifest = _read_review_run_manifest(run, run_id, job_id)
        manifest_stage = _require_manifest_string(manifest["stage"], max_length=64)
        manifest_ats = _require_manifest_string(
            manifest["ats_policy"], max_length=64
        )
        evidence_required = manifest_stage in _REVIEW_EVIDENCE_REQUIRED_STAGES
        observation = _read_manifest_artifact(
            run, manifest, "observation", required=evidence_required
        )
        plan = _read_manifest_artifact(
            run, manifest, "plan", required=evidence_required
        )
        actions = _read_manifest_artifact(
            run, manifest, "actions", required=evidence_required
        )
        failure = _read_manifest_artifact(
            run, manifest, "browser_failure", required=False
        )

    observation_summary = (
        _review_observation_summary(observation) if observation is not None else None
    )
    plan_summary = _review_plan_summary(plan) if plan is not None else None
    actions_summary = _review_actions_summary(actions) if actions is not None else None

    latest = manifest.get("latest")
    evidence_latest: dict[str, Any] | None = None
    if isinstance(latest, dict):
        evidence_latest = {
            "iteration": _require_manifest_int(
                latest["iteration"], max_value=_MAX_REVIEW_ITERATION
            ),
            "stage": _require_manifest_string(latest["stage"], max_length=64),
        }

    result: dict[str, Any] = {
        "run_id": run_id,
        "job_id": job_id,
        "status": status,
        "reason_code": reason_code,
        "started_at": started_at,
        "finished_at": finished_at,
        "outcome": outcome,
        "reviewed_at": reviewed_at,
        "title": title,
        "company": company,
        "job_status": job_status,
        "ats": manifest_ats,
        "artifact_ref": artifact_ref,
        "window_state": review_window_state(
            connection, run_id=run_id, artifact_root=artifact_root
        ),
        "evidence": {
            "ats": manifest_ats,
            "stage": manifest_stage,
            "latest": evidence_latest,
            "no_final_submit": True,
        },
        "observation": observation_summary,
        "plan": plan_summary,
        "actions": actions_summary,
        "browser_failure": None,
    }
    if failure is not None:
        result["browser_failure"] = _review_browser_failure_summary(
            failure, manifest_ats_policy=manifest_ats
        )
    return result


def complete_review(
    connection: sqlite3.Connection,
    *,
    run_id: int,
    outcome: Literal["submitted", "skipped"],
    artifact_root: ArtifactRoot,
    confirm_window_closed: bool = False,
) -> dict[str, Any]:
    _require_public_code(outcome, "outcome", ("submitted", "skipped"))
    if not isinstance(artifact_root, ArtifactRoot):
        raise TypeError("artifact_root must be an ArtifactRoot")
    _bind_artifact_root(connection, artifact_root, create=False)
    connection.execute("BEGIN IMMEDIATE")
    try:
        row = _load_review_row(connection, run_id, artifact_root, confirm_window_closed=confirm_window_closed)
        if row["status"] == "running":
            raise RuntimeError("window_state_unknown")
        if outcome == "submitted" and (
            row["status"] == "failed"
            or (
                row["status"] == "blocked"
                and row["reason_code"] == "unsafe_network_attempt"
                and not _manifest_reached_open_guarded(row)
            )
        ):
            raise RuntimeError("failed pre-open runs cannot be submitted")
        now = utc_now()
        changed = connection.execute(
            "UPDATE application_runs SET outcome=?, reviewed_at=? WHERE id=? AND reviewed_at IS NULL AND outcome IS NULL",
            (outcome, now, run_id),
        ).rowcount
        if changed != 1:
            raise RuntimeError("run review CAS failed")
        job_changed = connection.execute(
            "UPDATE jobs SET status='archived' WHERE id=? AND status='in_progress'",
            (row["job_id"],),
        ).rowcount
        if job_changed != 1:
            raise RuntimeError("state_conflict")
        result = {
            "run_id": run_id,
            "job_id": int(row["job_id"]),
            "status": row["status"],
            "reason_code": row["reason_code"],
            "outcome": outcome,
            "job_status": "archived",
            "window_state": "closed",
        }
    except Exception:
        connection.rollback()
        raise
    connection.commit()
    return result
def retry_review(connection: sqlite3.Connection, *, run_id: int, artifact_root: ArtifactRoot, confirm_window_closed: bool = False) -> dict[str, Any]:
    if not isinstance(artifact_root, ArtifactRoot):
        raise TypeError("artifact_root must be an ArtifactRoot")
    _bind_artifact_root(connection, artifact_root, create=False)
    connection.execute("BEGIN IMMEDIATE")
    try:
        row = _load_review_row(connection, run_id, artifact_root, confirm_window_closed=confirm_window_closed)
        now = utc_now()
        status = row["status"]
        reason_code = row["reason_code"]
        if status == "running":
            status = "failed"
            reason_code = "abandoned_running_attempt"
        if row["status"] == "running":
            changed = connection.execute(
                "UPDATE application_runs SET status=?, reason_code=?, finished_at=?, outcome='retry', reviewed_at=? WHERE id=? AND status='running' AND reviewed_at IS NULL AND outcome IS NULL",
                (status, reason_code, now, now, run_id),
            ).rowcount
        else:
            changed = connection.execute(
                "UPDATE application_runs SET outcome='retry', reviewed_at=? WHERE id=? AND reviewed_at IS NULL AND outcome IS NULL",
                (now, run_id),
            ).rowcount
        if changed != 1:
            raise RuntimeError("run retry CAS failed")
        job_changed = connection.execute(
            "UPDATE jobs SET status='queued' WHERE id=? AND status='in_progress'",
            (row["job_id"],),
        ).rowcount
        if job_changed != 1:
            raise RuntimeError("state_conflict")
        result = {
            "run_id": run_id,
            "job_id": int(row["job_id"]),
            "status": status,
            "reason_code": reason_code,
            "outcome": "retry",
            "job_status": "queued",
            "window_state": "closed",
        }
    except Exception:
        connection.rollback()
        raise
    connection.commit()
    return result


def reconcile_open_session_failure(
    connection: sqlite3.Connection,
    *,
    run_id: int,
    session_id: str | None,
    reason_code: str = "browser_error",
    artifact_root: ArtifactRoot | None = None,
) -> bool:
    # The arguments are compatibility hints only; the confined manifest is
    # authoritative for identity, terminal phase, and failure code.
    _require_public_code(reason_code, "reason_code", PUBLIC_REASON_CODES)
    if not isinstance(artifact_root, ArtifactRoot):
        return False
    # Bind before beginning the transaction or reading any run row.  A root
    # from another database must never be allowed to inspect this run.
    _bind_artifact_root(connection, artifact_root, create=False)
    connection.execute("BEGIN IMMEDIATE")
    try:
        row = connection.execute("SELECT * FROM application_runs WHERE id=?", (run_id,)).fetchone()
        if (
            row is None
            or row["reviewed_at"] is not None
            or row["status"] not in {"running", "review_ready", "manual", "blocked", "failed"}
        ):
            connection.commit()
            return False
        if _latest_run_id(connection, int(row["job_id"])) != run_id or row["session_id"] is None:
            connection.commit()
            return False
        if session_id is not None and session_id != row["session_id"]:
            connection.commit()
            return False

        try:
            manifest = _read_review_manifest(artifact_root, row)
        except RuntimeError:
            connection.commit()
            return False
        state = manifest.get("state")
        review_token = manifest.get("commit_token_sha256")
        closed_page_not_stable = (
            state == "closed"
            and manifest.get("terminal_reason") == "page_not_stable"
            and _window_cleanup_value(manifest.get("cleanup"))
            and isinstance(review_token, str)
        )
        if review_token is not None:
            run_manifest = _read_run_manifest(artifact_root, row)
            expected_token = run_manifest.get("commit_token_sha256")
            if not isinstance(expected_token, str) or not hmac.compare_digest(review_token, expected_token):
                connection.commit()
                return False
        elif state == "open_guarded":
            connection.commit()
            return False
        try:
            owner_identity = _manifest_identity(manifest, "owner")
            browser_identity = _manifest_identity(manifest, "browser")
        except RuntimeError:
            connection.commit()
            return False
        if closed_page_not_stable and (
            owner_identity is None or browser_identity is None
        ):
            connection.commit()
            return False
        observation = _decode_run_json(row["observation_json"])
        process = observation.get("_process", {})
        no_process_start = (
            state == "starting"
            and manifest.get("spawn_attempted") is False
            and observation.get("_spawn_attempted") is False
            and owner_identity is None
            and row["owner_pid"] is None
            and isinstance(process, dict)
            and process.get("owner") is None
        )
        no_process_terminal = (
            state == "failed"
            and manifest.get("spawn_attempted") is False
            and observation.get("_spawn_attempted") is False
            and manifest.get("terminal_reason") == "handoff_failed"
            and manifest.get("cleanup") is True
            and owner_identity is None
            and row["owner_pid"] is None
            and isinstance(process, dict)
            and process.get("owner") is None
        )
        no_process = no_process_start or no_process_terminal
        if not no_process:
            if owner_identity is None or row["owner_pid"] != owner_identity["pid"] or manifest.get("owner_pid") != owner_identity["pid"]:
                connection.commit()
                return False
            if not isinstance(process, dict) or process.get("owner") != owner_identity:
                connection.commit()
                return False
        if browser_identity is None:
            if row["browser_pid"] is not None or manifest.get("browser_pid") is not None:
                connection.commit()
                return False
            if isinstance(process, dict) and process.get("browser") is not None:
                connection.commit()
                return False
        elif (
            manifest.get("browser_pid") != browser_identity["pid"]
            or row["browser_pid"] != browser_identity["pid"]
            or not isinstance(process, dict)
            or process.get("browser") != browser_identity
        ):
            connection.commit()
            return False
        if (
            state in {"starting", "prepared"}
            and manifest.get("spawn_attempted") is True
            and observation.get("_spawn_attempted") is True
            and (owner_identity is None or browser_identity is None)
        ):
            connection.commit()
            return False
        if state in {"prepared", "open", "open_guarded"} and browser_identity is None:
            connection.commit()
            return False
        stale_spawn_shape = (
            state in {"starting", "prepared"}
            and manifest.get("spawn_attempted") is True
            and observation.get("_spawn_attempted") is True
            and owner_identity is not None
            and browser_identity is not None
            and not (
                row["status"] == "running"
                and row["reason_code"] is None
                and manifest.get("terminal_reason") == "handoff_failed"
            )
        )
        try:
            _require_process_groups_absent(connection, row)
        except RuntimeError:
            if stale_spawn_shape:
                connection.commit()
                return False
            raise
        if stale_spawn_shape:
            if row["status"] != "failed" or row["reason_code"] != "browser_error":
                connection.commit()
                return False
            _cleanup_review_ephemera(artifact_root, run_id)
            updated_manifest = dict(manifest)
            updated_manifest["state"] = "failed"
            updated_manifest["terminal_reason"] = "browser_error"
            updated_manifest["cleanup"] = "confirmed_stale"
            with artifact_root.open_run_dir(run_id) as run:
                run.replace_json("review_session.json", updated_manifest)
            manifest = updated_manifest
            state = "failed"
        if no_process_start:
            _cleanup_review_ephemera(artifact_root, run_id)
            updated_manifest = dict(manifest)
            updated_manifest["state"] = "failed"
            updated_manifest["terminal_reason"] = "handoff_failed"
            updated_manifest["cleanup"] = True
            with artifact_root.open_run_dir(run_id) as run:
                run.replace_json("review_session.json", updated_manifest)
            manifest = updated_manifest
            state = "failed"

        cleanup_value = manifest.get("cleanup")
        cleanup = (
            no_process
            or cleanup_value is True
            or (type(cleanup_value) is str and cleanup_value in {"complete", "confirmed_stale"})
        )
        if not cleanup or (state == "closed" and not closed_page_not_stable):
            connection.commit()
            return False
        if state not in {"starting", "prepared", "open", "open_guarded", "failed", "closed"}:
            connection.commit()
            return False
        effective_reason = (
            "page_not_stable"
            if closed_page_not_stable
            else (
                "handoff_failed"
                if no_process
                else _terminal_manifest_reason(manifest, state)
            )
        )
        if effective_reason is None:
            connection.commit()
            return False
        reconciled = observation.get("_terminal_reconciled")
        if (
            isinstance(reconciled, dict)
            and reconciled.get("session_id") == row["session_id"]
            and reconciled.get("reason_code") == effective_reason
        ):
            connection.commit()
            return False

        observation["_terminal_reconciled"] = {
            "session_id": row["session_id"],
            "reason_code": effective_reason,
        }
        if state in {"open", "open_guarded"}:
            observation["_ever_open_guarded"] = True
        target_status = REASON_STATUS[effective_reason]
        changed = connection.execute(
            """
            UPDATE application_runs
            SET status=?, reason_code=?, finished_at=COALESCE(finished_at, ?),
                observation_json=?
            WHERE id=? AND reviewed_at IS NULL AND outcome IS NULL
              AND status IN ('running', 'review_ready', 'manual', 'blocked', 'failed')
            """,
            (target_status, effective_reason, utc_now(), encode_json(observation), run_id),
        ).rowcount
        if changed != 1:
            connection.rollback()
            return False
    except Exception:
        connection.rollback()
        raise
    connection.commit()
    return True
def _require_description_override(value: Any) -> str | None:
    if value is None:
        return None
    if type(value) is not str:
        raise TypeError("description_override must be a string or None")
    if not value.strip():
        raise ValueError("description_override must be non-blank")
    return value


def _build_resume_snapshot_from_job_row(
    row: sqlite3.Row,
    *,
    description_override: str | None = None,
) -> Any:
    description_override = _require_description_override(description_override)
    requirements = None
    if description_override is None and row["raw_json"]:
        try:
            raw_data = json.loads(row["raw_json"])
            if isinstance(raw_data, dict) and "requirements" in raw_data and isinstance(raw_data["requirements"], (list, tuple)):
                requirements = [str(r) for r in raw_data["requirements"]]
        except Exception:
            pass

    from .resume import build_job_resume_snapshot
    return build_job_resume_snapshot(
        job_id=int(row["id"]),
        title=str(row["title"]),
        company=str(row["company"]),
        description_text=(
            description_override
            if description_override is not None
            else str(row["description"] or "")
        ),
        canonical_application_url=str(row["canonical_url"] or ""),
        location=str(row["location"]) if row["location"] else None,
        source_identifier=str(row["source_job_id"] or row["source"] or ""),
        requirements=requirements,
    )


def read_resume_job(
    connection: sqlite3.Connection,
    job_id: int | str | None = None,
    next_queued: bool = False,
    description_override: str | None = None,
) -> Any | None:
    """Read a job snapshot without mutating job status or claiming."""
    description_override = _require_description_override(description_override)
    row = None
    if job_id is not None:
        try:
            job_id_int = int(job_id)
        except (ValueError, TypeError):
            raise TypeError("job_id must be an integer or integer string") from None
        row = connection.execute("SELECT * FROM jobs WHERE id=?", (job_id_int,)).fetchone()
    elif next_queued:
        row = connection.execute(
            """
            SELECT * FROM jobs
            WHERE status='queued' AND canonical_url IS NOT NULL
            ORDER BY posted_at DESC NULLS LAST, first_seen_at ASC, id ASC
            LIMIT 1
            """
        ).fetchone()
    else:
        return None

    if row is None:
        return None

    return _build_resume_snapshot_from_job_row(
        row,
        description_override=description_override,
    )


def get_job_resume_snapshot(
    connection: sqlite3.Connection,
    job_id: int | str,
    description_override: str | None = None,
) -> Any | None:
    """Explicit job snapshot query without claiming/mutation."""
    return read_resume_job(
        connection,
        job_id=job_id,
        description_override=description_override,
    )


def get_next_queued_job_resume_snapshot(connection: sqlite3.Connection) -> Any | None:
    """Deterministic next queued job snapshot query without claiming/mutation."""
    return read_resume_job(connection, next_queued=True)


def get_ready_generated_resume(
    connection: sqlite3.Connection,
    job_id: int | str,
    *,
    job_snapshot_sha256: str | None = None,
    profile_sha256: str | None = None,
    source_resume_sha256: str | None = None,
    generation_config_sha256: str | None = None,
    raw_object: bool = False,
    public_shaping: bool = False,
) -> Any | None:
    """Idempotent ready lookup for generated resume artifacts."""
    try:
        job_id_int = int(job_id)
    except (ValueError, TypeError):
        raise TypeError("job_id must be an integer or integer string") from None

    clauses = ["job_id = ?", "state = 'ready'"]
    params: list[Any] = [job_id_int]

    if job_snapshot_sha256 is not None:
        clauses.append("job_snapshot_sha256 = ?")
        params.append(job_snapshot_sha256)
    if profile_sha256 is not None:
        clauses.append("profile_sha256 = ?")
        params.append(profile_sha256)
    if source_resume_sha256 is not None:
        clauses.append("source_resume_sha256 = ?")
        params.append(source_resume_sha256)
    if generation_config_sha256 is not None:
        clauses.append("generation_config_sha256 = ?")
        params.append(generation_config_sha256)

    sql = f"SELECT * FROM generated_resumes WHERE {' AND '.join(clauses)} ORDER BY created_at DESC LIMIT 1"
    row = connection.execute(sql, params).fetchone()
    if row is None:
        return None

    row_dict = dict(row)
    row_dict["status"] = row_dict["state"]
    if raw_object:
        from pathlib import Path
        from .resume import GeneratedResumeArtifact
        return GeneratedResumeArtifact(
            resume_id=str(row_dict["resume_id"]),
            job_id=int(row_dict["job_id"]),
            job_snapshot_sha256=str(row_dict["job_snapshot_sha256"]),
            profile_sha256=str(row_dict["profile_sha256"]),
            source_resume_sha256=str(row_dict["source_resume_sha256"]),
            generation_config_sha256=str(row_dict["generation_config_sha256"]),
            content_sha256=str(row_dict["content_sha256"] or ""),
            pdf_sha256=str(row_dict["pdf_sha256"] or ""),
            private_pdf_path=Path(row_dict["private_pdf_path"]),
            created_at=str(row_dict["created_at"]),
        )

    if public_shaping:
        row_dict.pop("private_pdf_path", None)
        row_dict.pop("artifact_dir", None)
        row_dict.pop("score_json", None)
    return row_dict


def create_generated_resume(
    connection: sqlite3.Connection,
    artifact: Any = None,
    score: Any = None,
    *,
    job_id: int | str | None = None,
    job_snapshot_sha256: str | None = None,
    profile_sha256: str | None = None,
    source_resume_sha256: str | None = None,
    generation_config_sha256: str | None = None,
    resume_id: str | None = None,
    state: str | None = None,
    status: str | None = None,
    reason_code: str | None = None,
    artifact_dir: str | None = None,
    completed_at: str | None = None,
    content_sha256: str | None = None,
    pdf_sha256: str | None = None,
    private_pdf_path: str | Path | None = None,
    score_json: Any = None,
    force: bool = False,
    public_shaping: bool = True,
    raw_object: bool = False,
) -> Any:
    """Create a generated_resumes record, with force superseding semantics."""
    if artifact is not None:
        if hasattr(artifact, "resume_id"):
            resume_id = str(artifact.resume_id)
        if hasattr(artifact, "job_id"):
            job_id = artifact.job_id
        if hasattr(artifact, "job_snapshot_sha256"):
            job_snapshot_sha256 = str(artifact.job_snapshot_sha256)
        if hasattr(artifact, "profile_sha256"):
            profile_sha256 = str(artifact.profile_sha256)
        if hasattr(artifact, "source_resume_sha256"):
            source_resume_sha256 = str(artifact.source_resume_sha256)
        if hasattr(artifact, "generation_config_sha256"):
            generation_config_sha256 = str(artifact.generation_config_sha256)
        if hasattr(artifact, "content_sha256"):
            content_sha256 = str(artifact.content_sha256)
        if hasattr(artifact, "pdf_sha256"):
            pdf_sha256 = str(artifact.pdf_sha256)
        if hasattr(artifact, "private_pdf_path"):
            private_pdf_path = artifact.private_pdf_path

    effective_state = state if state is not None else (status if status is not None else "pending")
    if effective_state == "draft":
        effective_state = "generating"

    if content_sha256 and effective_state in ("pending", "generating") and not reason_code:
        effective_state = "ready"

    if job_id is None:
        raise ValueError("job_id must be provided")
    try:
        job_id_int = int(job_id)
    except (ValueError, TypeError):
        raise TypeError("job_id must be an integer or integer string") from None

    if resume_id is None:
        import uuid
        resume_id = str(uuid.uuid4())

    if not job_snapshot_sha256 or not profile_sha256 or not source_resume_sha256 or not generation_config_sha256:
        raise ValueError("All hash inputs (job_snapshot_sha256, profile_sha256, source_resume_sha256, generation_config_sha256) must be provided")

    _require_rpc_sha256(job_snapshot_sha256, "job_snapshot_sha256")
    _require_rpc_sha256(profile_sha256, "profile_sha256")
    _require_rpc_sha256(source_resume_sha256, "source_resume_sha256")
    _require_rpc_sha256(generation_config_sha256, "generation_config_sha256")

    if effective_state not in GENERATED_RESUMES_STATES:
        raise ValueError(f"state must be one of {GENERATED_RESUMES_STATES}")

    if not force and effective_state == "ready":
        existing_ready = get_ready_generated_resume(
            connection,
            job_id=job_id_int,
            job_snapshot_sha256=job_snapshot_sha256,
            profile_sha256=profile_sha256,
            source_resume_sha256=source_resume_sha256,
            generation_config_sha256=generation_config_sha256,
            raw_object=raw_object,
            public_shaping=public_shaping,
        )
        if existing_ready is not None:
            return existing_ready

    now = utc_now()
    score_payload = score_json if score_json is not None else score
    encoded_score = encode_json(score_payload) if score_payload is not None else "{}"

    if effective_state in ("ready", "superseded", "failed") and completed_at is None:
        completed_at = now

    if effective_state == "failed":
        if not reason_code:
            raise ValueError("reason_code is required for failed state")
        content_sha256 = None
        pdf_sha256 = None
        private_pdf_path_str = None
    else:
        private_pdf_path_str = str(private_pdf_path) if private_pdf_path is not None else None

    connection.execute("BEGIN IMMEDIATE")
    try:
        if effective_state == "ready":
            connection.execute(
                """
                UPDATE generated_resumes
                SET state = 'superseded', updated_at = ?, completed_at = COALESCE(completed_at, ?)
                WHERE job_id = ?
                  AND job_snapshot_sha256 = ?
                  AND profile_sha256 = ?
                  AND source_resume_sha256 = ?
                  AND generation_config_sha256 = ?
                  AND state = 'ready'
                """,
                (now, now, job_id_int, job_snapshot_sha256, profile_sha256, source_resume_sha256, generation_config_sha256),
            )

        connection.execute(
            """
            INSERT INTO generated_resumes (
                resume_id, job_id, job_snapshot_sha256, profile_sha256, source_resume_sha256,
                generation_config_sha256, state, reason_code, artifact_dir, completed_at,
                content_sha256, pdf_sha256, private_pdf_path, created_at, updated_at, score_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                resume_id,
                job_id_int,
                job_snapshot_sha256,
                profile_sha256,
                source_resume_sha256,
                generation_config_sha256,
                effective_state,
                reason_code,
                artifact_dir,
                completed_at,
                content_sha256,
                pdf_sha256,
                private_pdf_path_str,
                now,
                now,
                encoded_score,
            ),
        )
    except Exception:
        connection.rollback()
        raise
    connection.commit()

    return get_generated_resume(connection, resume_id, public_shaping=public_shaping, raw_object=raw_object)


def transition_generated_resume_state(
    connection: sqlite3.Connection,
    resume_id: str,
    to_state: str,
    from_state: str | None = None,
    *,
    reason_code: str | None = None,
    artifact_dir: str | None = None,
    completed_at: str | None = None,
    content_sha256: str | None = None,
    pdf_sha256: str | None = None,
    private_pdf_path: str | Path | None = None,
    score: Any = None,
    public_shaping: bool = True,
) -> dict[str, Any]:
    """Transition a generated_resumes record state."""
    target_state = to_state
    if target_state == "draft":
        target_state = "generating"
    expected_from = from_state
    if expected_from == "draft":
        expected_from = "generating"

    if target_state not in GENERATED_RESUMES_STATES:
        raise ValueError(f"to_state must be one of {GENERATED_RESUMES_STATES}")

    connection.execute("BEGIN IMMEDIATE")
    try:
        row = connection.execute(
            "SELECT * FROM generated_resumes WHERE resume_id = ?", (resume_id,)
        ).fetchone()
        if row is None:
            raise KeyError(f"resume_id {resume_id} not found")

        current_state = row["state"]
        if expected_from is not None and current_state != expected_from:
            raise ValueError(f"State transition mismatch: expected {expected_from}, found {current_state}")

        if current_state in ("failed", "superseded"):
            raise ValueError(f"Terminal state '{current_state}' cannot be transitioned to '{target_state}'")

        if current_state == "ready" and target_state not in ("superseded", "ready"):
            raise ValueError(f"Ready resume can only be transitioned to superseded, got '{target_state}'")

        job_id_int = int(row["job_id"])
        now = utc_now()

        eff_completed_at = completed_at or row["completed_at"]
        if target_state in ("ready", "superseded", "failed") and not eff_completed_at:
            eff_completed_at = now

        eff_private_path = str(private_pdf_path) if private_pdf_path is not None else row["private_pdf_path"]

        if target_state == "ready":
            connection.execute(
                """
                UPDATE generated_resumes
                SET state = 'superseded', updated_at = ?, completed_at = COALESCE(completed_at, ?)
                WHERE job_id = ?
                  AND job_snapshot_sha256 = ?
                  AND profile_sha256 = ?
                  AND source_resume_sha256 = ?
                  AND generation_config_sha256 = ?
                  AND state = 'ready'
                  AND resume_id <> ?
                """,
                (
                    now,
                    now,
                    job_id_int,
                    row["job_snapshot_sha256"],
                    row["profile_sha256"],
                    row["source_resume_sha256"],
                    row["generation_config_sha256"],
                    resume_id,
                ),
            )

        if target_state == "failed":
            if not reason_code and not row["reason_code"]:
                raise ValueError("reason_code is required for failed state")
            eff_content = None
            eff_pdf = None
            eff_private_path = None
        else:
            eff_content = content_sha256 if content_sha256 is not None else row["content_sha256"]
            eff_pdf = pdf_sha256 if pdf_sha256 is not None else row["pdf_sha256"]

        encoded_score = encode_json(score) if score is not None else row["score_json"]

        connection.execute(
            """
            UPDATE generated_resumes SET
                state = ?,
                reason_code = COALESCE(?, reason_code),
                artifact_dir = COALESCE(?, artifact_dir),
                completed_at = ?,
                content_sha256 = ?,
                pdf_sha256 = ?,
                private_pdf_path = ?,
                updated_at = ?,
                score_json = ?
            WHERE resume_id = ?
            """,
            (
                target_state,
                reason_code,
                artifact_dir,
                eff_completed_at,
                eff_content,
                eff_pdf,
                eff_private_path,
                now,
                encoded_score,
                resume_id,
            ),
        )
    except Exception:
        connection.rollback()
        raise
    connection.commit()

    res = get_generated_resume(connection, resume_id, public_shaping=public_shaping)
    return res if isinstance(res, dict) else {}


def transition_generated_resume(
    connection: sqlite3.Connection,
    resume_id: str,
    from_state: str,
    to_state: str,
    **kwargs: Any,
) -> dict[str, Any]:
    return transition_generated_resume_state(
        connection, resume_id=resume_id, to_state=to_state, from_state=from_state, **kwargs
    )


def get_generated_resume(
    connection: sqlite3.Connection,
    resume_id: str,
    *,
    public_shaping: bool = True,
    raw_object: bool = False,
) -> Any:
    row = connection.execute(
        "SELECT * FROM generated_resumes WHERE resume_id = ?", (resume_id,)
    ).fetchone()
    if row is None:
        return None

    row_dict = dict(row)
    row_dict["status"] = row_dict["state"]

    if raw_object and row_dict["state"] == "ready" and row_dict["content_sha256"] and row_dict["pdf_sha256"] and row_dict["private_pdf_path"]:
        from pathlib import Path
        from .resume import GeneratedResumeArtifact
        return GeneratedResumeArtifact(
            resume_id=str(row_dict["resume_id"]),
            job_id=int(row_dict["job_id"]),
            job_snapshot_sha256=str(row_dict["job_snapshot_sha256"]),
            profile_sha256=str(row_dict["profile_sha256"]),
            source_resume_sha256=str(row_dict["source_resume_sha256"]),
            generation_config_sha256=str(row_dict["generation_config_sha256"]),
            content_sha256=str(row_dict["content_sha256"]),
            pdf_sha256=str(row_dict["pdf_sha256"]),
            private_pdf_path=Path(row_dict["private_pdf_path"]),
            created_at=str(row_dict["created_at"]),
        )

    if public_shaping:
        row_dict.pop("private_pdf_path", None)
        row_dict.pop("artifact_dir", None)
        row_dict.pop("score_json", None)

    return row_dict


def get_generated_resume_private(
    connection: sqlite3.Connection,
    resume_id: str,
) -> dict[str, Any] | None:
    """Explicit private resolver for internal service (returns all fields including paths and scoring)."""
    return get_generated_resume(connection, resume_id, public_shaping=False, raw_object=False)


def list_generated_resumes(
    connection: sqlite3.Connection,
    job_id: str | int | None = None,
    limit: int = 50,
    offset: int = 0,
    *,
    state: str | None = None,
    status: str | None = None,
    public_shaping: bool = True,
) -> tuple[dict[str, Any], ...]:
    eff_state = state if state is not None else status
    clauses = ["1=1"]
    params: list[Any] = []

    if job_id is not None:
        try:
            clauses.append("job_id = ?")
            params.append(int(job_id))
        except (ValueError, TypeError):
            raise TypeError("job_id must be an integer or integer string") from None

    if eff_state is not None:
        clauses.append("state = ?")
        params.append(eff_state)

    params.extend([limit, offset])
    sql = f"""
    SELECT * FROM generated_resumes
    WHERE {' AND '.join(clauses)}
    ORDER BY created_at DESC, resume_id ASC
    LIMIT ? OFFSET ?
    """
    rows = connection.execute(sql, params).fetchall()

    results: list[dict[str, Any]] = []
    for r in rows:
        d = dict(r)
        d["status"] = d["state"]
        if public_shaping:
            d.pop("private_pdf_path", None)
            d.pop("artifact_dir", None)
            d.pop("score_json", None)
        results.append(d)

    return tuple(results)


def format_public_generated_resume(resume_row: dict[str, Any] | sqlite3.Row) -> dict[str, Any]:
    d = dict(resume_row)
    d.pop("private_pdf_path", None)
    d.pop("artifact_dir", None)
    d.pop("score_json", None)
    return d


def _insert_application_resume_binding_locked(
    connection: sqlite3.Connection,
    *,
    resume_id: str,
    run_id: int,
    bound_at: str,
    replace_existing: bool,
) -> None:
    conflict_clause = (
        " ON CONFLICT(run_id) DO UPDATE SET "
        "resume_id = excluded.resume_id, bound_at = excluded.bound_at"
        if replace_existing
        else ""
    )
    connection.execute(
        f"""
        INSERT INTO application_resume_bindings (resume_id, run_id, bound_at)
        VALUES (?, ?, ?){conflict_clause}
        """,
        (resume_id, run_id, bound_at),
    )


def bind_generated_resume_to_application(
    connection: sqlite3.Connection,
    resume_id: str,
    run_id: int,
) -> dict[str, Any]:
    """Durably bind a ready generated resume to an application run of the same job."""
    if type(run_id) is not int or run_id <= 0:
        raise TypeError("run_id must be a positive integer")
    if type(resume_id) is not str or not resume_id:
        raise TypeError("resume_id must be a non-empty string")

    connection.execute("BEGIN IMMEDIATE")
    try:
        resume = connection.execute(
            "SELECT * FROM generated_resumes WHERE resume_id = ?", (resume_id,)
        ).fetchone()
        if resume is None:
            raise KeyError(f"Resume {resume_id} not found")
        if resume["state"] != "ready":
            raise ValueError(f"Resume {resume_id} state is '{resume['state']}', must be 'ready'")

        run = connection.execute(
            "SELECT * FROM application_runs WHERE id = ?", (run_id,)
        ).fetchone()
        if run is None:
            raise KeyError(f"Application run {run_id} not found")

        if int(resume["job_id"]) != int(run["job_id"]):
            raise ValueError(
                f"Binding mismatch: resume job_id {resume['job_id']} does not match application run job_id {run['job_id']}"
            )

        now = utc_now()
        _insert_application_resume_binding_locked(
            connection,
            resume_id=resume_id,
            run_id=run_id,
            bound_at=now,
            replace_existing=True,
        )
    except Exception:
        connection.rollback()
        raise
    connection.commit()
    return {"resume_id": resume_id, "run_id": run_id, "bound_at": now}
