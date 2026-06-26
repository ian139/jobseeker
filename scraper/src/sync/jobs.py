from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from importlib.resources import files
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urlsplit, urlunsplit

from dotenv import load_dotenv
from apply_pipeline.backlog import next_backlog_jobs
from apply_pipeline.contracts import StepStatus
from apply_pipeline.runner import load_applicant_profile, run_backlog_with_playwright
from apply_pipeline.runs import finish_application_run, start_application_run
from apply_pipeline.job_source import list_source_jobs, normalize_source_job, source_job_to_theirstack_like_raw

from theirstack.client import TheirStackClient
from theirstack.queries import PROFILE_NAMES, ProfileName, build_paid_fetch_payload, build_preview_payload


@dataclass(frozen=True)
class StoredJob:
    status: str
    discovered_at: str | None


@dataclass(frozen=True)
class DryRunProfile:
    profile: str
    payload: dict[str, Any]
    total_results: int | None
    dry_run_credits: int
    safe_preview_count_credits: int
    paid_sync_default_max_credits: int


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connect(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(db_path)
    connection.row_factory = sqlite3.Row
    return connection


def initialize_database(connection: sqlite3.Connection) -> None:
    schema = files("db").joinpath("schema.sql").read_text()
    connection.executescript(schema)


def canonicalize_url(value: str | None) -> str | None:
    if not value or not value.strip():
        return None
    parsed = urlsplit(value.strip())
    scheme = parsed.scheme.lower() or "https"
    netloc = parsed.netloc.lower()
    path = parsed.path.rstrip("/")
    query = "&".join(
        part
        for part in parsed.query.split("&")
        if part and not part.lower().startswith(("utm_", "gh_src=", "source="))
    )
    return urlunsplit((scheme, netloc, path, query, ""))


ROLE_PRIORITY_PATTERNS = (
    (
        0,
        (
            re.compile(r"\bsoftware engineer\b"),
            re.compile(r"\bsoftware developer\b"),
            re.compile(r"\bbackend engineer\b"),
            re.compile(r"\bfull[- ]stack engineer\b"),
        ),
    ),
    (
        1,
        (
            re.compile(r"\bdata scientist\b"),
            re.compile(r"\bdata science\b"),
            re.compile(r"\bdata engineer\b"),
        ),
    ),
    (
        2,
        (
            re.compile(r"\bdevops\b"),
            re.compile(r"\bsite reliability\b"),
            re.compile(r"\bsre\b"),
            re.compile(r"\bplatform engineer\b"),
            re.compile(r"\binfrastructure engineer\b"),
            re.compile(r"\bcloud engineer\b"),
        ),
    ),
    (
        3,
        (
            re.compile(r"\bmachine learning engineer\b"),
            re.compile(r"\bml engineer\b"),
        ),
    ),
)

FALL_COOP_FILTER_PATTERN = re.compile(
    r"\b("
    r"co-op|"
    r"(fall|spring|winter)\b.{0,80}\b(co-op|intern(ship)?|2026)|"
    r"new grad(uate)?s?|"
    r"university grad(uate)?s?|"
    r"early career|"
    r"entry[- ]level|"
    r"graduate program"
    r")\b",
    re.IGNORECASE,
)
PROFILE_TEXT_KEYS = (
    "job_title",
    "title",
    "normalized_title",
    "description",
    "job_description",
    "description_text",
)


def matches_fall_coop_filter(raw: dict[str, Any]) -> bool:
    return any(
        FALL_COOP_FILTER_PATTERN.search(value)
        for key in PROFILE_TEXT_KEYS
        if isinstance((value := raw.get(key)), str)
    )


def matches_profile_filter(profile: ProfileName, raw: dict[str, Any]) -> bool:
    if profile == "fall_coop_swe_data":
        return matches_fall_coop_filter(raw)
    return True



def parse_job(raw: dict[str, Any]) -> dict[str, Any]:
    job_id = first_string(raw, "id", "job_id", "theirStackId", "theirstack_id")
    url = first_string(raw, "final_url", "url", "source_url", "apply_url")
    canonical_url = canonicalize_url(url)
    if not job_id and not canonical_url:
        raise ValueError("TheirStack job must include id or URL for dedupe")
    return {
        "theirstack_job_id": job_id,
        "canonical_url": canonical_url,
        "title": first_string(raw, "job_title", "title"),
        "company_name": company_name(raw),
        "location": location_text(raw),
        "country_code": first_string(raw, "job_country_code", "country_code"),
        "remote": bool_to_int(raw.get("remote")),
        "posted_at": first_string(raw, "date_posted", "posted_at"),
        "discovered_at": first_string(raw, "discovered_at"),
        "raw_json": json.dumps(raw, sort_keys=True),
    }


def first_string(raw: dict[str, Any], *keys: str) -> str | None:
    for key in keys:
        value = raw.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
        if isinstance(value, (int, float)):
            return str(value)
    return None


def role_priority(raw: dict[str, Any]) -> int:
    title = (first_string(raw, "job_title", "title", "normalized_title") or "").lower()
    for rank, patterns in ROLE_PRIORITY_PATTERNS:
        if any(pattern.search(title) for pattern in patterns):
            return rank
    return 4


def normalized_company_domain(value: str) -> str:
    domain = value.strip().lower()
    domain = domain.removeprefix("https://").removeprefix("http://")
    domain = domain.rstrip("/")
    domain = domain.removeprefix("www.")
    return domain


def normalized_company_name(value: str) -> str:
    return " ".join(value.split()).lower()


def company_key(raw: dict[str, Any], *, fallback: str) -> str:
    company_object = raw.get("company_object")
    if isinstance(company_object, dict):
        company_id = first_string(company_object, "id")
        if company_id:
            return f"company_id:{company_id.lower()}"
        domain = first_string(company_object, "domain")
        if domain:
            return f"company_domain:{normalized_company_domain(domain)}"
    raw_domain = first_string(raw, "company_domain")
    if raw_domain:
        return f"company_domain:{normalized_company_domain(raw_domain)}"
    if isinstance(company_object, dict):
        url = first_string(company_object, "url")
        if url:
            return f"company_domain:{normalized_company_domain(url)}"
        name = first_string(company_object, "name")
        if name:
            return f"company_name:{normalized_company_name(name)}"
    direct_name = first_string(raw, "company_name")
    if direct_name:
        return f"company_name:{normalized_company_name(direct_name)}"
    deprecated_company = raw.get("company")
    if isinstance(deprecated_company, str) and deprecated_company.strip():
        return f"company_name:{normalized_company_name(deprecated_company)}"
    return f"job:{fallback}"


def company_name(raw: dict[str, Any]) -> str | None:
    company_object = raw.get("company_object")
    if isinstance(company_object, dict):
        documented = first_string(company_object, "name")
        if documented:
            return documented
    direct = first_string(raw, "company_name")
    if direct:
        return direct
    company = raw.get("company")
    if isinstance(company, str) and company.strip():
        return company.strip()
    if isinstance(company, dict):
        return first_string(company, "name", "company_name")
    return None


def location_text(raw: dict[str, Any]) -> str | None:
    location = raw.get("location")
    if isinstance(location, str) and location.strip():
        return location.strip()
    locations = raw.get("locations")
    if isinstance(locations, list):
        parts = [str(item).strip() for item in locations if str(item).strip()]
        return "; ".join(parts) if parts else None
    return first_string(raw, "job_location", "city")


def bool_to_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return int(value)
    return None


def recency_seconds(raw: dict[str, Any], key: str) -> float:
    value = first_string(raw, key)
    if not value:
        return float("-inf")
    try:
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
            return datetime.strptime(value, "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp()
        return datetime.fromisoformat(value.removesuffix("Z") + ("+00:00" if value.endswith("Z") else "")).timestamp()
    except ValueError:
        return float("-inf")


def job_fallback(raw: dict[str, Any], original_index: int) -> str:
    return (
        first_string(raw, "id", "job_id", "theirStackId", "theirstack_id")
        or canonicalize_url(first_string(raw, "final_url", "url", "source_url", "apply_url"))
        or str(original_index)
    )


def select_one_job_per_company(
    raw_jobs: list[dict[str, Any]], *, max_selected: int | None = None
) -> list[dict[str, Any]]:
    best_by_company: dict[str, tuple[tuple[int, float, float, int], dict[str, Any], int]] = {}
    for original_index, raw in enumerate(raw_jobs):
        key = company_key(raw, fallback=job_fallback(raw, original_index))
        candidate_key = (
            role_priority(raw),
            -recency_seconds(raw, "date_posted"),
            -recency_seconds(raw, "discovered_at"),
            original_index,
        )
        current = best_by_company.get(key)
        if current is None or candidate_key < current[0]:
            best_by_company[key] = (candidate_key, raw, original_index)

    selected_with_index = [(value, original_index) for _, value, original_index in best_by_company.values()]
    selected_with_index.sort(
        key=lambda item: (
            -recency_seconds(item[0], "date_posted"),
            -recency_seconds(item[0], "discovered_at"),
            role_priority(item[0]),
            item[1],
        )
    )
    selected = [raw for raw, _ in selected_with_index]
    if max_selected is not None:
        return selected[:max_selected]
    return selected

def upsert_job(connection: sqlite3.Connection, raw: dict[str, Any]) -> StoredJob:
    parsed = parse_job(raw)
    now = utc_now()
    existing = find_existing_job(connection, parsed["theirstack_job_id"], parsed["canonical_url"])
    if existing is None:
        connection.execute(
            """
            INSERT INTO jobs (
                theirstack_job_id, canonical_url, title, company_name, location, country_code, remote,
                posted_at, discovered_at, raw_json, first_seen_at, last_seen_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                parsed["theirstack_job_id"],
                parsed["canonical_url"],
                parsed["title"],
                parsed["company_name"],
                parsed["location"],
                parsed["country_code"],
                parsed["remote"],
                parsed["posted_at"],
                parsed["discovered_at"],
                parsed["raw_json"],
                now,
                now,
            ),
        )
        return StoredJob("inserted", parsed["discovered_at"])

    connection.execute(
        """
        UPDATE jobs
        SET theirstack_job_id = COALESCE(?, theirstack_job_id),
            canonical_url = COALESCE(?, canonical_url),
            title = ?, company_name = ?, location = ?, country_code = ?, remote = ?,
            posted_at = ?, discovered_at = ?, raw_json = ?, last_seen_at = ?
        WHERE id = ?
        """,
        (
            parsed["theirstack_job_id"],
            parsed["canonical_url"],
            parsed["title"],
            parsed["company_name"],
            parsed["location"],
            parsed["country_code"],
            parsed["remote"],
            parsed["posted_at"],
            parsed["discovered_at"],
            parsed["raw_json"],
            now,
            existing["id"],
        ),
    )
    return StoredJob("updated", parsed["discovered_at"])


def find_existing_job(connection: sqlite3.Connection, job_id: str | None, canonical_url: str | None) -> sqlite3.Row | None:
    if job_id:
        row = connection.execute("SELECT id FROM jobs WHERE theirstack_job_id = ?", (job_id,)).fetchone()
        if row:
            return row
    if canonical_url:
        row = connection.execute("SELECT id FROM jobs WHERE canonical_url = ?", (canonical_url,)).fetchone()
        if row:
            return row
    return None


def latest_successful_discovered_at(connection: sqlite3.Connection) -> str | None:
    row = connection.execute(
        """
        SELECT checkpoint_discovered_at AS checkpoint
        FROM sync_runs
        WHERE success = 1 AND checkpoint_discovered_at IS NOT NULL
        ORDER BY id DESC
        LIMIT 1
        """
    ).fetchone()
    return str(row["checkpoint"]) if row and row["checkpoint"] else None

def max_discovered_at(current: str | None, candidates: Iterable[str | None]) -> str | None:
    best = current
    for candidate in candidates:
        if candidate and (best is None or candidate > best):
            best = candidate
    return best


def extract_jobs(response: dict[str, Any]) -> list[dict[str, Any]]:
    for key in ("data", "jobs", "results"):
        value = response.get(key)
        if isinstance(value, list):
            return [item for item in value if isinstance(item, dict)]
    return []


def response_total_results(response: dict[str, Any]) -> int | None:
    value = response.get("total_results")
    if isinstance(value, int):
        return value
    metadata = response.get("metadata")
    if isinstance(metadata, dict) and isinstance(metadata.get("total_results"), int):
        return metadata["total_results"]
    return None


def dry_run_profiles(
    client: TheirStackClient | None = None,
    *,
    call_api: bool = False,
    profiles: Iterable[ProfileName] = PROFILE_NAMES,
    posted_at_max_age_days: int | None = None,
) -> list[DryRunProfile]:
    rows: list[DryRunProfile] = []
    for profile in profiles:
        payload = build_preview_payload(profile, posted_at_max_age_days=posted_at_max_age_days)
        total_results = None
        if call_api:
            response = client.search_jobs(payload) if client is not None else None
            total_results = response_total_results(response or {})
        rows.append(
            DryRunProfile(
                profile=profile,
                payload=payload,
                total_results=total_results,
                dry_run_credits=0,
                safe_preview_count_credits=0,
                paid_sync_default_max_credits=25,
            )
        )
    return rows


def sync_profile(
    client: TheirStackClient,
    connection: sqlite3.Connection,
    profile: ProfileName,
    *,
    limit: int = 25,
    max_pages: int = 1,
    unique_companies: bool = True,
    posted_at_max_age_days: int | None = None,
) -> dict[str, int | str | None]:
    initialize_database(connection)
    checkpoint = latest_successful_discovered_at(connection)
    started_at = utc_now()
    run = connection.execute(
        "INSERT INTO sync_runs (profile, mode, started_at, checkpoint_discovered_at) VALUES (?, 'paid', ?, ?)",
        (profile, started_at, checkpoint),
    )
    run_id = int(run.lastrowid)
    completed_result_set = False
    max_stored_discovered_at: str | None = checkpoint
    returned = inserted = updated = 0
    profile_jobs_for_selection: list[dict[str, Any]] = []
    try:
        for page in range(max_pages):
            payload = build_paid_fetch_payload(
                profile,
                page=page,
                limit=limit,
                discovered_at_gte=checkpoint,
                posted_at_max_age_days=posted_at_max_age_days,
            )
            response = client.search_jobs(payload)
            jobs = extract_jobs(response)
            returned += len(jobs)
            profile_jobs_for_selection.extend(job for job in jobs if matches_profile_filter(profile, job))
            max_stored_discovered_at = max_discovered_at(
                max_stored_discovered_at, [first_string(job, "discovered_at") for job in jobs]
            )
            if len(jobs) < limit:
                completed_result_set = True
                break

        selected_jobs = (
            select_one_job_per_company(profile_jobs_for_selection)
            if unique_companies
            else profile_jobs_for_selection
        )
        selected = len(selected_jobs)
        skipped_duplicate_company = len(profile_jobs_for_selection) - selected if unique_companies else 0
        for raw_job in selected_jobs:
            result = upsert_job(connection, raw_job)
            inserted += int(result.status == "inserted")
            updated += int(result.status == "updated")

        checkpoint_after = max_stored_discovered_at if completed_result_set else checkpoint
        connection.execute(
            """
            UPDATE sync_runs
            SET finished_at = ?, success = 1, jobs_returned = ?, jobs_inserted = ?, jobs_updated = ?,
                checkpoint_discovered_at = ?
            WHERE id = ?
            """,
            (utc_now(), returned, inserted, updated, checkpoint_after, run_id),
        )
        return {
            "returned": returned,
            "selected": selected,
            "inserted": inserted,
            "updated": updated,
            "skipped_duplicate_company": skipped_duplicate_company,
            "checkpoint": checkpoint_after,
        }
    except Exception as exc:
        connection.execute(
            "UPDATE sync_runs SET finished_at = ?, error = ? WHERE id = ?",
            (utc_now(), str(exc), run_id),
        )
        raise


def make_client_from_env() -> TheirStackClient:
    load_dotenv()
    api_key = os.environ.get("THEIRSTACK_API_KEY", "")
    enable_paid = os.environ.get("ENABLE_PAID_FETCH", "false").lower() == "true"
    base_url = os.environ.get("THEIRSTACK_BASE_URL", "https://api.theirstack.com")
    return TheirStackClient(api_key, enable_paid_fetch=enable_paid, base_url=base_url)


def db_path_from_env() -> Path:
    load_dotenv()
    return Path(os.environ.get("JOB_SYNC_DB_PATH", "data/jobs.sqlite3"))


def job_source_settings_from_env() -> tuple[str, str]:
    load_dotenv()
    base_url = os.environ.get("JOB_SOURCE_BASE_URL", "").strip()
    api_key = os.environ.get("JOB_SOURCE_API_KEY", "").strip()
    if not base_url:
        raise ValueError("JOB_SOURCE_BASE_URL is required")
    if not api_key:
        raise ValueError("JOB_SOURCE_API_KEY is required")
    return base_url, api_key


def import_job_source(
    connection: sqlite3.Connection,
    *,
    base_url: str,
    api_key: str,
    limit: int = 100,
    offset: int = 0,
    lane: str | None = None,
    query: str | None = None,
) -> dict[str, int]:
    initialize_database(connection)
    payload = list_source_jobs(base_url=base_url, api_key=api_key, limit=limit, offset=offset, lane=lane, query=query)
    inserted = updated = skipped = 0
    for raw in payload["data"]:
        if not isinstance(raw, dict):
            skipped += 1
            continue
        try:
            source_job = normalize_source_job(raw)
            stored = upsert_job(connection, source_job_to_theirstack_like_raw(source_job))
        except ValueError:
            skipped += 1
            continue
        if stored.status == "inserted":
            inserted += 1
        else:
            updated += 1
    return {
        "returned": len(payload["data"]),
        "inserted": inserted,
        "updated": updated,
        "skipped": skipped,
    }


def apply_dry_run_stub(
    connection: sqlite3.Connection, *, limit: int = 1, max_pages: int = 6
) -> dict[str, int | list[int]]:
    initialize_database(connection)
    attempted = dry_run_ready = needs_review = blocked = failed = 0
    run_ids: list[int] = []
    for job in next_backlog_jobs(connection, limit=limit):
        attempted += 1
        run_id = start_application_run(connection, job_id=int(job["id"]), started_at=utc_now())
        run_ids.append(run_id)
        url = job_application_url_for_cli(job)
        reason = (
            f"live Playwright runner not wired yet; queued for observer/resolver/executor loop "
            f"with max_pages={max_pages}"
        )
        finish_application_run(
            connection,
            run_id=run_id,
            status=StepStatus.FAILED,
            reason=reason,
            finished_at=utc_now(),
            final_url=url,
            actions=[],
        )
        failed += 1
    return {
        "attempted": attempted,
        "dry_run_ready": dry_run_ready,
        "needs_review": needs_review,
        "blocked": blocked,
        "failed": failed,
        "run_ids": run_ids,
    }


def job_application_url_for_cli(job: sqlite3.Row) -> str | None:
    value = job["canonical_url"]
    return str(value) if value else None


def failure_reason_group(reason: str) -> str:
    lowered = reason.lower()
    if "captcha" in lowered:
        return "captcha"
    if "sign in" in lowered or "login" in lowered:
        return "sign_in"
    if "unsupported" in lowered:
        return "unsupported"
    if "unknown" in lowered or "manual" in lowered or "review" in lowered:
        return "needs_review"
    if "max pages" in lowered:
        return "max_pages"
    if "final submit" in lowered:
        return "final_submit_boundary"
    return "other"


def url_host(value: str | None) -> str | None:
    if not value:
        return None
    return urlsplit(value).netloc.lower() or None


def sample_application_failures(
    connection: sqlite3.Connection, *, status: str = "blocked", limit: int = 10
) -> list[dict[str, Any]]:
    initialize_database(connection)
    rows = connection.execute(
        """
        SELECT application_runs.id AS run_id, application_runs.status, application_runs.reason,
               application_runs.final_url, jobs.title, jobs.company_name, jobs.canonical_url
        FROM application_runs
        JOIN jobs ON jobs.id = application_runs.job_id
        WHERE application_runs.status = ?
        ORDER BY application_runs.id DESC
        LIMIT ?
        """,
        (status, limit),
    ).fetchall()
    samples: list[dict[str, Any]] = []
    for row in rows:
        sample = dict(row)
        sample["apply_host"] = url_host(sample.get("final_url") or sample.get("canonical_url"))
        sample["reason_group"] = failure_reason_group(str(sample.get("reason") or ""))
        samples.append(sample)
    return samples


def main(argv: Iterable[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Credit-safe TheirStack job sync prototype")
    subparsers = parser.add_subparsers(dest="command", required=True)
    dry_run_parser = subparsers.add_parser("dry-run", help="Print credit-safe preview payloads")
    dry_run_parser.add_argument("--call-api", action="store_true", help="Make free-preview count calls; never paid fetch")
    dry_run_parser.add_argument("--profile", choices=PROFILE_NAMES, help="Only print or count one profile")
    dry_run_parser.add_argument("--posted-at-max-age-days", type=int, help="Override posted_at_max_age_days")
    subparsers.add_parser("init-db", help="Initialize the local SQLite database")
    apply_parser = subparsers.add_parser("apply-dry-run", help="Prepare queued applications without final submit")
    apply_parser.add_argument("--limit", type=int, default=1)
    apply_parser.add_argument("--max-pages", type=int, default=6)
    apply_parser.add_argument("--live", action="store_true", help="Use Playwright to open and process application pages")
    apply_parser.add_argument("--profile-json", help="Applicant facts JSON file for live apply dry runs; defaults merge AGENTS.md applicant reference")
    apply_parser.add_argument("--resume", help="Resume path allowed for upload fields; defaults to AGENTS.md applicant reference")
    apply_parser.add_argument("--headed", action="store_true", help="Show browser during live apply dry runs")
    apply_parser.add_argument("--manual-handoff", action="store_true", help="Keep the live browser open on terminal statuses until Enter ends inspection; Enter does not resume or submit")
    apply_parser.add_argument("--no-llm", action="store_true", help="Disable the optional Ollama Cloud DeepSeek resolver; by default live runs use it when OLLAMA_CLOUD_API_KEY or OLLAMA_API_KEY is set")
    sample_parser = subparsers.add_parser("apply-sample-failures", help="List application failures for review")
    sample_parser.add_argument("--status", choices=["dry_run_ready", "needs_review", "blocked", "failed"], default="blocked")
    sample_parser.add_argument("--limit", type=int, default=10)
    sync_parser = subparsers.add_parser("sync-once", help="Run paid sync if ENABLE_PAID_FETCH=true")
    sync_parser.add_argument("--profile", choices=PROFILE_NAMES, default="fall_coop_swe_data")
    sync_parser.add_argument("--limit", type=int, default=25)
    sync_parser.add_argument("--max-pages", type=int, default=1)
    sync_parser.add_argument("--posted-at-max-age-days", type=int, help="Override posted_at_max_age_days")
    sync_parser.add_argument(
        "--allow-multiple-per-company",
        action="store_true",
        help="Persist every returned job instead of keeping one selected job per company",
    )
    source_parser = subparsers.add_parser("import-job-source", help="Import read-only jobs from JOB_SOURCE_BASE_URL /v1/jobs")
    source_parser.add_argument("--limit", type=int, default=100)
    source_parser.add_argument("--offset", type=int, default=0)
    source_parser.add_argument("--lane")
    source_parser.add_argument("--query")
    args = parser.parse_args(list(argv) if argv is not None else None)

    if args.command == "dry-run":
        client = make_client_from_env() if args.call_api else None
        selected_profiles = [args.profile] if args.profile else PROFILE_NAMES
        rows = dry_run_profiles(
            client,
            call_api=args.call_api,
            profiles=selected_profiles,
            posted_at_max_age_days=args.posted_at_max_age_days,
        )
        print(json.dumps([row.__dict__ for row in rows], indent=2, sort_keys=True))
        return

    with connect(db_path_from_env()) as connection:
        initialize_database(connection)
        if args.command == "init-db":
            print(f"Initialized {db_path_from_env()}")
            return
        if args.command == "apply-dry-run":
            if args.live:
                profile = load_applicant_profile(args.profile_json, resume_path=args.resume)
                result = run_backlog_with_playwright(
                    connection,
                    profile=profile,
                    now=utc_now,
                    limit=args.limit,
                    max_pages=args.max_pages,
                    headed=args.headed,
                    manual_handoff=args.manual_handoff,
                    use_llm=not args.no_llm,
                )
            else:
                result = apply_dry_run_stub(connection, limit=args.limit, max_pages=args.max_pages)
            print(json.dumps(result, indent=2, sort_keys=True))
            return
        if args.command == "apply-sample-failures":
            rows = sample_application_failures(connection, status=args.status, limit=args.limit)
            print(json.dumps(rows, indent=2, sort_keys=True))
            return
        if args.command == "import-job-source":
            base_url, api_key = job_source_settings_from_env()
            result = import_job_source(
                connection,
                base_url=base_url,
                api_key=api_key,
                limit=args.limit,
                offset=args.offset,
                lane=args.lane,
                query=args.query,
            )
            print(json.dumps(result, indent=2, sort_keys=True))
            return
        if args.command == "sync-once":
            client = make_client_from_env()
            result = sync_profile(
                client,
                connection,
                args.profile,
                limit=args.limit,
                max_pages=args.max_pages,
                unique_companies=not args.allow_multiple_per_company,
                posted_at_max_age_days=args.posted_at_max_age_days,
            )
            print(json.dumps(result, indent=2, sort_keys=True))
            return


if __name__ == "__main__":
    main()
