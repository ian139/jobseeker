from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from . import __version__
from .db import connect, init_db
from .job_source import fetch_source_jobs, import_source_jobs
from .live_smoke import check_playwright_available
from .review import sample_failures
from .runner import run_static_dry_run

DEFAULT_DB = Path(os.environ.get("DATABASE_URL", "data/jobs.sqlite3"))
DEFAULT_RESUME = Path(os.environ.get("RESUME_PATH", "archive/old-applier/data/Main_Resume.pdf"))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="jobs-assistant", description="Local job backlog and application dry-run assistant")
    parser.add_argument("--version", action="store_true", help="print package version")
    parser.add_argument("--db", default=str(DEFAULT_DB), help="SQLite database path")
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("init-db", help="initialize the SQLite database")
    import_feed = sub.add_parser("import-feed", help="import normalized jobs from JSON file or GET /v1/jobs feed")
    import_feed.add_argument("--json-file")
    import_feed.add_argument("--base-url")
    dry = sub.add_parser("dry-run-static", help="run one queued job against static HTML files")
    dry.add_argument("--job-id", type=int, required=True)
    dry.add_argument("--html", nargs="+", required=True)
    dry.add_argument("--facts-json", default="{}")
    dry.add_argument("--url", default="static://application")
    dry.add_argument("--resume", default=str(DEFAULT_RESUME))
    sample = sub.add_parser("sample-failures", help="group failed/blocked/needs-review runs")
    sub.add_parser("live-smoke", help="check optional Playwright live adapter availability")
    sample.add_argument("--limit", type=int, default=20)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.version:
        print(__version__)
        return 0
    if args.command is None:
        parser.print_help()
        return 0
    if args.command == "live-smoke":
        result = check_playwright_available()
        print(json.dumps({"status": result.status, "message": result.message}, sort_keys=True))
        return 0 if result.status in {"ready", "unavailable"} else 1
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
        elif args.base_url:
            raw_jobs = fetch_source_jobs(args.base_url, api_key=os.environ.get("JOB_SOURCE_API_KEY"))
        else:
            parser.error("import-feed requires --json-file or --base-url")
        seen, inserted, updated = import_source_jobs(conn, raw_jobs)
        print(json.dumps({"seen": seen, "inserted": inserted, "updated": updated}, sort_keys=True))
        return 0
    if args.command == "dry-run-static":
        facts = json.loads(args.facts_json)
        pages = [Path(path).read_text() for path in args.html]
        run_id, status, actions = run_static_dry_run(conn, job_id=args.job_id, html_pages=pages, start_url=args.url, facts=facts, resume_path=args.resume)
        print(json.dumps({"run_id": run_id, "status": status.value, "actions": len(actions)}, sort_keys=True))
        return 0
    if args.command == "sample-failures":
        print(json.dumps(sample_failures(conn, limit=args.limit), sort_keys=True))
        return 0
    parser.error(f"unknown command: {args.command}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
