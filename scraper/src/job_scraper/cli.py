from __future__ import annotations

import argparse
import json
import sys
import webbrowser
from dataclasses import asdict
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

from job_scraper.applications import _merged_job_mapping, prepare_application_pack
from job_scraper.applier import apply_to_job
from job_scraper.config import AppSettings, build_search_payload, has_company_identifier_filters, load_config
from job_scraper.llm import ChatCompletionsResumeLLM, ResumeLLMError
from job_scraper.outreach import OutreachStorage, load_outreach_config, normalize_linkedin_profile_url
from job_scraper.public_json import PublicJsonClient, import_public_json
from job_scraper.matching import ScoredJob, score_jobs
from job_scraper.resume import ResumeProfile, load_resume_profile, tailor_resume
from job_scraper.resume_uploads import UploadedResumeAnalysis
from job_scraper.scheduler import run_daemon
from job_scraper.storage import APPLICATION_STATUSES, JobStorage
from job_scraper.sync import SyncSummary, sync_once
from job_scraper.theirstack import TheirStackClient


def main(argv: list[str] | None = None) -> int:
    load_dotenv()
    parser = _build_parser()
    args = parser.parse_args(argv)
    settings = AppSettings()

    if args.command == "outreach":
        return _handle_outreach(args, settings, parser)

    if args.command == "webui":
        from job_scraper.web import run_web_ui

        run_web_ui(host=args.host, port=args.port, settings=settings)
        return 0

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

    if args.command == "analyze-job":
        storage = JobStorage(settings.job_scraper_db_path)
        job = storage.get_job(args.job_id)
        if job is None:
            raise ValueError(f"Unknown job id: {args.job_id}")
        profile = load_resume_profile(Path(args.profile))
        profile_text = _profile_analysis_text(profile)
        analysis = UploadedResumeAnalysis(
            filename=str(args.profile),
            kind="text",
            text=profile_text,
            facts_markdown="Profile YAML converted to deterministic analysis text.",
        )
        scored = score_jobs([job], analysis, target_roles=[], target_industries=[], keywords=[])[0]
        payload = _analysis_payload(scored)
        if args.json:
            print(json.dumps(payload, indent=2, sort_keys=True, default=str))
        else:
            _print_analysis_summary(payload)
        return 0

    if args.command == "apply":
        storage = JobStorage(settings.job_scraper_db_path)
        result = apply_to_job(
            storage,
            job_id=args.job_id,
            profile_path=Path(args.profile),
            resume_path=Path(args.resume_path) if args.resume_path else None,
            submit=args.submit,
            headless=args.headless or settings.application_browser_headless,
            timeout_ms=args.timeout_ms or settings.application_timeout_ms,
        )
        print(
            "\t".join(
                [
                    result.attempt.status,
                    result.application.status,
                    result.attempt.target_url,
                    result.attempt.message,
                ]
            )
        )
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
    if args.command == "import-public-json":
        storage = JobStorage(settings.job_scraper_db_path)
        client = PublicJsonClient(base_url=args.base_url)
        summary = import_public_json(client, storage)
        print(_format_public_json_summary(summary))
        return 0

    source = getattr(args, "source", None) or settings.job_source
    storage = JobStorage(settings.job_scraper_db_path)

    if args.command == "run-once" and source == "public-json":
        summary = _import_public_json_once(settings, storage)
        print(_format_public_json_summary(summary))
        return 0

    if args.command == "daemon" and source == "public-json":
        _run_public_json_daemon(settings, storage)
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




def _import_public_json_once(settings: AppSettings, storage: JobStorage) -> Any:
    client = PublicJsonClient(base_url=settings.public_json_base_url)
    return import_public_json(client, storage)


def _run_public_json_daemon(settings: AppSettings, storage: JobStorage) -> None:
    from apscheduler.schedulers.blocking import BlockingScheduler

    def scheduled_import() -> None:
        summary = _import_public_json_once(settings, storage)
        print(_format_public_json_summary(summary), flush=True)

    scheduled_import()
    scheduler = BlockingScheduler()
    scheduler.add_job(scheduled_import, "interval", hours=24, id="public-json-import", replace_existing=True)
    scheduler.start()


