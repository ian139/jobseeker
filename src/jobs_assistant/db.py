from __future__ import annotations

import hashlib
import hmac
import ipaddress
import json
import os
import sqlite3
import stat
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from . import artifacts as _artifacts
from .artifacts import ArtifactRoot, ArtifactSecurityError
from .contracts import ApplicationClaim, PublicReasonCode, StoredJobInfo
from .browser_adapter import SAFE_BROWSER_ERROR_CODES
from .safety import SUPPORTED_ATS_POLICIES


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


def decode_json(value: str | None, default: Any) -> Any:
    if not value:
        return default
    return json.loads(value)


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
    path = parsed.path.rstrip("/") or "/"
    return urlunsplit((parsed.scheme.lower() or "https", parsed.netloc.lower(), path, urlencode(query, doseq=True), ""))


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


def _normalize_sql(sql: str | None) -> str:
    return _canonicalize_sql(sql)


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


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
        indexes[name] = {"sql": _normalize_sql(row["sql"]), "unique": int(listing["unique"]) if listing else 0, "partial": int(listing["partial"]) if listing else 0, "columns": columns}
    return {
        "columns": _application_columns(connection),
        "table_sql": _normalize_sql(sql_row["sql"] if sql_row else ""),
        "xinfo": [dict(row) for row in connection.execute("PRAGMA table_xinfo(application_runs)").fetchall()],
        "foreign_keys": [dict(row) for row in connection.execute("PRAGMA foreign_key_list(application_runs)").fetchall()],
        "indexes": indexes,
        "triggers": [dict(row) for row in connection.execute("SELECT name, sql FROM sqlite_schema WHERE type='trigger' AND tbl_name='application_runs' ORDER BY name").fetchall()],
        "sequence": _sequence_semantics(connection).get("row"),
        "sequence_semantics": _sequence_semantics(connection),
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


def _ensure_core_schema(connection: sqlite3.Connection) -> None:
    for statement in _sql_statements(SCHEMA_SQL):
        connection.execute(statement)
    columns = {row["name"] for row in connection.execute("PRAGMA table_xinfo(sync_runs)").fetchall()}
    if "checkpoint" not in columns:
        connection.execute("ALTER TABLE sync_runs ADD COLUMN checkpoint TEXT")


def _db_namespace(connection: sqlite3.Connection) -> str:
    row = connection.execute("PRAGMA database_list").fetchone()
    identity = str(row[2]) if row and row[2] else f"memory:{id(connection)}"
    return hashlib.sha256(identity.encode("utf-8", "surrogatepass")).hexdigest()[:20]
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


def initialize_database(connection: sqlite3.Connection, *, migration_artifact_root: ArtifactRoot) -> None:
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


def claim_next_application_job(connection: sqlite3.Connection, *, owner: str) -> ApplicationClaim | None:
    if type(owner) is not str or not owner.strip():
        raise TypeError("owner must be a non-empty string")
    connection.execute("BEGIN IMMEDIATE")
    try:
        row = connection.execute(
            """
            SELECT * FROM jobs
            WHERE status='queued' AND canonical_url IS NOT NULL
            ORDER BY posted_at DESC NULLS LAST, first_seen_at ASC, id ASC
            LIMIT 1
            """
        ).fetchone()
        if row is None:
            connection.commit()
            return None
        changed = connection.execute("UPDATE jobs SET status='in_progress' WHERE id=? AND status='queued'", (row["id"],)).rowcount
        if changed != 1:
            connection.rollback()
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
        job = dict(selected)
    except Exception:
        connection.rollback()
        raise
    connection.commit()
    return ApplicationClaim(run_id=run_id, job=job)


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
        row = connection.execute("SELECT status, session_id, observation_json, artifact_dir FROM application_runs WHERE id=?", (run_id,)).fetchone()
        if row is None or row["status"] != "running" or row["artifact_dir"] is None:
            connection.rollback()
            return False
        if row["session_id"] is not None and row["session_id"] != session_id:
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
            f"SELECT status, {column}, owner_pid, session_id, artifact_dir, observation_json FROM application_runs WHERE id=?",
            (run_id,),
        ).fetchone()
        if row is None or row["status"] != "running" or row["session_id"] is None or row["artifact_dir"] is None:
            connection.rollback()
            return False
        if column == "browser_pid" and row["owner_pid"] is None:
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


def _connection_identity(connection: sqlite3.Connection) -> str:
    row = connection.execute("PRAGMA database_list").fetchone()
    return str(row[2]) if row and row[2] else f"memory:{id(connection)}"


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
    _require_public_code(status, "status", ("review_ready", "manual", "blocked", "failed"))
    _require_public_code(reason_code, "reason_code", PUBLIC_REASON_CODES)
    _require_reason_status(status, reason_code)
    if artifact_dir is not None:
        _require_run_artifact_ref(artifact_dir, run_id)
    if finished_at is not None:
        _require_exact_text(finished_at, "finished_at")
    connection.execute("BEGIN IMMEDIATE")
    try:
        current = connection.execute("SELECT observation_json, artifact_dir FROM application_runs WHERE id=?", (run_id,)).fetchone()
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
        changed = connection.execute(
            """
            UPDATE application_runs
            SET status=?, reason_code=?, finished_at=?, observation_json=?, plan_json=?,
                artifact_dir=COALESCE(?, artifact_dir)
            WHERE status='running' AND reason_code IS NULL AND outcome IS NULL AND reviewed_at IS NULL AND id=?
            """,
            (status, reason_code, finished_at or utc_now(), encode_json(observation), _redacted_summary(plan_summary or {}), artifact_dir, run_id),
        ).rowcount
        if changed != 1:
            raise RuntimeError("application run is not running")
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
        if not cleanup or state == "closed":
            connection.commit()
            return False
        if state not in {"starting", "prepared", "open", "open_guarded", "failed"}:
            connection.commit()
            return False
        effective_reason = (
            "handoff_failed"
            if no_process
            else _terminal_manifest_reason(manifest, state)
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
