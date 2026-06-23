from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

from job_scraper.applications import _merged_job_mapping, prepare_application_pack
from job_scraper.config import AppSettings, build_search_payload, has_company_identifier_filters, load_config
from job_scraper.llm import OpenAIResumeLLM, ResumeLLMError
from job_scraper.resume import load_resume_profile, tailor_resume
from job_scraper.scheduler import run_daemon
from job_scraper.storage import APPLICATION_STATUSES, JobStorage
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

    if args.command == "generate-resume":
        storage = JobStorage(settings.job_scraper_db_path)
        job = storage.get_job(args.job_id)
        if job is None:
            raise ValueError(f"Unknown job id: {args.job_id}")
        profile = load_resume_profile(Path(args.profile))
        llm = _resume_llm(settings, no_llm=args.no_llm)
        merged_job = _merged_job_mapping(job)
        try:
            resume = tailor_resume(profile, merged_job, llm=llm)
        except ResumeLLMError as exc:
            print(f"Warning: LLM resume rewrite failed; using deterministic resume: {exc}", file=sys.stderr)
            resume = tailor_resume(profile, merged_job, llm=None)
        if args.output:
            output_path = Path(args.output)
            output_path.parent.mkdir(parents=True, exist_ok=True)
            output_path.write_text(resume.markdown, encoding="utf-8")
            print(f"Wrote resume to {output_path}")
        else:
            print(resume.markdown, end="")
        return 0

    if args.command == "prepare-application":
        storage = JobStorage(settings.job_scraper_db_path)
        llm = _resume_llm(settings, no_llm=args.no_llm)
        try:
            pack = prepare_application_pack(
                storage,
                job_id=args.job_id,
                profile_path=Path(args.profile),
                output_dir=Path(args.output_dir) if args.output_dir else settings.application_pack_dir,
                llm=llm,
                notes=args.notes or "",
            )
        except ResumeLLMError as exc:
            print(f"Warning: LLM resume rewrite failed; using deterministic resume: {exc}", file=sys.stderr)
            pack = prepare_application_pack(
                storage,
                job_id=args.job_id,
                profile_path=Path(args.profile),
                output_dir=Path(args.output_dir) if args.output_dir else settings.application_pack_dir,
                llm=None,
                notes=args.notes or "",
            )
        print(f"Prepared application {pack.application.id} for {args.job_id}: {pack.resume_path}")
        return 0

    if args.command == "list-applications":
        storage = JobStorage(settings.job_scraper_db_path)
        applications = storage.list_applications(status=args.status)
        if not applications:
            print("No applications found")
            return 0
        for application in applications:
            print(
                "\t".join(
                    [
                        str(application.id),
                        application.status,
                        application.theirstack_id,
                        application.resume_path or "",
                        application.updated_at,
                        application.notes,
                    ]
                )
            )
        return 0

    if args.command == "update-application":
        storage = JobStorage(settings.job_scraper_db_path)
        application = storage.update_application(
            args.job_id,
            status=args.status,
            notes=args.notes,
            contact_name=args.contact_name,
            contact_email=args.contact_email,
            applied_at=args.applied_at,
            follow_up_at=args.follow_up_at,
        )
        print(f"Updated application {application.id}: {application.status}")
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




def _resume_llm(settings: AppSettings, *, no_llm: bool) -> OpenAIResumeLLM | None:
    if no_llm or not settings.openai_api_key.strip():
        return None
    return OpenAIResumeLLM(settings.openai_api_key, settings.openai_model)

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

    generate_resume = subparsers.add_parser("generate-resume", help="Generate a tailored Markdown resume for a saved job")
    generate_resume.add_argument("--job-id", required=True, help="Saved TheirStack job id")
    generate_resume.add_argument("--profile", required=True, help="Path to resume profile YAML")
    generate_resume.add_argument("--output", help="Path to write Markdown; stdout when omitted")
    generate_resume.add_argument("--no-llm", action="store_true", help="Disable optional OpenAI rewrite")

    prepare_application = subparsers.add_parser("prepare-application", help="Create a local application pack and CRM row")
    prepare_application.add_argument("--job-id", required=True, help="Saved TheirStack job id")
    prepare_application.add_argument("--profile", required=True, help="Path to resume profile YAML")
    prepare_application.add_argument("--notes", default="", help="Application notes")
    prepare_application.add_argument("--output-dir", help="Directory for application packs")
    prepare_application.add_argument("--no-llm", action="store_true", help="Disable optional OpenAI rewrite")

    list_applications = subparsers.add_parser("list-applications", help="List local application CRM rows")
    list_applications.add_argument("--status", choices=APPLICATION_STATUSES, help="Filter by application status")

    update_application = subparsers.add_parser("update-application", help="Update a local application CRM row")
    update_application.add_argument("--job-id", required=True, help="Saved TheirStack job id")
    update_application.add_argument("--status", choices=APPLICATION_STATUSES, help="New application status")
    update_application.add_argument("--notes", help="Application notes")
    update_application.add_argument("--contact-name", help="Contact name")
    update_application.add_argument("--contact-email", help="Contact email")
    update_application.add_argument("--applied-at", help="Applied date")
    update_application.add_argument("--follow-up-at", help="Follow-up date")

    return parser


if __name__ == "__main__":
    raise SystemExit(main())
