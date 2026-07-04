import sqlite3
import json

from sync.jobs import (
    company_key,
    dry_run_profiles,
    initialize_database,
    latest_successful_discovered_at,
    matches_fall_coop_filter,
    role_priority,
    select_one_job_per_company,
    sync_profile,
    upsert_job,
)
from theirstack.client import PaidFetchDisabledError
import sync.jobs as jobs_module


class RecordingClient:
    def __init__(self, response: dict[str, object]) -> None:
        self.response = response
        self.payloads: list[dict[str, object]] = []

    def search_jobs(self, payload: dict[str, object]) -> dict[str, object]:
        self.payloads.append(payload)
        return self.response


class SequenceClient:
    def __init__(self, responses: list[dict[str, object]]) -> None:
        self.responses = responses
        self.payloads: list[dict[str, object]] = []

    def search_jobs(self, payload: dict[str, object]) -> dict[str, object]:
        self.payloads.append(payload)
        return self.responses[len(self.payloads) - 1]


class BlockingClient:
    def search_jobs(self, payload: dict[str, object]) -> dict[str, object]:
        raise PaidFetchDisabledError("blocked")


def memory_db() -> sqlite3.Connection:
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    initialize_database(connection)
    return connection


def test_upsert_dedupes_by_theirstack_id() -> None:
    connection = memory_db()
    first = upsert_job(connection, {"id": "ts-1", "job_title": "Software Engineer", "url": "https://example.com/a?utm_source=x"})
    second = upsert_job(connection, {"id": "ts-1", "job_title": "Backend Engineer", "url": "https://example.com/a"})
    count = connection.execute("SELECT COUNT(*) AS count FROM jobs").fetchone()["count"]
    title = connection.execute("SELECT title FROM jobs WHERE theirstack_job_id = 'ts-1'").fetchone()["title"]
    assert first.status == "inserted"
    assert second.status == "updated"
    assert count == 1
    assert title == "Backend Engineer"


def test_upsert_dedupes_by_canonical_url_without_id() -> None:
    connection = memory_db()
    first = upsert_job(connection, {"job_title": "Software Engineer", "url": "HTTPS://Example.com/jobs/1/?utm_campaign=x"})
    second = upsert_job(connection, {"job_title": "Software Engineer II", "url": "https://example.com/jobs/1"})
    row = connection.execute("SELECT COUNT(*) AS count, title FROM jobs").fetchone()
    assert first.status == "inserted"
    assert second.status == "updated"
    assert row["count"] == 1
    assert row["title"] == "Software Engineer II"


def test_upsert_prefers_apply_url_over_listing_url_fields() -> None:
    connection = memory_db()
    upsert_job(
        connection,
        {
            "id": "ts-apply-url",
            "job_title": "Software Engineer",
            "url": "https://jobs.example.com/listing/1",
            "source_url": "https://jobs.example.com/source/1",
            "apply_url": "https://boards.example.com/apply/1",
        },
    )

    row = connection.execute("SELECT canonical_url FROM jobs WHERE theirstack_job_id = 'ts-apply-url'").fetchone()
    assert row["canonical_url"] == "https://boards.example.com/apply/1"


def test_dry_run_does_not_call_api_by_default() -> None:
    rows = dry_run_profiles(BlockingClient(), call_api=False)
    assert len(rows) == 1
    assert all(row.total_results is None for row in rows)
    assert sum(row.dry_run_credits for row in rows) == 0
    assert sum(row.safe_preview_count_credits for row in rows) == 0
    assert sum(row.paid_sync_default_max_credits for row in rows) == 25


def test_dry_run_can_collect_safe_preview_counts() -> None:
    client = RecordingClient({"total_results": 7, "data": []})
    rows = dry_run_profiles(client, call_api=True)
    assert [row.total_results for row in rows] == [7]
    assert all(payload["blur_company_data"] is True for payload in client.payloads)
    assert all(payload["limit"] == 1 for payload in client.payloads)
    rows = dry_run_profiles(client, call_api=True, posted_at_max_age_days=2)
    assert client.payloads[-1]["posted_at_max_age_days"] == 2


def test_dry_run_reads_total_results_from_metadata() -> None:
    client = RecordingClient({"metadata": {"total_results": 11}, "data": []})
    rows = dry_run_profiles(client, call_api=True, profiles=["fall_coop_swe_data"])
    assert [row.total_results for row in rows] == [11]