def _resume_llm(settings: AppSettings, *, no_llm: bool) -> ChatCompletionsResumeLLM | None:
    if no_llm or not settings.llm_api_key.strip():
        return None
    return ChatCompletionsResumeLLM(settings.llm_api_key, settings.llm_model, base_url=settings.llm_base_url)



def _profile_analysis_text(profile: ResumeProfile) -> str:
    lines = [f"# {profile.name}"]
    if profile.headline:
        lines.append(profile.headline)

    contact_parts = [part for part in (profile.contact.email, profile.contact.phone, profile.contact.location) if part]
    contact_parts.extend(profile.contact.links)
    if contact_parts:
        lines.append(" | ".join(contact_parts))

    if profile.skills:
        lines.append("## Skills")
        for group, skills in profile.skills.items():
            lines.append(f"- **{group}:** {', '.join(skills)}")

    for section in profile.sections:
        lines.append(f"## {section.heading}")
        for item in section.items:
            item_heading = f"### {item.title} — {item.organization}"
            if item.location:
                item_heading += f", {item.location}"
            lines.append(item_heading)
            lines.append(f"*{item.dates}*")
            for bullet in item.bullets:
                lines.append(f"- {bullet.text}")

    return "\n".join(lines).strip()


def _analysis_payload(scored: ScoredJob) -> dict[str, object]:
    return {
        "job_id": scored.job.theirstack_id,
        "job": {
            "title": scored.job.title,
            "company": scored.job.company,
            "company_domain": scored.job.company_domain,
            "country_code": scored.job.country_code,
            "remote": scored.job.remote,
            "date_posted": scored.job.date_posted,
            "discovered_at": scored.job.discovered_at,
            "url": scored.job.url,
            "source_url": scored.job.source_url,
            "final_url": scored.job.final_url,
        },
        "score": {
            "overall": scored.score,
            "category": scored.category,
            "region": scored.region,
            "remote_label": scored.remote_label,
            "category_fit": scored.category_fit,
            "matched_terms": list(scored.matched_terms),
            "missing_terms": list(scored.missing_terms),
            "missing_requirements": list(scored.missing_requirements),
            "concerns": list(scored.concerns),
            "explanation": scored.explanation,
            "components": [asdict(component) for component in scored.score_components],
        },
        "analysis": asdict(scored.analysis) if scored.analysis is not None else None,
    }


def _print_analysis_summary(payload: dict[str, object]) -> None:
    job = payload.get("job") if isinstance(payload.get("job"), dict) else {}
    score = payload.get("score") if isinstance(payload.get("score"), dict) else {}
    job_id = str(payload.get("job_id") or "")
    title = job.get("title") or job_id
    company = job.get("company") or "Unknown company"

    print(f"Job: {title} at {company}")
    print(f"Score: {score.get('overall', 0)}/100")

    analysis = payload.get("analysis")
    if not isinstance(analysis, dict):
        print("Analysis: unavailable")
        return

    summary = analysis.get("summary") if isinstance(analysis.get("summary"), dict) else {}
    requirement_coverage = summary.get("requirement_coverage") or "0/0"
    evidence = analysis.get("evidence") if isinstance(analysis.get("evidence"), list) else []
    missing = analysis.get("missing_requirements") if isinstance(analysis.get("missing_requirements"), list) else []
    improvements = analysis.get("improvements") if isinstance(analysis.get("improvements"), list) else []
    bottleneck = summary.get("bottleneck") or "No missing requirements detected"

    print(f"Requirement coverage: {requirement_coverage}")
    print(f"Evidence: {len(evidence)}; Missing: {len(missing)}; Improvements: {len(improvements)}")
    print(f"Bottleneck: {bottleneck}")

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

