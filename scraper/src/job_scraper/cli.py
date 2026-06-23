from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

from job_scraper.config import AppSettings, build_search_payload, has_company_identifier_filters, load_config
from job_scraper.scheduler import run_daemon
from job_scraper.storage import JobStorage
from job_scraper.sync import SyncSummary, sync_once
from job_scraper.theirstack import TheirStackClient


def main(argv: list[str] | None = None) -> int:
    load_dotenv()
    parser = _build_parser()
    args = parser.parse_args(argv)
    settings = AppSettings()

    if args.command == "init":
        storage = JobStorage(settings.job_scraper_db_path)
        storage.initialize()
        print(f"Initialized SQLite database at {storage.db_path}")
        return 0

    filters_path = Path(args.filters)
    if not filters_path.exists():
        parser.error(
            f"Filter file not found: {filters_path}. Copy config/filters.example.yaml "
            "to config/filters.yaml before live runs, or pass --filters."
        )
    config = load_config(filters_path)

    if args.command == "preview-count":
        if has_company_identifier_filters(config):
            parser.error(
                "preview-count is unavailable when company identifier filters are configured "
                "(company_domain_or, company_linkedin_url_or, or company_name_or)"
            )
        client = TheirStackClient(settings.theirstack_api_key)
        response = preview_count(client, config)
        print(_preview_count_line(response))
        return 0

    storage = JobStorage(settings.job_scraper_db_path)
    client = TheirStackClient(settings.theirstack_api_key)

    if args.command == "run-once":
        summary = sync_once(client, storage, config)
        print(format_summary(summary))
        return 0

    if args.command == "daemon":
        run_daemon(client, storage, config, on_summary=lambda summary: print(format_summary(summary), flush=True))
        return 0

    parser.error(f"Unknown command: {args.command}")
    return 2


def preview_count(client: TheirStackClient, config: Any) -> dict[str, object]:
    payload = build_search_payload(config, page=0, preview_count=True)
    return client.search_jobs(payload)


def format_summary(summary: SyncSummary) -> str:
    return (
        "Run summary: "
        f"pages_fetched={summary.pages_fetched} "
        f"jobs_returned={summary.jobs_returned} "
        f"inserted={summary.inserted} "
        f"updated={summary.updated} "
        f"skipped={summary.skipped} "
        f"checkpoint_before={summary.checkpoint_before or 'none'} "
        f"checkpoint_after={summary.checkpoint_after or 'none'}"
    )


def _preview_count_line(response: dict[str, object]) -> str:
    total = _find_total_results(response)
    if total is None:
        return "Preview total_results=unknown"
    return f"Preview total_results={total}"


def _find_total_results(value: object) -> object | None:
    if isinstance(value, dict):
        for key in ("total_results", "total", "count"):
            if key in value:
                return value[key]
        for nested in value.values():
            found = _find_total_results(nested)
            if found is not None:
                return found
    return None


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="job-scraper")
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("init", help="Create the SQLite data directory and tables")

    run_once = subparsers.add_parser("run-once", help="Run one TheirStack sync immediately")
    run_once.add_argument("--filters", default="config/filters.yaml", help="Path to the YAML filter file")

    daemon = subparsers.add_parser("daemon", help="Run immediately, then sync every 24 hours")
    daemon.add_argument("--filters", default="config/filters.yaml", help="Path to the YAML filter file")

    preview = subparsers.add_parser("preview-count", help="Fetch a blurred TheirStack total count without saving jobs")
    preview.add_argument("--filters", default="config/filters.yaml", help="Path to the YAML filter file")

    return parser


if __name__ == "__main__":
    raise SystemExit(main())
