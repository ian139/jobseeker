from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path

from . import __version__
from .application import run_browser_autofill, sample_manual_runs
from .db import connect, init_db, latest_sync_checkpoint, record_sync_run, update_sync_run, utc_now
from .job_source import fetch_source_jobs, import_source_jobs
from .theirstack import (
    PROFILE_NAMES,
    ProfileName,
    TheirStackClient,
    build_paid_fetch_payload,
    build_preview_payload,
    response_total_results,
    sync_theirstack_response,
)

DEFAULT_DB = Path(os.environ.get("DATABASE_URL", "data/jobs.sqlite3"))


def _add_source_profile_argument(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--source-profile",
        "--profile",
        dest="source_profile",
        choices=PROFILE_NAMES,
        default="new_grad_cs",
        help="TheirStack/source filter profile",
    )


def build_job_scrape_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="job-scrape", description="Pull filtered source jobs into the local backlog")
    parser.add_argument("--version", action="store_true", help="print package version")
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

    preview = sub.add_parser("theirstack-preview", help="preview filtered TheirStack match count without persisting jobs")
    _add_source_profile_argument(preview)

    sync = sub.add_parser("theirstack-sync", help="pull filtered TheirStack jobs into the backlog")
    _add_source_profile_argument(sync)
    sync.add_argument("--limit", type=int, default=25, help="maximum jobs to fetch, 1-100")
    sync.add_argument("--paid-fetch", action="store_true", help="confirm this run may consume TheirStack credits")

    autofill = sub.add_parser("autofill", help="open queued jobs and fill safe inferred fields with no-final-submit guard", description="Guarded application draft workflow: fill safe fields only and enforce no-final-submit.")
    autofill.add_argument("--limit", type=int, default=1, help="maximum queued jobs to process")
    autofill.add_argument("--resume-dir", default="resume", help="directory containing resume/profile context")
    autofill.add_argument("--application-profile-json", "--profile-json", dest="application_profile_json", help="explicit application/profile facts JSON; values here are never inferred from resume text")
    autofill.add_argument("--artifact-dir", help="directory for per-run observations, plans, filled-state evidence, and screenshots")
    autofill.add_argument("--ats", choices=("auto", "greenhouse"), default="auto", help="ATS adapter to use for guarded draft filling")
    autofill.add_argument("--headed", action="store_true", help="run headed browser; no-final-submit enforced")

    review = sub.add_parser("autofill-review", help="print recent manual or blocked autofill runs")
    review.add_argument("--limit", type=int, default=10, help="maximum runs to print")
    return parser


def _theirstack_client(*, paid_fetch: bool) -> TheirStackClient:
    api_key = os.environ.get("THEIRSTACK_API_KEY")
    if not api_key:
        raise ValueError("THEIRSTACK_API_KEY is required")
    return TheirStackClient(api_key, enable_paid_fetch=paid_fetch, base_url=os.environ.get("THEIRSTACK_BASE_URL", "https://api.theirstack.com"))


def _paid_fetch_allowed(args: argparse.Namespace) -> bool:
    return bool(args.paid_fetch or os.environ.get("THEIRSTACK_ENABLE_PAID_FETCH", "").lower() in {"1", "true", "yes"})


def run_theirstack_paid_sync(
    conn,
    *,
    source_profile: ProfileName,
    limit: int,
    mode: str,
) -> dict[str, int | str]:
    client = _theirstack_client(paid_fetch=True)
    run_id = record_sync_run(conn, "theirstack", mode, profile=source_profile)
    try:
        checkpoint = latest_sync_checkpoint(conn, source="theirstack", profile=source_profile)
        payload = build_paid_fetch_payload(source_profile, limit=limit, discovered_at_gte=checkpoint)
        response = client.search_jobs(payload)
        seen, inserted, updated = sync_theirstack_response(conn, response, paid_fetch_enabled=True)
        finished_at = utc_now()
        update_sync_run(
            conn,
            run_id,
            finished_at=finished_at,
            checkpoint=finished_at,
            success=True,
            jobs_seen=seen,
            jobs_returned=seen,
            jobs_inserted=inserted,
            jobs_updated=updated,
        )
        return {"source_profile": source_profile, "count": limit, "seen": seen, "inserted": inserted, "updated": updated}
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
    conn = connect(args.db)
    init_db(conn)
    result = run_theirstack_paid_sync(conn, source_profile=args.source_profile, limit=args.count, mode="job_scrape")
    print(json.dumps(result, sort_keys=True))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.version:
        print(__version__)
        return 0
    if args.command is None:
        parser.print_help()
        return 0
    conn = connect(args.db)
    if args.command == "init-db":
        init_db(conn)
        print(f"initialized {args.db}")
        return 0
    init_db(conn)
    if args.command == "import-feed":
        if args.json_file:
            payload = json.loads(Path(args.json_file).read_text())
            raw_jobs = payload if isinstance(payload, list) else payload.get("jobs", payload.get("data", []))
        else:
            base_url = args.base_url or os.environ.get("JOB_SOURCE_BASE_URL")
            if not base_url:
                parser.error("import-feed requires --json-file, --base-url, or JOB_SOURCE_BASE_URL")
            raw_jobs = fetch_source_jobs(base_url, api_key=os.environ.get("JOB_SOURCE_API_KEY"))
        seen, inserted, updated = import_source_jobs(conn, raw_jobs)
        print(json.dumps({"seen": seen, "inserted": inserted, "updated": updated}, sort_keys=True))
        return 0
    if args.command == "theirstack-preview":
        client = _theirstack_client(paid_fetch=False)
        payload = build_preview_payload(args.source_profile)
        response = client.search_jobs(payload)
        print(json.dumps({"profile": args.source_profile, "total_results": response_total_results(response), "credit_safe": True}, sort_keys=True))
        return 0
    if args.command == "theirstack-sync":
        paid_fetch = _paid_fetch_allowed(args)
        if not paid_fetch:
            parser.error("theirstack-sync requires --paid-fetch or THEIRSTACK_ENABLE_PAID_FETCH=true")
        result = run_theirstack_paid_sync(conn, source_profile=args.source_profile, limit=args.limit, mode="paid_fetch")
        print(json.dumps(result, sort_keys=True))
        return 0
    if args.command == "autofill":
        results = asyncio.run(run_browser_autofill(conn, limit=args.limit, resume_dir=args.resume_dir, application_profile_json=args.application_profile_json, artifact_dir=args.artifact_dir, ats=args.ats, headed=args.headed))
        print(json.dumps({"results": results}, sort_keys=True))
        return 0
    if args.command == "autofill-review":
        print(json.dumps({"runs": sample_manual_runs(conn, limit=args.limit)}, sort_keys=True))
        return 0
    parser.error(f"unknown command: {args.command}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