def test_company_key_prefers_documented_company_object_id() -> None:
    assert company_key({"company_object": {"id": "C_1", "name": "Acme"}}, fallback="0") == "company_id:c_1"
    assert company_key({"company_object": {"id": "C_2", "name": "ACME"}}, fallback="1") == "company_id:c_2"


def test_role_priority_orders_swe_then_ds_then_devops_then_mle() -> None:
    titles = [
        "Software Engineer Intern",
        "Data Scientist Intern",
        "DevOps Engineer Intern",
        "Machine Learning Engineer Intern",
    ]
    assert [role_priority({"job_title": title}) for title in titles] == [0, 1, 2, 3]


def test_select_one_job_per_company_prefers_role_before_recency_within_company() -> None:
    jobs = [
        {
            "id": "mle",
            "job_title": "Machine Learning Engineer Intern",
            "company_object": {"id": "C_1"},
            "date_posted": "2026-06-20",
        },
        {
            "id": "swe",
            "job_title": "Software Engineer Intern",
            "company_object": {"id": "C_1"},
            "date_posted": "2026-06-01",
        },
    ]
    assert [job["id"] for job in select_one_job_per_company(jobs)] == ["swe"]


def test_select_one_job_per_company_sorts_selected_jobs_by_newest_across_companies() -> None:
    jobs = [
        {
            "id": "older",
            "job_title": "Software Engineer Intern",
            "company_object": {"id": "C_1"},
            "date_posted": "2026-06-01",
            "discovered_at": "2026-06-02T00:00:00+00:00",
        },
        {
            "id": "newer",
            "job_title": "Data Scientist Intern",
            "company_object": {"id": "C_2"},
            "date_posted": "2026-06-20",
            "discovered_at": "2026-06-21T00:00:00+00:00",
        },
    ]
    assert [job["id"] for job in select_one_job_per_company(jobs)] == ["newer", "older"]


def test_fall_coop_filter_rejects_summer_or_generic_internships() -> None:
    assert matches_fall_coop_filter({"job_title": "Fall Software Engineer Intern"})
    assert matches_fall_coop_filter({"description": "Work in a fall 2026 co-op program."})
    assert matches_fall_coop_filter({"job_title": "Spring Data Scientist Intern"})
    assert matches_fall_coop_filter({"description": "Winter 2026 internship"})
    assert matches_fall_coop_filter({"job_title": "New Grad Software Engineer"})
    assert matches_fall_coop_filter({"description": "Early career graduate program for engineers."})
    assert not matches_fall_coop_filter({"job_title": "Software Engineer Intern", "description": "Summer 2026 internship"})


def test_sync_filters_fall_coop_profile_before_company_selection() -> None:
    connection = memory_db()
    client = RecordingClient(
        {
            "data": [
                {
                    "id": "generic-swe",
                    "job_title": "Software Engineer Intern",
                    "company_object": {"id": "Acme"},
                    "url": "https://example.com/generic-swe",
                    "date_posted": "2026-06-20",
                    "discovered_at": "2026-06-20T00:00:00+00:00",
                    "description": "Summer 2026 internship",
                },
                {
                    "id": "fall-mle",
                    "job_title": "Machine Learning Engineer Intern",
                    "company_object": {"id": "Acme"},
                    "url": "https://example.com/fall-mle",
                    "date_posted": "2026-06-01",
                    "discovered_at": "2026-06-01T00:00:00+00:00",
                    "description": "Fall 2026 co-op",
                },
                {
                    "id": "generic-ds",
                    "job_title": "Data Scientist Intern",
                    "company_object": {"id": "Globex"},
                    "url": "https://example.com/generic-ds",
                    "date_posted": "2026-06-19",
                    "discovered_at": "2026-06-19T00:00:00+00:00",
                    "description": "Summer 2026 internship",
                },
            ]
        }
    )
    result = sync_profile(client, connection, "fall_coop_swe_data", limit=25, max_pages=1)
    rows = connection.execute("SELECT theirstack_job_id, title FROM jobs").fetchall()
    assert result["returned"] == 3
    assert result["selected"] == 1
    assert result["skipped_duplicate_company"] == 0
    assert [(row["theirstack_job_id"], row["title"]) for row in rows] == [
        ("fall-mle", "Machine Learning Engineer Intern")
    ]


