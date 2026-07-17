from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import httpx
import os
import re
import secrets
import sqlite3
import stat
import sys
from pathlib import Path
from collections.abc import Mapping

from html.parser import HTMLParser

from . import __version__
from .application import (
    AnnotationError,
    AnnotationUnavailable,
    persist_review_annotation,
    run_application_workflow,
)
from .artifacts import ArtifactRoot, ArtifactSecurityError
from .browser_adapter import BrowserAdapterError, PuppeteerSession
from .contracts import ATSFilter, PublicReasonCode
from .ats import SUPPORTED_ATS, load_application_profile, load_applicant_description, load_resume_context
from .application_preferences import (
    APPLICATION_PREFERENCES_SCHEMA_VERSION,
    ApplicationPreferences,
    PreferenceMapping,
    PreferenceMatcher,
    PreferenceOptOut,
    PreferenceValidationError,
    load_application_preferences,
)
from .backlog import (
    BACKLOG_PUBLIC_FIELDS as _BACKLOG_PUBLIC_FIELDS,
    BACKLOG_STATUSES as _BACKLOG_STATUSES,
    BacklogArchiveConflictError,
    BacklogArchiveError,
    MAX_ARCHIVE_JOB_IDS,
    MAX_BACKLOG_LIMIT,
    MAX_BACKLOG_OFFSET,
    MAX_BACKLOG_SOURCE_CHARS,
    archive_queued_jobs,
    list_backlog_jobs,
)
from .application_profiles import load_application_profile_preset
from .db import (
    REASON_STATUS,
    complete_review,
    connect,
    connect_read_only,
    init_db,
    initialize_database,
    latest_sync_checkpoint,
    list_application_reviews,
    record_sync_run,
    retry_review,
    review_window_state,
    update_sync_run,
    utc_now,
)
from .job_source import extract_source_jobs, fetch_source_jobs, import_source_jobs
from .theirstack import (
    ATS_FILTER_NAMES,
    PROFILE_NAMES,
    ProfileName,
    TheirStackClient,
    TheirStackError,
    build_paid_fetch_payload,
    build_preview_payload,
    checkpoint_profile_key,
    response_total_results,
    sync_theirstack_response,
    validate_ats_filter_name,
)

DEFAULT_DB = Path(os.environ.get("DATABASE_URL", "data/jobs.sqlite3"))
DEFAULT_RESUME_FILE = Path("resume/Main_Resume.pdf")
DEFAULT_ARTIFACT_ROOT = Path("data/application-runs")
_AUTOFILL_STATUSES = frozenset({"review_ready", "manual", "blocked", "failed"})
_AUTOFILL_REASON_CODES = frozenset(code.value for code in PublicReasonCode if code.value != "legacy_run")
_REVIEW_REASON_CODES = frozenset(code.value for code in PublicReasonCode)
_AUTOFILL_WINDOW_STATES = frozenset({"open", "closed"})
_AUTOFILL_RESULT_FIELDS = frozenset(
    {"job_id", "run_id", "status", "reason_code", "ats", "artifact_ref", "window_state"}
)
_REVIEW_PUBLIC_FIELDS = frozenset(
    {
        "run_id",
        "job_id",
        "status",
        "reason_code",
        "title",
        "company",
        "artifact_ref",
        "finished_at",
        "outcome",
        "window_state",
    }
)
_REVIEW_STATUSES = frozenset({"running", "review_ready", "manual", "blocked", "failed"})
_REVIEW_OUTCOMES = frozenset({"submitted", "skipped", "retry"})
_REVIEW_WINDOW_STATES = frozenset({"open", "starting", "prepared", "closed", "stale", "failed", "none", "unknown"})
MAX_BACKLOG_DESCRIPTION_CHARS = 12_000


class _BacklogPlainTextParser(HTMLParser):
    """Extract bounded plain text without exposing markup or embedded scripts."""

    _BLOCK_TAGS = frozenset({"article", "br", "div", "li", "p", "section", "tr"})
    _IGNORED_TAGS = frozenset({"script", "style", "template"})

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._ignored_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        del attrs
        normalized = tag.lower()
        if normalized in self._IGNORED_TAGS:
            self._ignored_depth += 1
        elif not self._ignored_depth and normalized in self._BLOCK_TAGS:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        normalized = tag.lower()
        if normalized in self._IGNORED_TAGS:
            self._ignored_depth = max(0, self._ignored_depth - 1)
        elif not self._ignored_depth and normalized in self._BLOCK_TAGS:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self._ignored_depth:
            self.parts.append(data)


def _safe_backlog_description(raw: object) -> str | None:
    """Return bounded text-only listing description, preserving nullability."""
    if raw is None:
        return None
    if type(raw) is not str:
        raise _CliFailure("database_error")
    parser = _BacklogPlainTextParser()
    parser.feed(raw[: MAX_BACKLOG_DESCRIPTION_CHARS + 1])
    parser.close()
    text = "".join(parser.parts)
    text = re.sub(r"\r\n?", "\n", text)
    text = re.sub(r"[ \t]*\n[ \t]*", "\n", text)
    return text.strip()[:MAX_BACKLOG_DESCRIPTION_CHARS]


_PUBLIC_ERROR_MESSAGES = {
    "invalid_input": "autofill input was rejected",
    "artifact_root_error": "artifact root was rejected",
    "browser_preflight_error": "browser preflight failed",
    "database_error": "database operation failed",
    "database_privacy_error": "database privacy validation failed",
    "theirstack_error": "TheirStack paid sync failed; no jobs were written and the request was not replayed automatically",
    "workflow_error": "autofill workflow failed",
    "invalid_result": "autofill returned an invalid result",
    "run_not_found": "review run was not found",
    "not_latest_run": "review run is not latest",
    "run_already_reviewed": "review run was already reviewed",
    "window_live": "review window is still live",
    "window_state_unknown": "review window state is unknown",
    "manifest_error": "review manifest is invalid",
    "annotation_error": "review annotation was rejected",
    "annotation_unavailable": "review annotation is unavailable",
    "backlog_archive_confirmation": "backlog archive requires explicit confirmation",
    "backlog_archive_input": "backlog archive input was rejected",
    "backlog_archive_conflict": "backlog archive state conflict",
}


class _CliFailure(RuntimeError):
    """Internal failure carrying only a public, fixed error code."""

    def __init__(self, code: str):
        if code not in _PUBLIC_ERROR_MESSAGES:
            code = "workflow_error"
        super().__init__(code)
        self.code = code


def _emit_failure(code: str) -> int:
    """Write the one permitted redacted runtime error and return failure status."""
    safe_code = code if code in _PUBLIC_ERROR_MESSAGES else "workflow_error"
    print(
        json.dumps(
            {"error": {"code": safe_code, "message": _PUBLIC_ERROR_MESSAGES[safe_code]}},
            sort_keys=True,
        ),
        file=sys.stderr,
    )
    return 1

def _close_database(connection: object | None) -> None:
    if connection is None:
        return
    close = getattr(connection, "close", None)
    if callable(close):
        close()


