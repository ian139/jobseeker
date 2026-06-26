from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Protocol

from .backlog import job_application_url, next_backlog_jobs
from .contracts import ExecutorAction, StepStatus
from .executor import ActionTarget, execute_actions
from .llm import LLMAnswerClient, ollama_cloud_client_from_env
from .observer import observe_page
from .policy import plan_guarded_actions
from .resolver import resolve_snapshot
from .runs import finish_application_run, record_application_page, start_application_run

APPLICANT_REFERENCE_HEADER = "## Applicant reference"
DEFAULT_RESUME_LABEL = "Resume file:"
DEFAULT_LINKEDIN_LABEL = "LinkedIn:"
DEFAULT_PERSONAL_SITE_LABEL = "Personal site:"
HANDOFF_STATUSES = frozenset({StepStatus.DRY_RUN_READY, StepStatus.NEEDS_REVIEW, StepStatus.BLOCKED, StepStatus.FAILED})


class PageSession(Protocol):
    url: str

    def goto(self, url: str, *, wait_until: str = "domcontentloaded") -> None: ...
    def content(self) -> str: ...
    def wait_for_load_state(self, state: str = "domcontentloaded") -> None: ...


class BrowserSession(Protocol):
    def new_page(self) -> PageSession: ...
    def close(self) -> None: ...


class ActionTargetFactory(Protocol):
    def __call__(self, page: PageSession) -> ActionTarget: ...


ManualHandoff = Callable[[int, StepStatus, PageSession], None]


def _css_attr(name: str, value: str) -> str:
    return f"[{name}={json.dumps(value)}]"


def _target_selectors(target_id: str) -> tuple[str, ...]:
    return (f"#{target_id}", _css_attr("id", target_id), _css_attr("name", target_id), _css_attr("aria-label", target_id))


class PlaywrightActionTarget:
    def __init__(self, page: PageSession) -> None:
        self.page = page

    def _locator(self, target_id: str) -> Any:
        last_error: Exception | None = None
        for selector in _target_selectors(target_id):
            try:
                locator = self.page.locator(selector).first()  # type: ignore[attr-defined]
                if locator.count():
                    return locator
            except Exception as exc:
                last_error = exc
        if last_error is not None:
            raise ValueError(f"Unable to find page target: {target_id}") from last_error
        raise ValueError(f"Unable to find page target: {target_id}")

    def fill(self, target_id: str, value: str) -> None:
        self._locator(target_id).fill(value)

    def select(self, target_id: str, value: str | list[str]) -> None:
        self._locator(target_id).select_option(value)

    def check(self, target_id: str, value: bool | str) -> None:
        locator = self._locator(target_id)
        if isinstance(value, bool):
            locator.set_checked(value)
        else:
            locator.check()

    def upload(self, target_id: str, path: str) -> None:
        self._locator(target_id).set_input_files(str(Path(path).expanduser()))

    def click(self, target_id: str) -> None:
        self._locator(target_id).click()


@dataclass(frozen=True)
class ApplicantProfile:
    facts: dict[str, str]
    resume_path: str | None = None


def find_agents_file(start: Path | None = None) -> Path | None:
    candidates: list[Path] = []
    if start is not None:
        candidates.append(start.expanduser().resolve())
    candidates.append(Path.cwd().resolve())
    candidates.append(Path(__file__).resolve())
    for candidate in candidates:
        directory = candidate if candidate.is_dir() else candidate.parent
        for parent in (directory, *directory.parents):
            agents_path = parent / "AGENTS.md"
            if agents_path.exists():
                return agents_path
    return None


def clean_reference_value(value: str) -> str:
    return value.strip().strip("`")