def test_sync_uses_completed_run_checkpoint_for_later_runs() -> None:
    connection = memory_db()
    client = RecordingClient(
        {
            "data": [
                {
                    "id": "ts-1",
                    "job_title": "Software Engineer",
                    "url": "https://example.com/jobs/1",
                    "description": "Fall 2026 internship",
                    "discovered_at": "2026-06-20T00:00:00+00:00",
                }
            ]
        }
    )
    result = sync_profile(client, connection, "fall_coop_swe_data", limit=25, max_pages=1)
    assert result == {
        "returned": 1,
        "selected": 1,
        "inserted": 1,
        "updated": 0,
        "skipped_duplicate_company": 0,
        "checkpoint": "2026-06-20T00:00:00+00:00",
    }
    assert latest_successful_discovered_at(connection) == "2026-06-20T00:00:00+00:00"

    client.response = {"data": []}
    sync_profile(client, connection, "fall_coop_swe_data", limit=25, max_pages=1)
    assert client.payloads[-1]["discovered_at_gte"] == "2026-06-20T00:00:00+00:00"
    sync_profile(client, connection, "fall_coop_swe_data", limit=25, max_pages=1, posted_at_max_age_days=2)
    assert client.payloads[-1]["posted_at_max_age_days"] == 2


def test_sync_does_not_advance_checkpoint_when_page_window_truncates_results() -> None:
    connection = memory_db()
    client = RecordingClient(
        {
            "data": [
                {
                    "id": "ts-1",
                    "url": "https://example.com/1",
                    "description": "Fall 2026 internship",
                    "discovered_at": "2026-06-20T00:00:00+00:00",
                },
                {
                    "id": "ts-2",
                    "url": "https://example.com/2",
                    "description": "Fall 2026 internship",
                    "discovered_at": "2026-06-21T00:00:00+00:00",
                },
            ]
        }
    )
    result = sync_profile(client, connection, "fall_coop_swe_data", limit=2, max_pages=1)
    assert result["checkpoint"] is None
    assert latest_successful_discovered_at(connection) is None


def test_sync_skips_duplicate_company_jobs_by_default() -> None:
    connection = memory_db()
    client = RecordingClient(
        {
            "data": [
                {
                    "id": "bd-mle",
                    "job_title": "Machine Learning Engineer Intern",
                    "company_object": {"id": "ByteDance"},
                    "url": "https://example.com/bd-mle",
                    "date_posted": "2026-06-20",
                    "discovered_at": "2026-06-20T00:00:00+00:00",
                    "description": "Fall 2026 co-op",
                },
                {
                    "id": "acme-ds",
                    "job_title": "Data Scientist Intern",
                    "company_object": {"domain": "https://www.acme.com/"},
                    "url": "https://example.com/acme-ds",
                    "date_posted": "2026-06-19",
                    "discovered_at": "2026-06-19T00:00:00+00:00",
                    "description": "Fall 2026 internship",
                },
                {
                    "id": "bd-swe",
                    "job_title": "Software Engineer Intern",
                    "company_object": {"id": "ByteDance"},
                    "url": "https://example.com/bd-swe",
                    "date_posted": "2026-06-01",
                    "discovered_at": "2026-06-01T00:00:00+00:00",
                    "description": "Fall 2026 co-op",
                },
                {
                    "id": "globex-devops",
                    "job_title": "DevOps Engineer Intern",
                    "company_name": "Globex",
                    "url": "https://example.com/globex-devops",
                    "date_posted": "2026-06-18",
                    "discovered_at": "2026-06-18T00:00:00+00:00",
                    "description": "Fall 2026 internship",
                },
            ]
        }
    )
    result = sync_profile(client, connection, "fall_coop_swe_data", limit=25, max_pages=1)
    titles = [row["title"] for row in connection.execute("SELECT title FROM jobs ORDER BY title").fetchall()]
    assert result["returned"] == 4
    assert result["selected"] == 3
    assert result["inserted"] == 3
    assert result["skipped_duplicate_company"] == 1
    assert "Software Engineer Intern" in titles
    assert "Machine Learning Engineer Intern" not in titles