def _sanitize_autofill_result(raw: object) -> dict[str, object]:
    """Validate one workflow row before exposing its fixed public projection."""
    if not isinstance(raw, Mapping) or set(raw) != _AUTOFILL_RESULT_FIELDS:
        raise _CliFailure("invalid_result")
    job_id = raw.get("job_id")
    run_id = raw.get("run_id")
    if type(job_id) is not int or job_id <= 0 or type(run_id) is not int or run_id <= 0:
        raise _CliFailure("invalid_result")
    status = raw.get("status")
    reason_code = raw.get("reason_code")
    ats = raw.get("ats")
    artifact_ref = raw.get("artifact_ref")
    window_state = raw.get("window_state")
    if (
        type(status) is not str
        or status not in _AUTOFILL_STATUSES
        or type(reason_code) is not str
        or reason_code not in _AUTOFILL_REASON_CODES
        or REASON_STATUS.get(reason_code) != status
        or type(ats) is not str
        or ats not in SUPPORTED_ATS
        or type(artifact_ref) is not str
        or artifact_ref != f"run-{run_id}"
        or type(window_state) is not str
        or window_state not in _AUTOFILL_WINDOW_STATES
    ):
        raise _CliFailure("invalid_result")
    return {
        "job_id": job_id,
        "run_id": run_id,
        "status": status,
        "reason_code": reason_code,
        "ats": ats,
        "artifact_ref": artifact_ref,
        "window_state": window_state,
    }


def _sanitize_autofill_results(raw: object) -> list[dict[str, object]]:
    if not isinstance(raw, (list, tuple)) or len(raw) > 10:
        raise _CliFailure("invalid_result")
    return [_sanitize_autofill_result(item) for item in raw]
def _valid_review_artifact_ref(value: object, run_id: int) -> str | None:
    if value is None:
        return None
    if type(value) is not str or not re.fullmatch(r"(?:run|legacy-run)-[0-9]+", value):
        raise _CliFailure("invalid_result")
    if value.rsplit("-", 1)[-1] != str(run_id):
        raise _CliFailure("invalid_result")
    return value


def _sanitize_review_row(raw: object) -> dict[str, object]:
    if not isinstance(raw, Mapping) or set(raw) != _REVIEW_PUBLIC_FIELDS:
        raise _CliFailure("invalid_result")
    run_id = raw.get("run_id")
    job_id = raw.get("job_id")
    if type(run_id) is not int or run_id <= 0 or type(job_id) is not int or job_id <= 0:
        raise _CliFailure("invalid_result")
    status = raw.get("status")
    raw_reason = raw.get("reason_code")
    reason = "" if status == "running" and raw_reason in (None, "None") else raw_reason
    if type(status) is not str or status not in _REVIEW_STATUSES or type(reason) is not str:
        raise _CliFailure("invalid_result")
    if status == "running":
        if reason != "":
            raise _CliFailure("invalid_result")
    elif reason not in _REVIEW_REASON_CODES or REASON_STATUS.get(reason) != status:
        raise _CliFailure("invalid_result")
    title = raw.get("title")
    company = raw.get("company")
    if (
        type(title) is not str
        or not title
        or len(title) > 2048
        or any(ord(char) < 0x20 and char not in "\t\n\r" for char in title)
        or type(company) is not str
        or not company
        or len(company) > 2048
        or any(ord(char) < 0x20 and char not in "\t\n\r" for char in company)
    ):
        raise _CliFailure("invalid_result")
    artifact_ref = _valid_review_artifact_ref(raw.get("artifact_ref"), run_id)
    finished_at = raw.get("finished_at")
    if finished_at is not None and (type(finished_at) is not str or len(finished_at) > 128):
        raise _CliFailure("invalid_result")
    if raw.get("outcome") is not None:
        raise _CliFailure("invalid_result")
    window_state = raw.get("window_state")
    if type(window_state) is not str or window_state not in _REVIEW_WINDOW_STATES:
        raise _CliFailure("invalid_result")
    return {
        "run_id": run_id,
        "job_id": job_id,
        "status": status,
        "reason_code": reason,
        "title": title,
        "company": company,
        "artifact_ref": artifact_ref,
        "finished_at": finished_at,
        "outcome": None,
        "window_state": window_state,
    }


def _sanitize_review_transition(raw: object, artifact_ref: str | None, *, outcome: str) -> dict[str, object]:
    fields = frozenset({"run_id", "job_id", "status", "reason_code", "outcome", "job_status", "window_state"})
    if not isinstance(raw, Mapping) or set(raw) != fields:
        raise _CliFailure("invalid_result")
    run_id = raw.get("run_id")
    job_id = raw.get("job_id")
    status = raw.get("status")
    reason = raw.get("reason_code")
    job_status = raw.get("job_status")
    if (
        type(run_id) is not int
        or run_id <= 0
        or type(job_id) is not int
        or job_id <= 0
        or type(status) is not str
        or status not in _REVIEW_STATUSES - {"running"}
        or type(reason) is not str
        or reason not in _REVIEW_REASON_CODES
        or REASON_STATUS.get(reason) != status
        or raw.get("outcome") != outcome
        or type(job_status) is not str
        or job_status not in ({"archived"} if outcome in {"submitted", "skipped"} else {"queued"})
        or raw.get("window_state") != "closed"
    ):
        raise _CliFailure("invalid_result")
    _valid_review_artifact_ref(artifact_ref, run_id)
    return {
        "run_id": run_id,
        "job_id": job_id,
        "status": status,
        "reason_code": reason,
        "outcome": outcome,
        "job_status": job_status,
        "artifact_ref": artifact_ref,
        "window_state": "closed",
    }




def _runtime_failure_code(exc: BaseException) -> str:
    """Map implementation failures to a fixed public code without inspecting text."""
    if isinstance(exc, _CliFailure):
        return exc.code
    if isinstance(exc, ArtifactSecurityError):
        return "artifact_root_error"
    if isinstance(exc, BrowserAdapterError):
        return "browser_preflight_error"
    if isinstance(exc, sqlite3.Error):
        return "database_error"
    if isinstance(exc, (TypeError, ValueError)):
        return "invalid_input"
    return "workflow_error"

_REVIEW_EXCEPTION_CODES = {
    "run_not_found",
    "not latest run",
    "run_already_reviewed",
    "window_live",
    "window_state_unknown",
    "state_conflict",
    "failed pre-open runs cannot be submitted",
    "run review CAS failed",
    "run retry CAS failed",
}


def _review_failure_code(exc: BaseException) -> str:
    if isinstance(exc, _CliFailure):
        return exc.code
    if isinstance(exc, sqlite3.Error):
        return "database_error"
    if isinstance(exc, (TypeError, ValueError)):
        return "invalid_input"
    detail = str(exc)
    if detail in _REVIEW_EXCEPTION_CODES:
        if detail in {"failed pre-open runs cannot be submitted", "run review CAS failed", "run retry CAS failed"}:
            return "state_conflict"
        if detail == "not latest run":
            return "not_latest_run"
        return detail
    if "manifest" in detail:
        return "manifest_error"
    if "window" in detail:
        return "window_state_unknown"
    if "artifact" in detail:
        return "artifact_root_error"
    return "database_error"


