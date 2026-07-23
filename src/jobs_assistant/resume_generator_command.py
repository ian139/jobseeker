from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from pathlib import Path
from typing import NoReturn

from .db import connect_read_only
from .resume_generator import GeneratedResume, ResumeJob, generate_resume

DEFAULT_DB = Path(os.environ.get("DATABASE_URL", "data/jobs.sqlite3"))
DEFAULT_PROFILE = Path("resume/generator/profile.json")
DEFAULT_TEMPLATE = Path("resume/generator/Resume.tex")
DEFAULT_SKILL = Path("resume/generator/SKILL.md")
DEFAULT_OUTPUT_ROOT = Path("data/generated-resumes-generator")
DEFAULT_LIMIT = 10
MIN_LIMIT = 1
MAX_LIMIT = 100


class _ArgumentParserFailure(Exception):
    """Quiet parse failure converted to the CLI's fixed JSON error."""


class _QuietArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> NoReturn:
        raise _ArgumentParserFailure(message)

_RESUME_ERROR_MESSAGES: dict[str, str] = {
    "invalid_input": "resume generation input was rejected",
    "database_error": "database operation failed",
    "generation_error": "resume generation failed",
    "job_not_found": "requested job was not found or not queued",
    "no_queued_jobs": "no queued jobs with descriptions found",
}


def _emit_failure(code: str) -> int:
    safe_code = code if code in _RESUME_ERROR_MESSAGES else "generation_error"
    print(
        json.dumps(
            {"error": {"code": safe_code, "message": _RESUME_ERROR_MESSAGES[safe_code]}},
            sort_keys=True,
        ),
        file=sys.stderr,
    )
    return 1


def _select_jobs(
    conn: sqlite3.Connection,
    limit: int,
    job_ids: tuple[int, ...] | None,
) -> tuple[list[sqlite3.Row] | None, str | None]:
    """Return (rows, None) on success or (None, error_code) on failure.

    When *job_ids* is given, every requested ID must exist, be queued, and
    have a non-empty description — otherwise the call fails before any
    compilation.
    """
    if job_ids:
        placeholders = ",".join("?" * len(job_ids))
        rows = conn.execute(
            f"""
            SELECT id, title, company, description, location, posted_at
            FROM jobs
            WHERE id IN ({placeholders})
              AND status = 'queued'
              AND description IS NOT NULL
              AND TRIM(description, char(9) || char(10) || char(11) || char(12) || char(13) || ' ') != ''
            ORDER BY posted_at DESC NULLS LAST, first_seen_at ASC, id ASC
            """,
            tuple(job_ids),
        ).fetchall()

        found: set[int] = {row["id"] for row in rows}
        if len(found) != len(job_ids):
            return None, "job_not_found"

        return rows, None

    rows = conn.execute(
        """
        SELECT id, title, company, description, location, posted_at
        FROM jobs
        WHERE status = 'queued'
          AND description IS NOT NULL
          AND TRIM(description, char(9) || char(10) || char(11) || char(12) || char(13) || ' ') != ''
        ORDER BY posted_at DESC NULLS LAST, first_seen_at ASC, id ASC
        LIMIT ?
        """,
        (limit,),
    ).fetchall()

    if not rows:
        return None, "no_queued_jobs"

    return rows, None


def build_parser() -> argparse.ArgumentParser:
    parser = _QuietArgumentParser(
        prog="resume-generate",
        description="Generate LaTeX/PDF resumes for queued backlog jobs",
    )
    parser.add_argument(
        "--db",
        type=Path,
        default=DEFAULT_DB,
        help="SQLite database path (default: DATABASE_URL or data/jobs.sqlite3)",
    )
    parser.add_argument(
        "--profile",
        type=Path,
        default=DEFAULT_PROFILE,
        help="profile JSON path (default: resume/generator/profile.json)",
    )
    parser.add_argument(
        "--template",
        type=Path,
        default=DEFAULT_TEMPLATE,
        help="LaTeX template path (default: resume/generator/Resume.tex)",
    )
    parser.add_argument(
        "--skill",
        type=Path,
        default=DEFAULT_SKILL,
        help="resume generation skill path (default: resume/generator/SKILL.md)",
    )
    parser.add_argument(
        "--output-root",
        type=Path,
        default=DEFAULT_OUTPUT_ROOT,
        help="output root directory (default: data/generated-resumes)",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=DEFAULT_LIMIT,
        help=(
            f"max jobs to generate "
            f"(default: {DEFAULT_LIMIT}, range: {MIN_LIMIT}–{MAX_LIMIT})"
        ),
    )
    parser.add_argument(
        "--job-id",
        type=int,
        action="append",
        dest="job_ids",
        default=None,
        help="specific job ID to generate (repeatable; overrides default selection)",
    )
    parser.add_argument(
        "--compiler",
        type=str,
        default=None,
        help="LaTeX compiler override (default: auto-detect tectonic then pdflatex)",
    )
    return parser


def _validate_args(parser: argparse.ArgumentParser, args: argparse.Namespace) -> str | None:
    """Return error_code on failure, None on success."""
    del parser
    if type(args.limit) is not int or not (MIN_LIMIT <= args.limit <= MAX_LIMIT):
        return "invalid_input"
    if args.job_ids is not None:
        for jid in args.job_ids:
            if type(jid) is not int or jid <= 0:
                return "invalid_input"
        if len(args.job_ids) > MAX_LIMIT:
            return "invalid_input"
        if len(set(args.job_ids)) != len(args.job_ids):
            return "invalid_input"
    skill_path = args.skill if args.skill is not None else args.template.with_name("SKILL.md")
    for path in (args.db, args.profile, args.template, skill_path):
        try:
            if not path.exists() or not path.is_file() or path.is_symlink():
                return "invalid_input"
        except OSError:
            return "invalid_input"
    return None


def _result_row(gen: GeneratedResume) -> dict[str, object]:
    return {
        "job_id": gen.job_id,
        "artifact_ref": gen.artifact_ref,
        "pdf": f"{gen.artifact_ref}/resume.pdf",
        "pages": gen.pages,
        "field": gen.field,
        "graduation_date": gen.graduation_date,
        "matched_keywords": list(gen.matched_keywords),
    }


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    try:
        args = parser.parse_args(argv)
    except _ArgumentParserFailure:
        return _emit_failure("invalid_input")
    error_code = _validate_args(parser, args)
    if error_code is not None:
        return _emit_failure(error_code)
    conn = None
    try:
        conn = connect_read_only(args.db)
        job_ids: tuple[int, ...] | None = (
            tuple(args.job_ids) if args.job_ids is not None else None
        )
        rows, error_code = _select_jobs(conn, args.limit, job_ids)
    except (sqlite3.DatabaseError, ValueError, OSError, PermissionError):
        return _emit_failure("database_error")
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass

    if error_code is not None:
        return _emit_failure(error_code)

    assert rows is not None
    results: list[dict[str, object]] = []
    for row in rows:
        try:
            job = ResumeJob(
                id=row["id"],
                title=row["title"],
                company=row["company"],
                description=row["description"],
                location=row["location"],
                posted_at=row["posted_at"],
            )
            gen = generate_resume(
                job,
                profile_path=args.profile,
                template_path=args.template,
                skill_path=args.skill,
                output_root=args.output_root,
                compiler=args.compiler,
            )
            results.append(_result_row(gen))
        except Exception:
            return _emit_failure("generation_error")

    print(json.dumps({"results": results}, sort_keys=True))
    return 0