def test_sync_dedupes_duplicate_companies_across_pages() -> None:
    connection = memory_db()
    client = SequenceClient(
        [
            {
                "data": [
                    {
                        "id": "acme-mle",
                        "job_title": "Machine Learning Engineer Intern",
                        "company_object": {"id": "Acme"},
                        "url": "https://example.com/acme-mle",
                        "date_posted": "2026-06-20",
                        "discovered_at": "2026-06-20T00:00:00+00:00",
                        "description": "Fall 2026 co-op",
                    },
                    {
                        "id": "globex-ds",
                        "job_title": "Data Scientist Intern",
                        "company_object": {"id": "Globex"},
                        "url": "https://example.com/globex-ds",
                        "date_posted": "2026-06-19",
                        "discovered_at": "2026-06-19T00:00:00+00:00",
                        "description": "Fall 2026 internship",
                    },
                ]
            },
            {
                "data": [
                    {
                        "id": "acme-swe",
                        "job_title": "Software Engineer Intern",
                        "company_object": {"id": "Acme"},
                        "url": "https://example.com/acme-swe",
                        "date_posted": "2026-06-01",
                        "discovered_at": "2026-06-01T00:00:00+00:00",
                        "description": "Fall 2026 co-op",
                    }
                ]
            },
        ]
    )
    result = sync_profile(client, connection, "fall_coop_swe_data", limit=2, max_pages=2)
    rows = connection.execute("SELECT theirstack_job_id FROM jobs ORDER BY theirstack_job_id").fetchall()
    assert result["returned"] == 3
    assert result["selected"] == 2
    assert result["inserted"] == 2
    assert result["skipped_duplicate_company"] == 1
    assert [row["theirstack_job_id"] for row in rows] == ["acme-swe", "globex-ds"]

def test_sync_can_allow_multiple_jobs_per_company() -> None:
    connection = memory_db()
    client = RecordingClient(
        {
            "data": [
                {
                    "id": "bd-mle",
                    "job_title": "Machine Learning Engineer Intern",
                    "company_object": {"id": "ByteDance"},
                    "url": "https://example.com/bd-mle",
                    "date_posted": "2026-06-20",
                    "discovered_at": "2026-06-20T00:00:00+00:00",
                    "description": "Fall 2026 co-op",
                },
                {
                    "id": "acme-ds",
                    "job_title": "Data Scientist Intern",
                    "company_object": {"domain": "acme.com"},
                    "url": "https://example.com/acme-ds",
                    "date_posted": "2026-06-19",
                    "discovered_at": "2026-06-19T00:00:00+00:00",
                    "description": "Fall 2026 internship",
                },
                {
                    "id": "bd-swe",
                    "job_title": "Software Engineer Intern",
                    "company_object": {"id": "ByteDance"},
                    "url": "https://example.com/bd-swe",
                    "date_posted": "2026-06-01",
                    "discovered_at": "2026-06-01T00:00:00+00:00",
                    "description": "Fall 2026 co-op",
                },
                {
                    "id": "globex-devops",
                    "job_title": "DevOps Engineer Intern",
                    "company_name": "Globex",
                    "url": "https://example.com/globex-devops",
                    "date_posted": "2026-06-18",
                    "discovered_at": "2026-06-18T00:00:00+00:00",
                    "description": "Fall 2026 internship",
                },
            ]
        }
    )
    result = sync_profile(client, connection, "fall_coop_swe_data", limit=25, max_pages=1, unique_companies=False)
    row = connection.execute("SELECT COUNT(*) AS count FROM jobs").fetchone()
    assert result["returned"] == 4
    assert result["selected"] == 4
    assert result["inserted"] == 4
    assert result["skipped_duplicate_company"] == 0
    assert row["count"] == 4