def _open_review_root(path: str | Path = DEFAULT_ARTIFACT_ROOT) -> ArtifactRoot:
    try:
        return ArtifactRoot.open(path, cwd=Path.cwd())
    except Exception as exc:
        raise _CliFailure("artifact_root_error") from exc

def _review_artifact_ref(connection: object, _root: ArtifactRoot, run_id: int) -> str | None:
    """Load the requested run's DB-bound artifact ref without a list-window cap."""
    try:
        row = connection.execute(
            "SELECT artifact_dir FROM application_runs WHERE id=?",
            (run_id,),
        ).fetchone()
    except Exception as exc:
        raise _CliFailure(_review_failure_code(exc)) from exc
    if row is None:
        return None
    try:
        value = row["artifact_dir"]
    except (IndexError, KeyError, TypeError):
        try:
            value = row[0]
        except (IndexError, KeyError, TypeError):
            raise _CliFailure("invalid_result") from None
    return _valid_review_artifact_ref(value, run_id)


def _run_review(args: argparse.Namespace) -> int:
    if args.review_command == "list" and (type(args.limit) is not int or not 1 <= args.limit <= 100):
        raise _CliFailure("invalid_input")
    root = _open_review_root(getattr(args, "artifact_root", DEFAULT_ARTIFACT_ROOT))
    connection = None
    try:
        with root:
            try:
                try:
                    connection = connect(args.db)
                    initialize_database(connection, migration_artifact_root=root)
                except (PermissionError, OSError) as exc:
                    raise _CliFailure("database_privacy_error") from exc
                except Exception as exc:
                    raise _CliFailure("database_error") from exc
                if args.review_command == "list":
                    try:
                        rows = list_application_reviews(connection, limit=args.limit, artifact_root=root)
                    except Exception as exc:
                        raise _CliFailure(_review_failure_code(exc)) from exc
                    output = [_sanitize_review_row(row) for row in rows]
                    print(json.dumps({"runs": output}, sort_keys=True))
                    return 0
                artifact_ref = _review_artifact_ref(connection, root, args.run_id)
                if args.annotation_file:
                    try:
                        if artifact_ref is None:
                            raise AnnotationUnavailable("annotation_unavailable")
                        with root.open_artifact_ref(artifact_ref, run_id=args.run_id) as annotation_run:
                            persist_review_annotation(annotation_run, args.annotation_file)
                    except AnnotationUnavailable as exc:
                        raise _CliFailure("annotation_unavailable") from exc
                    except AnnotationError as exc:
                        raise _CliFailure("annotation_error") from exc
                    except Exception as exc:
                        raise _CliFailure("annotation_unavailable") from exc
                try:
                    if args.review_command == "complete":
                        result = complete_review(
                            connection,
                            run_id=args.run_id,
                            outcome=args.outcome,
                            artifact_root=root,
                            confirm_window_closed=args.confirm_window_closed,
                        )
                        output = _sanitize_review_transition(result, artifact_ref, outcome=args.outcome)
                    else:
                        result = retry_review(
                            connection,
                            run_id=args.run_id,
                            artifact_root=root,
                            confirm_window_closed=args.confirm_window_closed,
                        )
                        output = _sanitize_review_transition(result, artifact_ref, outcome="retry")
                except Exception as exc:
                    raise _CliFailure(_review_failure_code(exc)) from exc
                print(json.dumps(output, sort_keys=True))
                return 0
            finally:
                if connection is not None:
                    _close_database(connection)
    except _CliFailure:
        raise
    except Exception as exc:
        raise _CliFailure("artifact_root_error") from exc


def _add_source_profile_argument(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--source-profile",
        "--profile",
        dest="source_profile",
        choices=PROFILE_NAMES,
        default="new_grad_cs",
        help="TheirStack/source filter profile",
    )



def _add_theirstack_ats_argument(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--ats",
        choices=ATS_FILTER_NAMES,
        default="auto",
        help="TheirStack ATS filter; auto preserves legacy unfiltered ingestion (preview is always unfiltered)",
    )