def parse_applicant_reference(agents_path: Path) -> ApplicantProfile:
    facts: dict[str, str] = {}
    resume_value: str | None = None
    in_section = False
    for line in agents_path.read_text().splitlines():
        stripped = line.strip()
        if stripped == APPLICANT_REFERENCE_HEADER:
            in_section = True
            continue
        if in_section and stripped.startswith("## "):
            break
        if not in_section or not stripped.startswith("- "):
            continue
        item = stripped[2:].strip()
        if item.startswith(DEFAULT_RESUME_LABEL):
            resume_value = clean_reference_value(item.removeprefix(DEFAULT_RESUME_LABEL))
        elif item.startswith(DEFAULT_LINKEDIN_LABEL):
            value = clean_reference_value(item.removeprefix(DEFAULT_LINKEDIN_LABEL))
            if value:
                facts["linkedin"] = value
        elif item.startswith(DEFAULT_PERSONAL_SITE_LABEL):
            value = clean_reference_value(item.removeprefix(DEFAULT_PERSONAL_SITE_LABEL))
            if value:
                facts["portfolio"] = value
                facts["website"] = value
                facts["personal_site"] = value
    resume_path = None
    if resume_value:
        raw_resume_path = Path(resume_value).expanduser()
        resume_path = str(raw_resume_path if raw_resume_path.is_absolute() else agents_path.parent / raw_resume_path)
    return ApplicantProfile(facts=facts, resume_path=resume_path)


def default_applicant_profile(*, agents_path: str | Path | None = None) -> ApplicantProfile:
    resolved_agents_path = Path(agents_path).expanduser().resolve() if agents_path is not None else find_agents_file()
    if resolved_agents_path is None:
        return ApplicantProfile(facts={}, resume_path=None)
    return parse_applicant_reference(resolved_agents_path)


def load_applicant_profile(
    path: str | Path | None,
    *,
    resume_path: str | None = None,
    include_defaults: bool = True,
    agents_path: str | Path | None = None,
) -> ApplicantProfile:
    base = default_applicant_profile(agents_path=agents_path) if include_defaults else ApplicantProfile(facts={}, resume_path=None)
    if path is None:
        return ApplicantProfile(facts=base.facts, resume_path=resume_path or base.resume_path)
    raw = json.loads(Path(path).expanduser().read_text())
    if not isinstance(raw, dict):
        raise ValueError("Applicant profile JSON must be an object")
    facts = {**base.facts, **{str(key): str(value) for key, value in raw.items() if value is not None and str(value).strip()}}
    return ApplicantProfile(facts=facts, resume_path=resume_path or base.resume_path)


@dataclass
class ApplicationRunSummary:
    attempted: int = 0
    dry_run_ready: int = 0
    needs_review: int = 0
    blocked: int = 0
    failed: int = 0
    run_ids: list[int] = field(default_factory=list)

    def record(self, status: StepStatus | str) -> None:
        value = status.value if isinstance(status, StepStatus) else str(status)
        if value == StepStatus.DRY_RUN_READY.value:
            self.dry_run_ready += 1
        elif value == StepStatus.NEEDS_REVIEW.value:
            self.needs_review += 1
        elif value == StepStatus.BLOCKED.value:
            self.blocked += 1
        elif value == StepStatus.FAILED.value:
            self.failed += 1

    def asdict(self) -> dict[str, int | list[int]]:
        return {
            "attempted": self.attempted,
            "dry_run_ready": self.dry_run_ready,
            "needs_review": self.needs_review,
            "blocked": self.blocked,
            "failed": self.failed,
            "run_ids": self.run_ids,
        }


def job_description_from_row(job: sqlite3.Row) -> str | None:
    raw_json = job["raw_json"] if "raw_json" in job.keys() else None
    if not raw_json:
        return None
    try:
        raw = json.loads(raw_json)
    except (TypeError, json.JSONDecodeError):
        return None
    for key in ("description", "job_description", "description_text"):
        value = raw.get(key)
        if isinstance(value, str) and value.strip():
            return value
    return None


