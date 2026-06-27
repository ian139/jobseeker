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


def test_profile_from_resume_command_writes_resume_profile(tmp_path, capsys) -> None:
    resume = tmp_path / "resume.txt"
    output = tmp_path / "profile.json"
    resume.write_text("Ian Rapko\n\nSkills:\nPython, Playwright\n\nExperience:\nAutomation")

    jobs_module.main(["profile-from-resume", "--resume", str(resume), "--output", str(output)])

    payload = json.loads(output.read_text())
    assert "Ian Rapko" in payload["resume_summary"]
    assert payload["skills"] == "Python, Playwright"
    assert "Wrote" in capsys.readouterr().out