def build_job_scrape_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="job-scrape", description="Pull filtered source jobs into the local backlog")
    parser.add_argument("--version", action="store_true", help="print package version")
    _add_theirstack_ats_argument(parser)
    parser.add_argument("--db", default=str(DEFAULT_DB), help="SQLite database path")
    _add_source_profile_argument(parser)
    parser.add_argument("--count", type=int, default=1, help="maximum jobs to fetch, 1-100")
    parser.add_argument("--paid-fetch", action="store_true", help="confirm this run may consume TheirStack credits")
    return parser


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="jobs-assistant", description="Minimal local job backlog ingestion assistant")
    parser.add_argument("--version", action="store_true", help="print package version")
    parser.add_argument("--db", default=str(DEFAULT_DB), help="SQLite database path")
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("init-db", help="initialize the SQLite database")

    import_feed = sub.add_parser("import-feed", help="import normalized jobs from JSON file or GET /v1/jobs feed")
    import_feed.add_argument("--json-file", help="import jobs from a JSON file containing a list, jobs[], or data[]")
    import_feed.add_argument("--base-url", help="source feed base URL; defaults to JOB_SOURCE_BASE_URL when unset")
    import_feed.add_argument(
        "--source",
        default="job_source",
        help=f"exact source value to store, non-empty and at most {MAX_BACKLOG_SOURCE_CHARS} characters",
    )

    preview = sub.add_parser("theirstack-preview", help="preview filtered TheirStack match count without persisting jobs")
    backlog_list = sub.add_parser("backlog-list", help="inspect backlog jobs without claiming or mutating them")
    backlog_list.add_argument(
        "--db",
        default=argparse.SUPPRESS,
        help="SQLite database path (also accepted as the global option before the command)",
    )

    backlog_list.add_argument(
        "--status",
        choices=tuple(sorted(_BACKLOG_STATUSES)),
        default="queued",
        help="backlog status to list",
    )
    backlog_list.add_argument(
        "--source",
        help=f"exact source value to list, non-empty and at most {MAX_BACKLOG_SOURCE_CHARS} characters",
    )
    backlog_list.add_argument(
        "--offset",
        type=int,
        default=argparse.SUPPRESS,
        help=f"number of matching jobs to skip, 0-{MAX_BACKLOG_OFFSET}",
    )
    backlog_list.add_argument("--limit", type=int, default=25, help="maximum jobs to list, 1-100")

    backlog_show = sub.add_parser(
        "backlog-show",
        help="show one backlog job without claiming or mutating it",
    )
    backlog_show.add_argument(
        "--db",
        default=argparse.SUPPRESS,
        help="SQLite database path (also accepted as the global option before the command)",
    )
    backlog_show.add_argument("job_id", type=int, metavar="JOB_ID", help="positive backlog job ID to show")

    backlog_archive = sub.add_parser(
        "backlog-archive",
        help="archive explicitly selected queued jobs without deleting them",
    )
    backlog_archive.add_argument(
        "--db",
        default=argparse.SUPPRESS,
        help="SQLite database path (also accepted as the global option before the command)",
    )
    backlog_archive.add_argument(
        "job_ids",
        nargs="*",
        type=int,
        metavar="JOB_ID",
        help=f"positive queued job IDs to archive, at most {MAX_ARCHIVE_JOB_IDS}",
    )
    backlog_archive.add_argument(
        "--confirm",
        action="store_true",
        help="confirm this explicit queued-only archive",
    )
    _add_source_profile_argument(preview)
    _add_theirstack_ats_argument(preview)

    sync = sub.add_parser("theirstack-sync", help="pull filtered TheirStack jobs into the backlog")
    _add_source_profile_argument(sync)
    _add_theirstack_ats_argument(sync)
    sync.add_argument("--limit", type=int, default=25, help="maximum jobs to fetch, 1-100")
    sync.add_argument("--paid-fetch", action="store_true", help="confirm this run may consume TheirStack credits")

    autofill = sub.add_parser(
        "autofill",
        help="open queued jobs and fill safe inferred fields with no-final-submit guard",
        description="Guarded application draft workflow: fill safe fields only and enforce no-final-submit.",
    )
    autofill.add_argument("--limit", type=int, default=1, help="maximum queued jobs to process, 1-10")
    autofill.add_argument("--resume-file", default=str(DEFAULT_RESUME_FILE), help="one explicit resume file staged for safe uploads")
    autofill.add_argument(
        "--application-profile-json",
        "--profile-json",
        dest="application_profile_json",
        help="explicit application/profile facts JSON; values here are never inferred from resume text",
    )
    autofill.add_argument("--applicant-description-file", help="optional applicant description used only for the guarded resolver")
    autofill.add_argument("--artifact-root", default=str(DEFAULT_ARTIFACT_ROOT), help="private root for per-run evidence and review manifests")
    autofill.add_argument(
        "--ats",
        choices=("auto", *SUPPORTED_ATS),
        default="auto",
        help="ATS route policy (auto selects a validated supported adapter)",
    )
    autofill.add_argument("--application-profile-preset", help="named profile preset")
    autofill.add_argument("--application-profile-dir", help="directory containing named profile presets")
    autofill.add_argument("--application-preferences", help="validated safe application preferences JSON")
    pref = sub.add_parser("application-preferences", help="atomically edit safe application preferences")
    pref_sub = pref.add_subparsers(dest="preferences_command", required=True)
    pref_init = pref_sub.add_parser("init")
    pref_init.add_argument("path")
    pref_show = pref_sub.add_parser("show")
    pref_show.add_argument("path")
    def _pref_matcher_args(parser: argparse.ArgumentParser, *, value: bool = False) -> None:
        parser.add_argument("path")
        parser.add_argument("--ats", choices=("*", *SUPPORTED_ATS), required=True)
        parser.add_argument("--kind", required=True)
        group = parser.add_mutually_exclusive_group(required=True)
        group.add_argument("--name")
        group.add_argument("--label")
        if value:
            parser.add_argument("--value", required=True)
    for command, needs_value in (("set-mapping", True), ("remove-mapping", False), ("set-opt-out", False), ("remove-opt-out", False), ("set-review-order", False), ("remove-review-order", False)):
        _pref_matcher_args(pref_sub.add_parser(command), value=needs_value)
    autofill.add_argument("--headed", action="store_true", help="leave a guarded review window open for human review; no-final-submit enforced")
    review = sub.add_parser("autofill-review", help="review guarded autofill handoffs")
    review.add_argument("--artifact-root", default=str(DEFAULT_ARTIFACT_ROOT), help="private root for per-run evidence and review manifests")
    review_sub = review.add_subparsers(dest="review_command", required=True)
    review_list = review_sub.add_parser("list", help="list latest unreviewed application runs")
    review_list.add_argument("--limit", type=int, default=10, help="maximum runs to list, 1-100")
    review_complete = review_sub.add_parser("complete", help="record the human review outcome")
    review_complete.add_argument("--run-id", type=int, required=True)
    review_complete.add_argument("--outcome", choices=("submitted", "skipped"), required=True)
    review_complete.add_argument("--confirm-window-closed", action="store_true")
    review_complete.add_argument("--annotation-file", required=False)
    review_retry = review_sub.add_parser("retry", help="queue an explicit retry for a reviewed run")
    review_retry.add_argument("--run-id", type=int, required=True)
    review_retry.add_argument("--confirm-window-closed", action="store_true")
    review_retry.add_argument("--annotation-file", required=False)
    return parser



def _theirstack_client(*, paid_fetch: bool) -> TheirStackClient:
    api_key = os.environ.get("THEIRSTACK_API_KEY")
    if not api_key:
        raise ValueError("THEIRSTACK_API_KEY is required")
    # A paid request may consume credits even if its response is ambiguous, so
    # paid syncs never replay. Credit-safe previews retain bounded retries.
    max_retries = 0 if paid_fetch else 2
    return TheirStackClient(
        api_key,
        enable_paid_fetch=paid_fetch,
        base_url=os.environ.get("THEIRSTACK_BASE_URL", "https://api.theirstack.com"),
        max_retries=max_retries,
    )
def _paid_fetch_allowed(args: argparse.Namespace) -> bool:
    return bool(args.paid_fetch or os.environ.get("THEIRSTACK_ENABLE_PAID_FETCH", "").lower() in {"1", "true", "yes"})

def run_theirstack_paid_sync(
    conn,
    *,
    source_profile: ProfileName,
    limit: int,
    mode: str,
    ats_filter: ATSFilter = "auto",
) -> dict[str, int | str | bool]:
    selected_filter = validate_ats_filter_name(ats_filter)
    client = _theirstack_client(paid_fetch=True)
    checkpoint_profile = checkpoint_profile_key(source_profile, selected_filter)
    run_id = record_sync_run(conn, "theirstack", mode, profile=checkpoint_profile)
    try:
        if selected_filter == "auto":
            checkpoint = latest_sync_checkpoint(conn, source="theirstack", profile=checkpoint_profile)
            payload = build_paid_fetch_payload(
                source_profile,
                limit=limit,
                discovered_at_gte=checkpoint,
                ats_filter=selected_filter,
            )
        else:
            # Pinned ATS syncs intentionally re-fetch the latest raw window on
            # every invocation.  Filtering a limited page after fetch can
            # reject newer rows, so a wall-clock checkpoint would make
            # unconsumed eligible rows unreachable.
            payload = build_paid_fetch_payload(source_profile, limit=limit, ats_filter=selected_filter)
        response = client.search_jobs(payload)
        stats: dict[str, int] = {}
        seen, inserted, updated = sync_theirstack_response(
            conn,
            response,
            paid_fetch_enabled=True,
            ats_filter=selected_filter,
            stats=stats,
        )
        finished_at = utc_now()
        sync_fields: dict[str, object] = {
            "finished_at": finished_at,
            "success": True,
            "jobs_seen": seen,
            "jobs_returned": stats["fetched"],
            "jobs_inserted": inserted,
            "jobs_updated": updated,
        }
        if selected_filter == "auto":
            # Preserve the historical incremental checkpoint for auto mode.
            sync_fields["checkpoint"] = finished_at
        update_sync_run(conn, run_id, **sync_fields)
        result: dict[str, int | str | bool] = {
            "source_profile": source_profile,
            "count": limit,
            "seen": seen,
            "inserted": inserted,
            "updated": updated,
        }
        if selected_filter != "auto":
            result.update(
                {
                    "ats_filter": selected_filter,
                    "ats_filter_applied": True,
                    "checkpoint_advanced": False,
                    "fetched": stats["fetched"],
                    "ats_eligible": stats["ats_eligible"],
                    "ats_rejected": stats["ats_rejected"],
                }
            )
        return result
    except TheirStackError:
        update_sync_run(
            conn,
            run_id,
            finished_at=utc_now(),
            success=False,
            error="theirstack request failed",
        )
        raise
    except Exception as exc:
        update_sync_run(conn, run_id, finished_at=utc_now(), success=False, error=str(exc))
        raise