def test_apply_dry_run_live_uses_default_profile_loader_when_args_omitted(monkeypatch, tmp_path, capsys) -> None:
    seen: dict[str, object] = {}

    def fake_load_applicant_profile(path: str | None, *, resume_path: str | None = None, exclude_facts=()):
        seen["profile_path"] = path
        seen["resume_path"] = resume_path
        seen["exclude_facts"] = tuple(exclude_facts)
        return "profile"

    def fake_run_backlog_with_playwright(connection, *, profile, now, limit, max_pages, headed, manual_handoff, use_llm, block_linkedin_jobs):
        seen["profile"] = profile
        seen["limit"] = limit
        seen["max_pages"] = max_pages
        seen["headed"] = headed
        seen["manual_handoff"] = manual_handoff
        seen["use_llm"] = use_llm
        seen["block_linkedin_jobs"] = block_linkedin_jobs
        return {"attempted": 0, "dry_run_ready": 0, "needs_review": 0, "blocked": 0, "failed": 0, "run_ids": []}

    monkeypatch.setenv("JOB_SYNC_DB_PATH", str(tmp_path / "jobs.sqlite3"))
    monkeypatch.setattr(jobs_module, "load_applicant_profile", fake_load_applicant_profile)
    monkeypatch.setattr(jobs_module, "run_backlog_with_playwright", fake_run_backlog_with_playwright)

    jobs_module.main(["apply-dry-run", "--live"])

    assert seen == {
        "profile_path": None,
        "resume_path": None,
        "exclude_facts": (),
        "profile": "profile",
        "limit": 1,
        "max_pages": 6,
        "headed": False,
        "manual_handoff": False,
        "use_llm": True,
        "block_linkedin_jobs": False,
    }
    assert json.loads(capsys.readouterr().out)["attempted"] == 0


def test_apply_dry_run_live_forwards_linkedin_blocker_and_profile_exclusion(monkeypatch, tmp_path, capsys) -> None:
    seen: dict[str, object] = {}

    def fake_load_applicant_profile(path: str | None, *, resume_path: str | None = None, exclude_facts=()):
        seen["exclude_facts"] = tuple(exclude_facts)
        return "profile"

    def fake_run_backlog_with_playwright(connection, *, profile, now, limit, max_pages, headed, manual_handoff, use_llm, block_linkedin_jobs):
        seen["block_linkedin_jobs"] = block_linkedin_jobs
        return {"attempted": 0, "dry_run_ready": 0, "needs_review": 0, "blocked": 0, "failed": 0, "run_ids": []}

    monkeypatch.setenv("JOB_SYNC_DB_PATH", str(tmp_path / "jobs.sqlite3"))
    monkeypatch.setattr(jobs_module, "load_applicant_profile", fake_load_applicant_profile)
    monkeypatch.setattr(jobs_module, "run_backlog_with_playwright", fake_run_backlog_with_playwright)

    jobs_module.main(["apply-dry-run", "--live", "--block-linkedin-jobs", "--exclude-profile-fact", "linkedin"])

    assert seen == {"exclude_facts": ("linkedin",), "block_linkedin_jobs": True}
    assert json.loads(capsys.readouterr().out)["attempted"] == 0



def test_apply_dry_run_live_no_llm_forwards_disabled_flag(monkeypatch, tmp_path, capsys) -> None:
    seen: dict[str, object] = {}

    def fake_load_applicant_profile(path: str | None, *, resume_path: str | None = None, exclude_facts=()):
        return "profile"

    def fake_run_backlog_with_playwright(connection, *, profile, now, limit, max_pages, headed, manual_handoff, use_llm, block_linkedin_jobs):
        seen["use_llm"] = use_llm
        return {"attempted": 0, "dry_run_ready": 0, "needs_review": 0, "blocked": 0, "failed": 0, "run_ids": []}

    monkeypatch.setenv("JOB_SYNC_DB_PATH", str(tmp_path / "jobs.sqlite3"))
    monkeypatch.setattr(jobs_module, "load_applicant_profile", fake_load_applicant_profile)
    monkeypatch.setattr(jobs_module, "run_backlog_with_playwright", fake_run_backlog_with_playwright)

    jobs_module.main(["apply-dry-run", "--live", "--no-llm"])

    assert seen == {"use_llm": False}
    assert json.loads(capsys.readouterr().out)["attempted"] == 0

def test_profile_from_resume_command_writes_resume_profile(tmp_path, capsys) -> None:
    resume = tmp_path / "resume.txt"
    output = tmp_path / "profile.json"
    resume.write_text("Ian Rapko\n\nSkills:\nPython, Playwright\n\nExperience:\nAutomation")

    jobs_module.main(["profile-from-resume", "--resume", str(resume), "--output", str(output)])

    payload = json.loads(output.read_text())
    assert "Ian Rapko" in payload["resume_summary"]
    assert payload["skills"] == "Python, Playwright"
    assert "Wrote" in capsys.readouterr().out


