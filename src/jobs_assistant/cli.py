from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from . import __version__
from .db import connect, init_db
from .job_source import fetch_source_jobs, import_source_jobs

DEFAULT_DB = Path(os.environ.get("DATABASE_URL", "data/jobs.sqlite3"))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="jobs-assistant", description="Minimal local job backlog ingestion assistant")
    parser.add_argument("--version", action="store_true", help="print package version")
    parser.add_argument("--db", default=str(DEFAULT_DB), help="SQLite database path")
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("init-db", help="initialize the SQLite database")
    import_feed = sub.add_parser("import-feed", help="import normalized jobs from JSON file or GET /v1/jobs feed")
    import_feed.add_argument("--json-file", help="import jobs from a JSON file containing a list, jobs[], or data[]")
    import_feed.add_argument("--base-url", help="source feed base URL; defaults to JOB_SOURCE_BASE_URL when unset")
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
    parser.error(f"unknown command: {args.command}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