def job_scrape_main(argv: list[str] | None = None) -> int:
    parser = build_job_scrape_parser()
    args = parser.parse_args(argv)
    if args.version:
        print(__version__)
        return 0
    if not (1 <= args.count <= 100):
        parser.error("job-scrape --count must be between 1 and 100")
    if not _paid_fetch_allowed(args):
        parser.error("job-scrape requires --paid-fetch or THEIRSTACK_ENABLE_PAID_FETCH=true")
    connection = None
    try:
        try:
            connection = connect(args.db)
            init_db(connection)
        except (PermissionError, OSError) as exc:
            return _emit_failure("database_privacy_error")
        except sqlite3.DatabaseError:
            return _emit_failure("database_error")
        try:
            result = run_theirstack_paid_sync(
                connection,
                source_profile=args.source_profile,
                limit=args.count,
                mode="job_scrape",
                ats_filter=args.ats,
            )
            print(json.dumps(result, sort_keys=True))
            return 0
        except TheirStackError:
            return _emit_failure("theirstack_error")
        except (ValueError, TypeError):
            return _emit_failure("invalid_input")
        except sqlite3.DatabaseError:
            return _emit_failure("database_error")
    finally:
        if connection is not None:
            _close_database(connection)

def _validate_autofill_args(parser: argparse.ArgumentParser, args: argparse.Namespace) -> None:
    """Reject autofill controls before opening artifacts, a browser, or SQLite."""
    if type(args.limit) is not int or not 1 <= args.limit <= 10:
        parser.error("autofill --limit must be between 1 and 10")
    if args.ats not in {"auto", *SUPPORTED_ATS}:
        parser.error("autofill --ats is unsupported")
    if args.application_profile_json is not None and args.application_profile_preset is not None:
        parser.error("--application-profile-json conflicts with --application-profile-preset")
    if args.application_profile_dir is not None and args.application_profile_preset is None:
        parser.error("--application-profile-dir requires --application-profile-preset")
    if args.application_profile_preset is not None and args.application_profile_dir is None:
        parser.error("--application-profile-preset requires --application-profile-dir")


def _validate_autofill_preclaim_inputs(args: argparse.Namespace) -> None:
    """Validate every user-controlled autofill input before opening SQLite."""
    try:
        with load_resume_context(args.resume_file):
            pass
        if args.application_profile_preset:
            preset = load_application_profile_preset(args.application_profile_dir, args.application_profile_preset, cwd=Path.cwd())
            profile = preset.profile
        else:
            profile = load_application_profile(args.application_profile_json)
        load_application_preferences(args.application_preferences, cwd=Path.cwd())
        load_applicant_description(args.applicant_description_file, profile)
    except Exception as exc:
        raise _CliFailure("invalid_input") from exc
    try:
        with ArtifactRoot.open(args.artifact_root, cwd=Path.cwd()):
            pass
    except Exception as exc:
        raise _CliFailure("artifact_root_error") from exc


def _preference_edit_path(raw: str, *, allow_missing: bool = False) -> Path:
    if type(raw) is not str or not raw or "\x00" in raw:
        raise PreferenceValidationError("invalid preferences path")
    root = Path.cwd().absolute()
    try:
        root_stat = root.stat()
    except OSError as exc:
        raise PreferenceValidationError("preferences cwd is unavailable") from exc
    if not stat.S_ISDIR(root_stat.st_mode) or root_stat.st_uid != os.geteuid():
        raise PreferenceValidationError("preferences cwd is not a private directory")
    path = Path(raw)
    if not path.is_absolute():
        path = root / path
    path = path.absolute()
    try:
        path.relative_to(root)
    except ValueError as exc:
        raise PreferenceValidationError("preferences path escapes cwd") from exc
    current = root
    for part in path.relative_to(root).parts[:-1]:
        current /= part
        try:
            component = current.stat()
        except OSError as exc:
            raise PreferenceValidationError("preferences parent directory is unavailable") from exc
        if current.is_symlink() or not stat.S_ISDIR(component.st_mode):
            raise PreferenceValidationError("preferences path contains a symlink")
        if component.st_uid != os.geteuid():
            raise PreferenceValidationError("preferences parent directory must be owned by effective user")
    if path.is_symlink():
        raise PreferenceValidationError("preferences file must be regular")
    if path.exists():
        if not path.is_file():
            raise PreferenceValidationError("preferences file must be regular")
        if path.stat().st_uid != os.geteuid():
            raise PreferenceValidationError("preferences file must be owned by effective user")
    elif not allow_missing:
        raise PreferenceValidationError("preferences file does not exist")
    return path


def _preferences_document(value: ApplicationPreferences) -> dict[str, object]:
    def matcher(item: PreferenceMatcher) -> dict[str, object]:
        return {"ats": item.ats, "name": item.name, "label": item.label, "kind": item.kind}
    return {
        "schema_version": APPLICATION_PREFERENCES_SCHEMA_VERSION,
        "mappings": [
            {**matcher(item), "value": item.value}
            for item in value.mappings
        ],
        "opt_outs": [matcher(item) for item in value.opt_outs],
        "review_order": [matcher(item) for item in value.review_order],
    }


_PREFERENCES_DIRECTORY_FLAGS = (
    os.O_RDONLY
    | getattr(os, "O_DIRECTORY", 0)
    | getattr(os, "O_NOFOLLOW", 0)
    | getattr(os, "O_CLOEXEC", 0)
)
_PREFERENCES_TEMP_FLAGS = (
    os.O_WRONLY
    | os.O_CREAT
    | os.O_EXCL
    | getattr(os, "O_NOFOLLOW", 0)
    | getattr(os, "O_CLOEXEC", 0)
)