def test_apply_review_packets_include_latest_snapshot_resolver_and_failed_actions() -> None:
    connection = memory_db()
    upsert_job(connection, {"id": "ts-1", "job_title": "Software Engineer", "company_name": "Acme", "url": "https://example.com/apply"})
    job_id = connection.execute("SELECT id FROM jobs WHERE theirstack_job_id = 'ts-1'").fetchone()["id"]
    run_id = connection.execute(
        """
        INSERT INTO application_runs (job_id, status, reason, started_at, finished_at, final_url, actions_json)
        VALUES (?, 'failed', 'executor_action_failed', '2026-01-01T00:00:00+00:00', '2026-01-01T00:01:00+00:00', 'https://example.com/apply', ?)
        """,
        (
            job_id,
            json.dumps(
                [
                    {"action": {"kind": "fill", "target_id": "email", "value": "ian@example.com"}, "status": "succeeded", "reason": None},
                    {"action": {"kind": "fill", "target_id": "why", "value": "x"}, "status": "failed", "reason": "field detached"},
                ]
            ),
        ),
    ).lastrowid
    connection.execute(
        """
        INSERT INTO application_pages (run_id, page_index, url, snapshot_json, resolver_json, created_at)
        VALUES (?, 0, 'https://example.com/apply', ?, ?, '2026-01-01T00:00:30+00:00')
        """,
        (
            run_id,
            json.dumps(
                {
                    "url": "https://example.com/apply",
                    "fields": [{"id": "why", "kind": "textarea", "label": "Why are you interested?", "required": True, "options": [], "value": None, "disabled": False, "visible": True, "frame": None, "selector": "#why"}],
                    "buttons": [{"id": "next", "text": "Continue", "type": None, "disabled": False, "final_submit_candidate": False, "visible": True, "frame": None, "selector": "#next"}],
                    "errors": [],
                    "blockers": [],
                    "metadata": {"observed_field_count": 1},
                }
            ),
            json.dumps(
                {
                    "answers": [],
                    "next_button_id": None,
                    "submit_button_id": None,
                    "needs_review": ["resolver_unknown_required_after_llm: Why are you interested?"],
                    "metadata": {"reason_codes": ["resolver_unknown_required_after_llm"]},
                }
            ),
        ),
    )

    packets = jobs_module.application_review_packets(connection, status="failed", limit=1)

    assert packets[0]["run_id"] == run_id
    assert packets[0]["job"] == {"title": "Software Engineer", "company_name": "Acme", "canonical_url": "https://example.com/apply"}
    assert packets[0]["latest_page"]["snapshot"]["fields"][0]["id"] == "why"
    assert packets[0]["latest_page"]["resolver"]["needs_review"] == ["resolver_unknown_required_after_llm: Why are you interested?"]
    assert packets[0]["failed_actions"] == [{"action": {"kind": "fill", "target_id": "why", "value": "x"}, "status": "failed", "reason": "field detached"}]
    assert packets[0]["annotation_template"]["run_id"] == run_id
    assert packets[0]["annotation_template"]["decisions"][0] == {"field_id": "why", "answer": "", "persistence": "once", "note": ""}