def _format_public_json_summary(summary: Any) -> str:
    return (
        f"Public JSON import snapshot={summary.snapshot_date} generated_at={summary.generated_at} "
        f"pages={summary.pages_fetched} jobs={summary.jobs_returned} inserted={summary.inserted} "
        f"updated={summary.updated} skipped={summary.skipped} duplicates={summary.duplicates}"
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
    if isinstance(value, list):
        for item in value:
            found = _find_total_results(item)
            if found is not None:
                return found
    return None


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="job-scraper")
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("init", help="Create the SQLite data directory and tables")

    run_once = subparsers.add_parser("run-once", help="Import jobs from the configured job source once")
    run_once.add_argument("--filters", default="config/filters.yaml", help="Path to the YAML filter file for TheirStack runs")
    run_once.add_argument("--source", choices=("public-json", "theirstack"), help="Override JOB_SOURCE for this run")

    daemon = subparsers.add_parser("daemon", help="Run immediately, then import/sync every 24 hours")
    daemon.add_argument("--filters", default="config/filters.yaml", help="Path to the YAML filter file for TheirStack runs")
    daemon.add_argument("--source", choices=("public-json", "theirstack"), help="Override JOB_SOURCE for this daemon")

    preview = subparsers.add_parser("preview-count", help="Fetch a blurred TheirStack total count without saving jobs")
    preview.add_argument("--filters", default="config/filters.yaml", help="Path to the YAML filter file")

    import_public = subparsers.add_parser(
        "import-public-json",
        help="Import public JSON jobs directly",
    )
    import_public.add_argument(
        "--base-url",
        default="https://doomersareretardedcommunists.com/",
        help="Public JSON homepage URL",
    )

    webui = subparsers.add_parser("webui", help="Run the local scraped-jobs resume prompt web UI")
    webui.add_argument("--host", default="127.0.0.1", help="Host interface for the web UI")
    webui.add_argument("--port", type=int, default=8000, help="Port for the web UI")

    generate_resume = subparsers.add_parser("generate-resume", help="Generate a tailored Markdown resume for a saved job")
    generate_resume.add_argument("--job-id", required=True, help="Saved job id")
    generate_resume.add_argument("--profile", required=True, help="Path to resume profile YAML")
    generate_resume.add_argument("--output", help="Path to write Markdown; stdout when omitted")
    generate_resume.add_argument("--no-llm", action="store_true", help="Disable optional LLM rewrite")

    prepare_application = subparsers.add_parser("prepare-application", help="Create a local application pack and CRM row")
    prepare_application.add_argument("--job-id", required=True, help="Saved job id")
    prepare_application.add_argument("--profile", required=True, help="Path to resume profile YAML")
    prepare_application.add_argument("--notes", default="", help="Application notes")
    prepare_application.add_argument("--output-dir", help="Directory for application packs")
    prepare_application.add_argument("--no-llm", action="store_true", help="Disable optional LLM rewrite")

    analyze_job = subparsers.add_parser("analyze-job", help="Inspect structured job/resume analysis for a saved job")
    analyze_job.add_argument("--job-id", required=True, help="Saved job id")
    analyze_job.add_argument("--profile", required=True, help="Path to resume profile YAML")
    analyze_job.add_argument("--json", action="store_true", help="Print machine-readable structured analysis JSON")

    apply = subparsers.add_parser("apply", help="Fill a saved job application in Chromium")
    apply.add_argument("--job-id", required=True, help="Saved TheirStack job id")
    apply.add_argument("--profile", required=True, help="Path to resume profile YAML for contact fields")
    apply.add_argument("--resume-path", help="Resume file to upload; defaults to the application resume_path")
    apply.add_argument("--submit", action="store_true", help="Click final submit when all required fields are filled")
    apply.add_argument("--headless", action="store_true", help="Run Chromium headlessly")
    apply.add_argument("--timeout-ms", type=int, help="Browser action timeout in milliseconds")

    list_applications = subparsers.add_parser("list-applications", help="List local application CRM rows")
    list_applications.add_argument("--status", choices=APPLICATION_STATUSES, help="Filter by application status")

    update_application = subparsers.add_parser("update-application", help="Update a local application CRM row")
    update_application.add_argument("--job-id", required=True, help="Saved job id")
    update_application.add_argument("--status", choices=APPLICATION_STATUSES, help="New application status")
    update_application.add_argument("--notes", help="Application notes")
    update_application.add_argument("--contact-name", help="Contact name")
    update_application.add_argument("--contact-email", help="Contact email")
    update_application.add_argument("--applied-at", help="Applied date")
    update_application.add_argument("--follow-up-at", help="Follow-up date")

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