def _open_preferences_parent(path: Path) -> int:
    components = tuple(part for part in path.parent.parts if part not in (path.parent.anchor, ""))
    if not components:
        raise PreferenceValidationError("preferences parent directory is unavailable")
    expected: list[os.stat_result] = []
    current = Path(path.parent.anchor or "/")
    for component in components:
        current /= component
        try:
            expected.append(os.lstat(current))
        except OSError as exc:
            raise PreferenceValidationError("preferences parent directory is unavailable") from exc
    try:
        parent_fd = os.open(path.parent.anchor or "/", _PREFERENCES_DIRECTORY_FLAGS)
    except OSError as exc:
        raise PreferenceValidationError("preferences parent directory is unavailable") from exc
    try:
        for index, component in enumerate(components):
            try:
                child_fd = os.open(component, _PREFERENCES_DIRECTORY_FLAGS, dir_fd=parent_fd)
            except OSError as exc:
                raise PreferenceValidationError("preferences parent directory is unavailable") from exc
            try:
                actual = os.fstat(child_fd)
                expected_stat = expected[index]
                if (
                    not stat.S_ISDIR(actual.st_mode)
                    or stat.S_ISLNK(expected_stat.st_mode)
                    or actual.st_dev != expected_stat.st_dev
                    or actual.st_ino != expected_stat.st_ino
                ):
                    raise PreferenceValidationError("preferences parent directory changed while opening")
                mode = stat.S_IMODE(actual.st_mode)
                if mode & (stat.S_IWGRP | stat.S_IWOTH):
                    if not (index < len(components) - 1 and actual.st_uid == 0 and mode & stat.S_ISVTX):
                        raise PreferenceValidationError("preferences parent directory is group/world writable")
                if index == len(components) - 1 and actual.st_uid != os.geteuid():
                    raise PreferenceValidationError("preferences parent directory must be owned by effective user")
            except Exception:
                os.close(child_fd)
                raise
            os.close(parent_fd)
            parent_fd = child_fd
        return parent_fd
    except Exception:
        os.close(parent_fd)
        raise