def test_apply_review_annotations_store_once_always_never_without_plain_sensitive_facts(tmp_path) -> None:
    connection = memory_db()
    upsert_job(connection, {"id": "ts-1", "job_title": "Software Engineer", "url": "https://example.com/apply"})
    job_id = connection.execute("SELECT id FROM jobs WHERE theirstack_job_id = 'ts-1'").fetchone()["id"]
    run_id = connection.execute(
        """
        INSERT INTO application_runs (job_id, status, reason, started_at, actions_json)
        VALUES (?, 'needs_review', 'resolver_sensitive_field', '2026-01-01T00:00:00+00:00', '[]')
        """,
        (job_id,),
    ).lastrowid
    connection.execute(
        """
        INSERT INTO application_pages (run_id, page_index, url, snapshot_json, resolver_json, created_at)
        VALUES (?, 0, 'https://example.com/apply', ?, NULL, '2026-01-01T00:00:30+00:00')
        """,
        (
            run_id,
            json.dumps(
                {
                    "url": "https://example.com/apply",
                    "fields": [
                        {"id": "gender", "kind": "typeahead", "label": "Gender", "required": False, "options": [], "value": None, "disabled": False, "visible": True, "frame": None, "selector": "#gender"},
                        {"id": "hispanic_ethnicity", "kind": "typeahead", "label": "Are you Hispanic/Latino?", "required": False, "options": [], "value": None, "disabled": False, "visible": True, "frame": None, "selector": "#hispanic_ethnicity"},
                        {"id": "country", "kind": "typeahead", "label": "Country", "required": True, "options": [], "value": None, "disabled": False, "visible": True, "frame": None, "selector": "#country"},
                        {"id": "custom", "kind": "textarea", "label": "Explain manually", "required": True, "options": [], "value": None, "disabled": False, "visible": True, "frame": None, "selector": "#custom"},
                    ],
                    "buttons": [],
                    "errors": [],
                    "blockers": [],
                    "metadata": {},
                }
            ),
        ),
    )
    profile_path = tmp_path / "profile.json"
    profile_path.write_text(json.dumps({"email": "ian@example.com"}) + "\n")
    annotation_path = tmp_path / "annotations.json"
    annotation_path.write_text(
        json.dumps(
            {
                "run_id": run_id,
                "decisions": [
                    {"field_id": "gender", "answer": "Male", "persistence": "always"},
                    {"field_id": "hispanic_ethnicity", "answer": "No", "persistence": "once"},
                    {"field_id": "country", "answer": "United States", "persistence": "always"},
                    {"field_id": "custom", "persistence": "never", "note": "Manual only"},
                ],
            }
        )
    )

    result = jobs_module.apply_review_annotations(connection, annotation_path, profile_path=profile_path, created_at="2026-01-01T00:02:00+00:00")

    assert result == {"run_id": run_id, "stored": 4, "profile_updates": ["country", "sensitive_profile.gender"]}
    rows = connection.execute("SELECT field_id, answer_json, persistence, note FROM application_review_annotations ORDER BY field_id").fetchall()
    assert [dict(row) for row in rows] == [
        {"field_id": "country", "answer_json": '"United States"', "persistence": "always", "note": None},
        {"field_id": "custom", "answer_json": None, "persistence": "never", "note": "Manual only"},
        {"field_id": "gender", "answer_json": '"Male"', "persistence": "always", "note": None},
        {"field_id": "hispanic_ethnicity", "answer_json": '"No"', "persistence": "once", "note": None},
    ]
    profile = json.loads(profile_path.read_text())
    assert profile["country"] == "United States"
    assert "gender" not in profile
    assert "hispanic_ethnicity" not in profile
    assert profile["sensitive_profile"]["answers"]["gender"] == {"value": "Male", "persistence": "always"}



def test_apply_review_annotations_save_always_answers_under_canonical_profile_keys(tmp_path) -> None:
    connection = memory_db()
    upsert_job(connection, {"id": "ts-1", "job_title": "Software Engineer", "url": "https://example.com/apply"})
    job_id = connection.execute("SELECT id FROM jobs WHERE theirstack_job_id = 'ts-1'").fetchone()["id"]
    run_id = connection.execute(
        """
        INSERT INTO application_runs (job_id, status, reason, started_at, actions_json)
        VALUES (?, 'needs_review', 'resolver_sensitive_field', '2026-01-01T00:00:00+00:00', '[]')
        """,
        (job_id,),
    ).lastrowid
    connection.execute(
        """
        INSERT INTO application_pages (run_id, page_index, url, snapshot_json, resolver_json, created_at)
        VALUES (?, 0, 'https://example.com/apply', ?, NULL, '2026-01-01T00:00:30+00:00')
        """,
        (
            run_id,
            json.dumps(
                {
                    "url": "https://example.com/apply",
                    "fields": [
                        {"id": "demographic_race_select", "kind": "typeahead", "label": "Please identify your race", "required": False, "options": [], "value": None, "disabled": False, "visible": True, "frame": None, "selector": "#race"},
                        {"id": "country_dropdown_proxy", "kind": "typeahead", "label": "Country", "required": True, "options": [], "value": None, "disabled": False, "visible": True, "frame": None, "selector": "#country"},
                    ],
                    "buttons": [],
                    "errors": [],
                    "blockers": [],
                    "metadata": {},
                }
            ),
        ),
    )
    profile_path = tmp_path / "profile.json"
    profile_path.write_text("{}\n")
    annotation_path = tmp_path / "annotations.json"
    annotation_path.write_text(
        json.dumps(
            {
                "run_id": run_id,
                "decisions": [
                    {"field_id": "demographic_race_select", "answer": "White", "persistence": "always"},
                    {"field_id": "country_dropdown_proxy", "answer": "United States", "persistence": "always"},
                ],
            }
        )
    )

    result = jobs_module.apply_review_annotations(connection, annotation_path, profile_path=profile_path)

    assert result["profile_updates"] == ["country", "sensitive_profile.race"]
    profile = json.loads(profile_path.read_text())
    assert profile["country"] == "United States"
    assert "country_dropdown_proxy" not in profile
    assert "demographic_race_select" not in profile
    assert profile["sensitive_profile"]["answers"]["race"] == {"value": "White", "persistence": "always"}

