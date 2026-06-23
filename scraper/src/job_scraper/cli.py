from __future__ import annotations

import argparse
import webbrowser
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

from job_scraper.config import AppSettings, build_search_payload, has_company_identifier_filters, load_config
from job_scraper.outreach import load_outreach_config, normalize_linkedin_profile_url
from job_scraper.outreach_storage import OutreachStorage
from job_scraper.scheduler import run_daemon
from job_scraper.storage import JobStorage
from job_scraper.sync import SyncSummary, sync_once
from job_scraper.theirstack import TheirStackClient


def main(argv: list[str] | None = None) -> int:
    load_dotenv()
    parser = _build_parser()
    args = parser.parse_args(argv)
    settings = AppSettings()

    if args.command == "outreach":
        return _handle_outreach(args, settings, parser)

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


def _handle_outreach(args: argparse.Namespace, settings: AppSettings, parser: argparse.ArgumentParser) -> int:
    storage = OutreachStorage(settings.job_scraper_db_path)

    if args.outreach_command == "init":
        storage.initialize()
        print(f"Initialized BotDog outreach tables at {storage.db_path}")
        return 0

    if args.outreach_command == "import-contacts":
        summary = storage.import_contacts_csv(Path(args.csv))
        print(f"Imported contacts: inserted={summary.inserted} updated={summary.updated} skipped={summary.skipped}")
        return 0

    if args.outreach_command == "queue":
        config_path = Path(args.config)
        if not config_path.exists():
            if config_path == Path("config/outreach.yaml"):
                parser.error(
                    "Outreach config not found: config/outreach.yaml. Copy config/outreach.example.yaml "
                    "to config/outreach.yaml before queueing, or pass --config."
                )
            parser.error(f"Outreach config not found: {config_path}")
        config = load_outreach_config(config_path)
        summary = storage.queue_sequence(config)
        print(
            "Queued outreach actions: "
            f"contacts_considered={summary.contacts_considered} "
            f"actions_created={summary.actions_created} "
            f"actions_existing={summary.actions_existing} "
            f"skipped={summary.skipped}"
        )
        return 0

    if args.outreach_command == "next":
        limit = args.limit
        config_path = Path(args.config)
        if limit is None and config_path.exists():
            limit = load_outreach_config(config_path).limits.next_limit
        if limit is None:
            limit = 10
        if limit < 1:
            parser.error("--limit must be at least 1")

        actions = storage.due_actions(limit)
        if not actions:
            print("No pending outreach actions due.")
            return 0
        for action in actions:
            print(
                f"{action.id}\t{action.kind}\t{action.due_at}\t"
                f"{action.linkedin_profile_url}\t{action.message}"
            )
        if args.open:
            webbrowser.open(actions[0].linkedin_profile_url)
        return 0

    if args.outreach_command == "mark":
        action = storage.mark_action(args.action_id, args.status)
        print(f"Updated outreach action {action.id}: status={action.status}")
        return 0

    if args.outreach_command == "mark-contact":
        storage.mark_contact(args.linkedin_url, args.status)
        normalized_url = normalize_linkedin_profile_url(args.linkedin_url)
        print(f"Updated outreach contact {normalized_url}: status={args.status}")
        return 0

    parser.error(f"Unknown outreach command: {args.outreach_command}")
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

    outreach = subparsers.add_parser("outreach", help="Run BotDog LinkedIn outreach queue commands")
    outreach_subparsers = outreach.add_subparsers(dest="outreach_command", required=True)

    outreach_subparsers.add_parser("init", help="Create BotDog outreach tables")

    import_contacts = outreach_subparsers.add_parser("import-contacts", help="Import outreach contacts from CSV")
    import_contacts.add_argument("--csv", required=True, help="Path to contacts CSV")

    queue = outreach_subparsers.add_parser("queue", help="Queue configured outreach actions")
    queue.add_argument("--config", default="config/outreach.yaml", help="Path to the outreach YAML config")

    next_actions = outreach_subparsers.add_parser("next", help="Print due outreach actions")
    next_actions.add_argument("--config", default="config/outreach.yaml", help="Path to the outreach YAML config")
    next_actions.add_argument("--limit", type=int, default=None, help="Maximum number of due actions to print")
    next_actions.add_argument("--open", action="store_true", help="Open the first due LinkedIn profile")

    mark = outreach_subparsers.add_parser("mark", help="Mark an outreach action outcome")
    mark.add_argument("action_id", type=int, help="Outreach action ID")
    mark.add_argument("--status", required=True, choices=["sent", "skipped", "replied", "blocked"], help="Action status")

    mark_contact = outreach_subparsers.add_parser("mark-contact", help="Mark an outreach contact outcome")
    mark_contact.add_argument("--linkedin-url", required=True, help="LinkedIn profile URL")
    mark_contact.add_argument(
        "--status",
        required=True,
        choices=["connected", "replied", "skipped", "do_not_contact"],
        help="Contact status",
    )
    return parser


if __name__ == "__main__":
    raise SystemExit(main())