def _atomic_preferences_write(path: Path, value: ApplicationPreferences, *, replace_existing: bool = True) -> str:
    payload = json.dumps(_preferences_document(value), sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()
    digest = hashlib.sha256(payload).hexdigest()
    parent_fd = _open_preferences_parent(path)
    temp_name: str | None = None
    try:
        if not replace_existing:
            try:
                os.stat(path.name, dir_fd=parent_fd, follow_symlinks=False)
            except FileNotFoundError:
                pass
            else:
                raise PreferenceValidationError("preferences file already exists")
        fd = -1
        for _ in range(16):
            candidate = f".{path.name}.{secrets.token_hex(8)}"
            try:
                fd = os.open(candidate, _PREFERENCES_TEMP_FLAGS, 0o600, dir_fd=parent_fd)
            except FileExistsError:
                continue
            temp_name = candidate
            break
        if fd < 0 or temp_name is None:
            raise PreferenceValidationError("unable to create temporary preferences file")
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
        except Exception:
            fd = -1
            raise
        fd = -1
        os.replace(temp_name, path.name, src_dir_fd=parent_fd, dst_dir_fd=parent_fd)
        temp_name = None
        os.fsync(parent_fd)
        return digest
    finally:
        if temp_name is not None:
            try:
                os.unlink(temp_name, dir_fd=parent_fd)
            except FileNotFoundError:
                pass
        os.close(parent_fd)


def _preference_matcher_from_args(args: argparse.Namespace) -> PreferenceMatcher:
    return PreferenceMatcher(args.ats, args.name, args.label, args.kind)


def _run_application_preferences(args: argparse.Namespace) -> int:
    path = _preference_edit_path(args.path, allow_missing=args.preferences_command == "init")
    if args.preferences_command == "init":
        digest = _atomic_preferences_write(
            path,
            ApplicationPreferences(APPLICATION_PREFERENCES_SCHEMA_VERSION, (), (), ()),
            replace_existing=False,
        )
        print(json.dumps({"path": str(path), "schema_version": APPLICATION_PREFERENCES_SCHEMA_VERSION}, sort_keys=True))
        return 0
    prefs = load_application_preferences(path, cwd=Path.cwd())
    if args.preferences_command == "show":
        digest = prefs.source_sha256 or ""
        output = {
            "schema_version": prefs.schema_version,
            "sha256": digest,
            "mappings": [
                {"ats": item.ats, "name": item.name, "label": item.label, "kind": item.kind,
                 "value_length": len(item.value) if isinstance(item.value, str) else None,
                 "value_hash": hashlib.sha256(str(item.value).encode()).hexdigest()}
                for item in prefs.mappings
            ],
            "opt_outs": [_preferences_document(prefs)["opt_outs"][index] for index in range(len(prefs.opt_outs))],
            "review_order": [_preferences_document(prefs)["review_order"][index] for index in range(len(prefs.review_order))],
        }
        print(json.dumps(output, sort_keys=True))
        return 0
    matcher = _preference_matcher_from_args(args)
    mappings = list(prefs.mappings)
    opt_outs = list(prefs.opt_outs)
    review_order = list(prefs.review_order)
    if args.preferences_command == "set-mapping":
        value: str | bool = args.value
        if args.kind in {"checkbox", "radio"}:
            if args.value not in {"true", "false"}:
                raise PreferenceValidationError("checkbox/radio value must be true or false")
            value = args.value == "true"
        item = PreferenceMapping(args.ats, args.name, args.label, args.kind, value)
        mappings = [existing for existing in mappings if existing.matcher_id != item.matcher_id]
        mappings.append(item)
    elif args.preferences_command == "remove-mapping":
        mappings = [existing for existing in mappings if existing.matcher_id != matcher.matcher_id]
    elif args.preferences_command == "set-opt-out":
        item = PreferenceOptOut(args.ats, args.name, args.label, args.kind)
        opt_outs = [existing for existing in opt_outs if existing.matcher_id != item.matcher_id]
        opt_outs.append(item)
    elif args.preferences_command == "remove-opt-out":
        opt_outs = [existing for existing in opt_outs if existing.matcher_id != matcher.matcher_id]
    elif args.preferences_command == "set-review-order":
        review_order = [existing for existing in review_order if existing.matcher_id != matcher.matcher_id]
        review_order.append(matcher)
    elif args.preferences_command == "remove-review-order":
        matches = [existing for existing in review_order if existing.matcher_id == matcher.matcher_id]
        if not matches:
            raise PreferenceValidationError("review-order matcher is absent")
        if len(matches) > 1:
            raise PreferenceValidationError("review-order matcher is ambiguous")
        review_order = [existing for existing in review_order if existing.matcher_id != matcher.matcher_id]
    updated = ApplicationPreferences(APPLICATION_PREFERENCES_SCHEMA_VERSION, tuple(mappings), tuple(opt_outs), tuple(review_order))
    digest = _atomic_preferences_write(path, updated)
    print(json.dumps({"path": str(path), "sha256": digest}, sort_keys=True))
    return 0

def _run_autofill(args: argparse.Namespace) -> list[dict[str, object]]:
    try:
        artifacts = ArtifactRoot.open(args.artifact_root, cwd=Path.cwd())
    except Exception as exc:
        raise _CliFailure("artifact_root_error") from exc
    connection = None
    try:
        with artifacts:
            try:
                PuppeteerSession.preflight(headed=args.headed)
            except Exception as exc:
                raise _CliFailure("browser_preflight_error") from exc
            try:
                try:
                    connection = connect(args.db)
                    initialize_database(connection, migration_artifact_root=artifacts)
                except (PermissionError, OSError) as exc:
                    raise _CliFailure("database_privacy_error") from exc
                except Exception as exc:
                    raise _CliFailure("database_error") from exc
                try:
                    results = asyncio.run(
                        run_application_workflow(
                            connection,
                            limit=args.limit,
                            resume_file=args.resume_file,
                            application_profile_json=args.application_profile_json,
                            application_profile_preset=args.application_profile_preset,
                            application_profile_dir=args.application_profile_dir,
                            application_preferences=args.application_preferences,
                            ats=args.ats,
                            applicant_description_file=args.applicant_description_file,
                            artifact_root=args.artifact_root,
                            headed=args.headed,
                        )
                    )
                except sqlite3.DatabaseError as exc:
                    raise _CliFailure("database_error") from exc
                except Exception as exc:
                    raise _CliFailure("workflow_error") from exc
                return _sanitize_autofill_results(results)
            finally:
                if connection is not None:
                    _close_database(connection)
    except _CliFailure:
        raise
    except Exception as exc:
        raise _CliFailure("artifact_root_error") from exc


def _validate_backlog_archive_args(args: argparse.Namespace) -> None:
    """Reject archive controls before opening SQLite."""
    if not args.confirm:
        raise _CliFailure("backlog_archive_confirmation")
    job_ids = args.job_ids
    if not job_ids:
        raise _CliFailure("backlog_archive_input")
    if len(job_ids) > MAX_ARCHIVE_JOB_IDS:
        raise _CliFailure("backlog_archive_input")
    if any(type(job_id) is not int or job_id <= 0 for job_id in job_ids):
        raise _CliFailure("backlog_archive_input")
    if len(set(job_ids)) != len(job_ids):
        raise _CliFailure("backlog_archive_input")


def _run_backlog_archive(args: argparse.Namespace) -> int:
    connection = None
    try:
        try:
            connection = connect(args.db)
            init_db(connection)
        except (PermissionError, OSError) as exc:
            raise _CliFailure("database_privacy_error") from exc
        try:
            archived_ids = archive_queued_jobs(connection, args.job_ids)
        except BacklogArchiveConflictError as exc:
            raise _CliFailure("backlog_archive_conflict") from exc
        except BacklogArchiveError as exc:
            raise _CliFailure("backlog_archive_input") from exc
        print(json.dumps({"archived": list(archived_ids), "count": len(archived_ids)}, sort_keys=True))
        return 0
    finally:
        if connection is not None:
            _close_database(connection)


def _validate_import_feed_args(parser: argparse.ArgumentParser, args: argparse.Namespace) -> None:
    """Reject import-feed source controls before opening SQLite."""
    if (
        type(args.source) is not str
        or not args.source.strip()
        or len(args.source) > MAX_BACKLOG_SOURCE_CHARS
    ):
        parser.error(
            f"import-feed --source must be a non-empty string of at most {MAX_BACKLOG_SOURCE_CHARS} characters"
        )

def _run_import_feed(connection, args: argparse.Namespace, preloaded_jobs: list[dict[str, object]] | None) -> int:
    """Run one validated generic feed import and persist a redacted sync audit."""
    mode = "json_file" if args.json_file else "http"
    run_id = record_sync_run(connection, args.source, mode)
    returned = 0
    seen = 0
    transaction_started = False

    def finish_failure(error: str) -> None:
        try:
            update_sync_run(
                connection,
                run_id,
                finished_at=utc_now(),
                success=False,
                jobs_seen=seen,
                jobs_returned=returned,
                jobs_inserted=0,
                jobs_updated=0,
                error=error,
            )
        except Exception:
            # Keep the import failure and its public mapping primary if the
            # best-effort failure audit cannot be persisted.
            pass

    def rollback_import() -> bool:
        nonlocal transaction_started
        if not transaction_started:
            return True
        try:
            connection.rollback()
        except Exception:
            return False
        transaction_started = False
        return True

    try:
        if args.json_file:
            raw_jobs = preloaded_jobs
        else:
            try:
                base_url = args.base_url or os.environ.get("JOB_SOURCE_BASE_URL")
                raw_jobs = fetch_source_jobs(base_url, api_key=os.environ.get("JOB_SOURCE_API_KEY"))
            except (RecursionError, TypeError, ValueError) as exc:
                finish_failure("source response rejected")
                raise _CliFailure("invalid_input") from exc
            except httpx.HTTPError as exc:
                finish_failure("source request failed")
                raise _CliFailure("invalid_input") from exc
            except Exception as exc:
                finish_failure("source import failed")
                raise _CliFailure("workflow_error") from exc

        connection.execute("BEGIN")
        transaction_started = True
        if isinstance(raw_jobs, (list, tuple)):
            returned = len(raw_jobs)
            seen = returned
        seen, inserted, updated = import_source_jobs(connection, raw_jobs, source=args.source)
        update_sync_run(
            connection,
            run_id,
            finished_at=utc_now(),
            success=True,
            jobs_seen=seen,
            jobs_returned=seen,
            jobs_inserted=inserted,
            jobs_updated=updated,
            error=None,
        )
        transaction_started = False
        print(json.dumps({"seen": seen, "inserted": inserted, "updated": updated}, sort_keys=True))
        return 0
    except _CliFailure:
        rollback_import()
        raise
    except (RecursionError, TypeError, ValueError) as exc:
        if rollback_import():
            finish_failure("source payload rejected")
        raise _CliFailure("invalid_input") from exc
    except sqlite3.DatabaseError:
        if rollback_import():
            finish_failure("database operation failed")
        raise
    except Exception as exc:
        if rollback_import():
            finish_failure("source import failed")
        raise _CliFailure("workflow_error") from exc

def _validate_backlog_list_args(parser: argparse.ArgumentParser, args: argparse.Namespace) -> None:
    """Reject backlog-list controls before opening SQLite."""
    if args.status not in _BACKLOG_STATUSES:
        parser.error("backlog-list --status is unsupported")
    if type(args.limit) is not int or not 1 <= args.limit <= MAX_BACKLOG_LIMIT:
        parser.error(f"backlog-list --limit must be between 1 and {MAX_BACKLOG_LIMIT}")
    offset = getattr(args, "offset", 0)
    if type(offset) is not int or not 0 <= offset <= MAX_BACKLOG_OFFSET:
        parser.error(f"backlog-list --offset must be between 0 and {MAX_BACKLOG_OFFSET}")
    if args.source is not None and (
        type(args.source) is not str
        or not args.source.strip()
        or len(args.source) > MAX_BACKLOG_SOURCE_CHARS
    ):
        parser.error(
            f"backlog-list --source must be a non-empty string of at most {MAX_BACKLOG_SOURCE_CHARS} characters"
        )


def _validate_backlog_show_args(parser: argparse.ArgumentParser, args: argparse.Namespace) -> None:
    """Reject backlog-show controls before opening SQLite."""
    if type(args.job_id) is not int or args.job_id <= 0:
        parser.error("backlog-show JOB_ID must be a positive integer")


def _run_backlog_list(args: argparse.Namespace) -> int:
    connection = None
    try:
        try:
            connection = connect_read_only(args.db)
        except FileNotFoundError as exc:
            raise _CliFailure("database_error") from exc
        except (PermissionError, OSError) as exc:
            raise _CliFailure("database_privacy_error") from exc
        offset = getattr(args, "offset", 0)
        jobs, counts = list_backlog_jobs(
            connection,
            status=args.status,
            source=args.source,
            limit=args.limit,
            offset=offset,
        )
        output: dict[str, object] = {
            "jobs": jobs,
            "limit": args.limit,
            "pending": counts["pending"],
            "status": args.status,
            "total": counts["total"],
        }
        if hasattr(args, "offset"):
            output["offset"] = offset
        print(json.dumps(output, sort_keys=True))
        return 0
    finally:
        if connection is not None:
            _close_database(connection)


def _run_backlog_show(args: argparse.Namespace) -> int:
    connection = None
    try:
        try:
            connection = connect_read_only(args.db)
        except FileNotFoundError as exc:
            raise _CliFailure("database_error") from exc
        except (PermissionError, OSError) as exc:
            raise _CliFailure("database_privacy_error") from exc
        try:
            row = connection.execute(
                """
                SELECT id, source, source_job_id, canonical_url, title, company,
                       location, remote, posted_at, discovered_at, status,
                       substr(description, 1, ?) AS description
                FROM jobs
                WHERE id = ?
                """,
                (MAX_BACKLOG_DESCRIPTION_CHARS + 1, args.job_id),
            ).fetchone()
        except OverflowError as exc:
            raise _CliFailure("database_error") from exc
        if row is None or type(row["status"]) is not str or row["status"] not in _BACKLOG_STATUSES:
            raise _CliFailure("database_error")
        job = {field: row[field] for field in _BACKLOG_PUBLIC_FIELDS}
        job["description"] = _safe_backlog_description(row["description"])
        print(json.dumps(job, sort_keys=True))
        return 0
    finally:
        if connection is not None:
            _close_database(connection)


def _run_database_command(args: argparse.Namespace, parser: argparse.ArgumentParser) -> int:
    connection = None
    preloaded_jobs = None
    try:
        if args.command == "backlog-list":
            _validate_backlog_list_args(parser, args)
            return _run_backlog_list(args)
        if args.command == "backlog-show":
            _validate_backlog_show_args(parser, args)
            return _run_backlog_show(args)
        if args.command == "backlog-archive":
            _validate_backlog_archive_args(args)
            return _run_backlog_archive(args)
        if args.command == "import-feed":
            _validate_import_feed_args(parser, args)
            if args.json_file:
                try:
                    preloaded_jobs = extract_source_jobs(json.loads(Path(args.json_file).read_text()))
                except (OSError, RecursionError, TypeError, ValueError) as exc:
                    raise _CliFailure("invalid_input") from exc
            elif not (args.base_url or os.environ.get("JOB_SOURCE_BASE_URL")):
                parser.error("import-feed requires --json-file, --base-url, or JOB_SOURCE_BASE_URL")
        try:
            connection = connect(args.db)
            init_db(connection)
        except (PermissionError, OSError) as exc:
            raise _CliFailure("database_privacy_error") from exc
        if args.command == "init-db":
            print(f"initialized {args.db}")
            return 0
        if args.command == "import-feed":
            return _run_import_feed(connection, args, preloaded_jobs)
        if args.command == "theirstack-preview":
            client = _theirstack_client(paid_fetch=False)
            payload = build_preview_payload(args.source_profile)
            response = client.search_jobs(payload)
            total = response_total_results(response)
            output: dict[str, object] = {
                "profile": args.source_profile,
                "total_results": total,
                "credit_safe": True,
            }
            if args.ats != "auto":
                output.update(
                    {
                        "ats_filter": args.ats,
                        "ats_filter_applied": False,
                        "ats_filter_reason": "credit-free blurred preview has no application URLs; total is unfiltered",
                        "total_results_unfiltered": total,
                    }
                )
            print(json.dumps(output, sort_keys=True))
            return 0
        if args.command == "theirstack-sync":
            paid_fetch = _paid_fetch_allowed(args)
            if not paid_fetch:
                parser.error("theirstack-sync requires --paid-fetch or THEIRSTACK_ENABLE_PAID_FETCH=true")
            result = run_theirstack_paid_sync(
                connection,
                source_profile=args.source_profile,
                limit=args.limit,
                mode="paid_fetch",
                ats_filter=args.ats,
            )
            print(json.dumps(result, sort_keys=True))
            return 0
        parser.error(f"unknown command: {args.command}")
        return 2
    except TheirStackError as exc:
        raise _CliFailure("theirstack_error") from exc
    except (ValueError, TypeError) as exc:
        raise _CliFailure("invalid_input") from exc
    except sqlite3.DatabaseError as exc:
        raise _CliFailure("database_error") from exc
    finally:
        if connection is not None:
            _close_database(connection)


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.version:
        print(__version__)
        return 0
    if args.command is None:
        parser.print_help()
        return 0
    if args.command == "autofill-review":
        if args.review_command == "list" and (type(args.limit) is not int or not 1 <= args.limit <= 100):
            parser.error("autofill-review list --limit must be between 1 and 100")
        if args.review_command in {"complete", "retry"} and (type(args.run_id) is not int or args.run_id <= 0):
            parser.error("autofill-review run ID must be positive")
        try:
            return _run_review(args)
        except Exception as exc:
            return _emit_failure(_review_failure_code(exc))
    if args.command == "application-preferences":
        try:
            return _run_application_preferences(args)
        except Exception as exc:
            return _emit_failure("invalid_input" if isinstance(exc, (ValueError, TypeError, OSError)) else "workflow_error")
    if args.command == "autofill":
        # Validate all preclaim inputs before ArtifactRoot, browser, and DB work.
        _validate_autofill_args(parser, args)
        try:
            _validate_autofill_preclaim_inputs(args)
            results = _run_autofill(args)
        except Exception as exc:
            return _emit_failure(_runtime_failure_code(exc))
        print(json.dumps({"results": results}, sort_keys=True))
        return 0
    try:
        return _run_database_command(args, parser)
    except _CliFailure as exc:
        return _emit_failure(exc.code)
    except sqlite3.DatabaseError:
        return _emit_failure("database_error")
if __name__ == "__main__":
    raise SystemExit(main())