def test_apply_rerun_from_review_replays_once_annotations_for_same_job(monkeypatch, tmp_path, capsys) -> None:
    db_path = tmp_path / "jobs.sqlite3"
    connection = jobs_module.connect(db_path)
    initialize_database(connection)
    upsert_job(connection, {"id": "ts-1", "job_title": "Software Engineer", "url": "https://example.com/apply"})
    job_id = connection.execute("SELECT id FROM jobs WHERE theirstack_job_id = 'ts-1'").fetchone()["id"]
    run_id = connection.execute(
        """
        INSERT INTO application_runs (job_id, status, reason, started_at, actions_json)
        VALUES (?, 'needs_review', 'resolver_sensitive_field', '2026-01-01T00:00:00+00:00', '[]')
        """,
        (job_id,),
    ).lastrowid
    connection.execute(
        """
        INSERT INTO application_pages (run_id, page_index, url, snapshot_json, resolver_json, created_at)
        VALUES (?, 0, 'https://example.com/apply', ?, NULL, '2026-01-01T00:00:30+00:00')
        """,
        (
            run_id,
            json.dumps(
                {
                    "url": "https://example.com/apply",
                    "fields": [
                        {"id": "gender", "kind": "typeahead", "label": "Gender", "required": False, "options": [], "value": None, "disabled": False, "visible": True, "frame": None, "selector": "#gender"},
                        {"id": "hispanic_ethnicity", "kind": "typeahead", "label": "Are you Hispanic/Latino?", "required": False, "options": [], "value": None, "disabled": False, "visible": True, "frame": None, "selector": "#hispanic_ethnicity"},
                    ],
                    "buttons": [],
                    "errors": [],
                    "blockers": [],
                    "metadata": {},
                }
            ),
        ),
    )
    connection.commit()
    connection.close()
    profile_path = tmp_path / "profile.json"
    profile_path.write_text(json.dumps({"email": "ian@example.com"}) + "\n")
    annotation_path = tmp_path / "annotations.json"
    annotation_path.write_text(
        json.dumps(
            {
                "run_id": run_id,
                "decisions": [
                    {"field_id": "gender", "answer": "Male", "persistence": "always"},
                    {"field_id": "hispanic_ethnicity", "answer": "No", "persistence": "once"},
                ],
            }
        )
    )
    seen: dict[str, object] = {}

    def fake_run_review_job_with_playwright(connection, *, job_id, profile, now, max_pages, headed, manual_handoff, use_llm, block_linkedin_jobs):
        seen["job_id"] = job_id
        seen["facts"] = dict(profile.facts)
        seen["max_pages"] = max_pages
        seen["headed"] = headed
        seen["manual_handoff"] = manual_handoff
        seen["use_llm"] = use_llm
        seen["block_linkedin_jobs"] = block_linkedin_jobs
        return {"attempted": 1, "dry_run_ready": 1, "needs_review": 0, "blocked": 0, "failed": 0, "run_ids": [99]}

    monkeypatch.setenv("JOB_SYNC_DB_PATH", str(db_path))
    monkeypatch.setattr(jobs_module, "run_review_job_with_playwright", fake_run_review_job_with_playwright)

    jobs_module.main(["apply-rerun-from-review", "--input", str(annotation_path), "--profile-json", str(profile_path), "--max-pages", "4", "--no-llm"])

    assert seen["job_id"] == job_id
    assert seen["max_pages"] == 4
    assert seen["headed"] is False
    assert seen["manual_handoff"] is False
    assert seen["use_llm"] is False
    assert seen["block_linkedin_jobs"] is False
    assert seen["facts"]["email"] == "ian@example.com"
    assert seen["facts"]["gender"] == "Male"
    assert seen["facts"]["hispanic_ethnicity"] == "No"
    assert json.loads(capsys.readouterr().out)["run_ids"] == [99]