def run_application_once(
    connection: sqlite3.Connection,
    *,
    job: sqlite3.Row,
    page: PageSession,
    action_target: ActionTarget,
    profile: ApplicantProfile,
    now: Any,
    max_pages: int = 6,
    llm_client: LLMAnswerClient | None = None,
) -> tuple[int, StepStatus]:
    if max_pages < 1:
        raise ValueError("max_pages must be at least 1")
    run_id = start_application_run(connection, job_id=int(job["id"]), started_at=now())
    final_status = StepStatus.FAILED
    actions: list[ExecutorAction] = []
    try:
        url = job_application_url(job)
        if not url:
            final_status = StepStatus.BLOCKED
            finish_application_run(
                connection,
                run_id=run_id,
                status=final_status,
                reason="job has no application URL",
                finished_at=now(),
                actions=[],
            )
            return run_id, final_status

        page.goto(url, wait_until="domcontentloaded")
        for page_index in range(max_pages):
            snapshot = observe_page(page)
            resolved = resolve_snapshot(
                snapshot,
                facts=profile.facts,
                resume_path=profile.resume_path,
                job_description=job_description_from_row(job),
                llm_client=llm_client,
            )
            decision = plan_guarded_actions(snapshot, resolved)
            record_application_page(
                connection,
                run_id=run_id,
                page_index=page_index,
                url=snapshot.url,
                snapshot=snapshot,
                resolver_output=resolved,
                created_at=now(),
            )
            if decision.status != StepStatus.CONTINUE:
                if decision.status == StepStatus.DRY_RUN_READY:
                    actions.extend(execute_actions(action_target, decision, resume_path=profile.resume_path))
                finish_application_run(
                    connection,
                    run_id=run_id,
                    status=decision.status,
                    reason=decision.reason,
                    finished_at=now(),
                    final_url=snapshot.url,
                    actions=actions,
                )
                return run_id, decision.status

            executed = execute_actions(action_target, decision, resume_path=profile.resume_path)
            actions.extend(executed)
            page.wait_for_load_state("domcontentloaded")

        final_status = StepStatus.NEEDS_REVIEW
        finish_application_run(
            connection,
            run_id=run_id,
            status=final_status,
            reason=f"max pages reached without final submit boundary: {max_pages}",
            finished_at=now(),
            final_url=str(getattr(page, "url", None) or ""),
            actions=actions,
        )
        return run_id, final_status
    except Exception as exc:
        finish_application_run(
            connection,
            run_id=run_id,
            status=StepStatus.FAILED,
            reason=str(exc),
            finished_at=now(),
            final_url=str(getattr(page, "url", None) or ""),
            actions=actions,
        )
        return run_id, StepStatus.FAILED


def run_backlog_applications(
    connection: sqlite3.Connection,
    *,
    browser: BrowserSession,
    action_target_factory: ActionTargetFactory,
    profile: ApplicantProfile,
    now: Any,
    limit: int = 1,
    max_pages: int = 6,
    handoff_callback: ManualHandoff | None = None,
    close_browser: bool = True,
    llm_client: LLMAnswerClient | None = None,
) -> dict[str, int | list[int]]:
    summary = ApplicationRunSummary()
    try:
        for job in next_backlog_jobs(connection, limit=limit):
            summary.attempted += 1
            page = browser.new_page()
            run_id, status = run_application_once(
                connection,
                job=job,
                page=page,
                action_target=action_target_factory(page),
                profile=profile,
                now=now,
                max_pages=max_pages,
                llm_client=llm_client,
            )
            summary.run_ids.append(run_id)
            summary.record(status)
            if handoff_callback is not None and status in HANDOFF_STATUSES:
                handoff_callback(run_id, status, page)
    finally:
        if close_browser:
            browser.close()
    return summary.asdict()


def run_backlog_with_playwright(
    connection: sqlite3.Connection,
    *,
    profile: ApplicantProfile,
    now: Any,
    limit: int = 1,
    max_pages: int = 6,
    headed: bool = False,
    manual_handoff: bool = False,
    use_llm: bool = True,
) -> dict[str, int | list[int]]:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise RuntimeError("Install Playwright to use live apply dry runs") from exc

    def prompt_for_manual_handoff(run_id: int, status: StepStatus, page: PageSession) -> None:
        print(f"Manual handoff for run {run_id}: {status.value} at {getattr(page, 'url', 'about:blank')}")
        print("Review/edit the browser page now. Press Enter only to end inspection; it will not resume automation or submit, and the Playwright browser context will close.")
        input()

    llm_client = ollama_cloud_client_from_env() if use_llm else None

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=not (headed or manual_handoff))
        return run_backlog_applications(
            connection,
            browser=browser,
            action_target_factory=PlaywrightActionTarget,
            profile=profile,
            now=now,
            limit=limit,
            max_pages=max_pages,
            handoff_callback=prompt_for_manual_handoff if manual_handoff else None,
            close_browser=not manual_handoff,
            llm_client=llm_client,
        )
