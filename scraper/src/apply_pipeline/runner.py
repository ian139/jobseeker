from __future__ import annotations

import json
import sqlite3
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Protocol
from urllib.parse import urlsplit

from .backlog import job_application_url, next_backlog_jobs
from .contracts import ExecutorActionRecord, StepStatus
from .executor import ActionTarget, execute_actions_with_records
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
RESUME_SUMMARY_MAX_CHARS = 8000
SKILLS_MAX_CHARS = 2000
EMAIL_RE = re.compile(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", re.IGNORECASE)
PHONE_RE = re.compile(r"(?:\+?1[\s.-]?)?(?:\(?\d{3}\)?[\s.-]?)\d{3}[\s.-]?\d{4}")
GITHUB_RE = re.compile(r"(?:https?://)?(?:www\.)?github\.com/[A-Za-z0-9_.-]+", re.IGNORECASE)
US_STATE_RE = re.compile(r"\b(A[LKZR]|C[AOT]|D[CE]|FL|GA|HI|I[ADLN]|K[SY]|LA|M[ADEHINOPST]|N[CDEHJMVY]|O[HKR]|PA|RI|S[CD]|T[NX]|UT|V[AIT]|W[AIVY])\b")
COMMON_RESUME_SKILLS = (
    "Python",
    "Java",
    "C++",
    "JavaScript",
    "TypeScript",
    "React",
    "SQL",
    "SQLite",
    "PostgreSQL",
    "Playwright",
    "Docker",
    "Git",
    ".NET",
    "Razor",
    "CI/CD",
)


def normalized_name(value: str) -> str:
    return value.title() if value.isupper() else value




def extract_name_from_resume(resume_text: str) -> tuple[str, str, str] | None:
    for line in resume_text.splitlines()[:8]:
        candidate = " ".join(line.split())
        if not candidate or "@" in candidate or "http" in candidate.lower() or any(char.isdigit() for char in candidate):
            continue
        parts = candidate.split()
        if len(parts) >= 2:
            full_name = normalized_name(candidate)
            name_parts = full_name.split()
            return full_name, name_parts[0], name_parts[-1]
    return None


def extract_contact_facts(resume_text: str) -> dict[str, str]:
    facts: dict[str, str] = {}
    name = extract_name_from_resume(resume_text)
    if name:
        facts["full_name"], facts["first_name"], facts["last_name"] = name
    email = EMAIL_RE.search(resume_text)
    if email:
        facts["email"] = email.group(0)
    phone = PHONE_RE.search(resume_text)
    if phone:
        facts["phone"] = phone.group(0)
        facts.setdefault("country", "United States")
    github = GITHUB_RE.search(resume_text)
    if github:
        handle = github.group(0)
        facts["github"] = handle if handle.startswith("http") else f"https://{handle}"
    if "country" not in facts and US_STATE_RE.search(resume_text):
        facts["country"] = "United States"
    return facts




def normalized_resume_text(value: str) -> str:
    return "\n".join(line.strip() for line in value.replace("\x00", "").splitlines() if line.strip())


def extract_pdf_text(path: Path) -> str:
    try:
        from pypdf import PdfReader
    except ImportError as exc:
        raise RuntimeError("Install the live dependency group to extract PDF resume text: python -m pip install -e '.[live]'") from exc
    reader = PdfReader(str(path))
    return "\n".join(page.extract_text() or "" for page in reader.pages)


def extract_resume_text(path: str | Path) -> str:
    resume_path = Path(path).expanduser()
    if resume_path.suffix.casefold() == ".pdf":
        return normalized_resume_text(extract_pdf_text(resume_path))
    return normalized_resume_text(resume_path.read_text(errors="ignore"))


def extract_skills_text(resume_text: str) -> str:
    match = re.search(r"(?ims)^skills?\s*(?:\n|:)(.*?)(?=^\s*[A-Z][A-Za-z /&-]{2,}\s*(?:\n|:)|\Z)", resume_text)
    if match:
        return " ".join(match.group(1).split())[:SKILLS_MAX_CHARS]
    found = [skill for skill in COMMON_RESUME_SKILLS if re.search(rf"(?<![A-Za-z0-9+#.]){re.escape(skill)}(?![A-Za-z0-9+#.])", resume_text, re.IGNORECASE)]
    return ", ".join(dict.fromkeys(found))[:SKILLS_MAX_CHARS]


def resume_facts_from_path(path: str | Path) -> dict[str, str]:
    text = extract_resume_text(path)
    if not text:
        return {}
    facts = {**extract_contact_facts(text), "resume_summary": text[:RESUME_SUMMARY_MAX_CHARS]}
    skills = extract_skills_text(text)
    if skills:
        facts["skills"] = skills
    return facts


LINKEDIN_HOST_SUFFIX = "linkedin.com"


def is_linkedin_url(value: str) -> bool:
    host = urlsplit(value).netloc.lower().split("@")[-1].split(":")[0]
    return host == LINKEDIN_HOST_SUFFIX or host.endswith(f".{LINKEDIN_HOST_SUFFIX}")




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
        self._field_selectors: dict[str, str] = {}
        self._button_selectors: dict[str, str] = {}
        self._field_frames: dict[str, str] = {}
        self._button_frames: dict[str, str] = {}

    def set_selectors(
        self,
        field_selectors: dict[str, str],
        button_selectors: dict[str, str],
        field_frames: dict[str, str] | None = None,
        button_frames: dict[str, str] | None = None,
    ) -> None:
        self._field_selectors = dict(field_selectors)
        self._button_selectors = dict(button_selectors)
        self._field_frames = dict(field_frames or {})
        self._button_frames = dict(button_frames or {})

    def _scope_for_target(self, target_id: str) -> Any:
        frame_name = self._field_frames.get(target_id) or self._button_frames.get(target_id)
        if not frame_name or not frame_name.startswith("frame_"):
            return self.page
        try:
            frame_index = int(frame_name.removeprefix("frame_"))
        except ValueError:
            return self.page
        frames = list(getattr(self.page, "frames", []) or [])
        if 0 <= frame_index < len(frames):
            return frames[frame_index]
        return self.page

    def _locator(self, target_id: str) -> Any:
        last_error: Exception | None = None
        observer_selector = self._field_selectors.get(target_id) or self._button_selectors.get(target_id)
        selectors_to_try: list[str] = []
        if observer_selector:
            selectors_to_try.append(observer_selector)
        selectors_to_try.extend(_target_selectors(target_id))
        scope = self._scope_for_target(target_id)
        for selector in selectors_to_try:
            try:
                base = scope.locator(selector)  # type: ignore[attr-defined]
                first = getattr(base, "first", None)
                locator = first() if callable(first) else (first or base)
                if locator.count():
                    return locator
            except Exception as exc:
                last_error = exc
        if last_error is not None:
            raise ValueError(f"Unable to find page target: {target_id}") from last_error
        raise ValueError(f"Unable to find page target: {target_id}")

    def fill(self, target_id: str, value: str) -> None:
        locator = self._locator(target_id)
        locator.fill(value)
        role = locator.get_attribute("role")
        autocomplete = locator.get_attribute("aria-autocomplete")
        if role == "combobox" or autocomplete == "list":
            locator.press("Enter")

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
        locator = self._locator(target_id)
        expect_navigation = getattr(self.page, "expect_navigation", None)
        if not callable(expect_navigation):
            locator.click()
            return
        try:
            with expect_navigation(wait_until="domcontentloaded", timeout=10000):
                locator.click()
        except Exception as exc:
            if "timeout" not in str(exc).lower():
                raise


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


def with_resume_facts(facts: dict[str, str], resume_path: str | None) -> dict[str, str]:
    if not resume_path or not Path(resume_path).expanduser().exists():
        return facts
    enriched = dict(facts)
    for key, value in resume_facts_from_path(resume_path).items():
        enriched.setdefault(key, value)
    return enriched


def sensitive_profile_facts(raw: dict[str, object]) -> dict[str, str]:
    sensitive_profile = raw.get("sensitive_profile")
    if not isinstance(sensitive_profile, dict):
        return {}
    answers = sensitive_profile.get("answers")
    if not isinstance(answers, dict):
        return {}
    facts: dict[str, str] = {}
    for key, item in answers.items():
        if not isinstance(item, dict) or item.get("persistence") != "always":
            continue
        value = item.get("value")
        if value is not None and str(value).strip():
            facts[str(key)] = str(value)
    return facts


PROTECTED_PROFILE_FACT_KEYS = {
    "application_attestation",
    "disability_status",
    "gender",
    "hispanic_ethnicity",
    "hispanic_latino",
    "race",
    "sponsorship",
    "work_authorization",
    "veteran_status",
}


def plain_profile_facts(raw: dict[str, object]) -> dict[str, str]:
    return {
        str(key): str(value)
        for key, value in raw.items()
        if key != "sensitive_profile"
        and str(key).lower() not in PROTECTED_PROFILE_FACT_KEYS
        and value is not None
        and str(value).strip()
    }



def load_applicant_profile(
    path: str | Path | None,
    *,
    resume_path: str | None = None,
    include_defaults: bool = True,
    agents_path: str | Path | None = None,
    exclude_facts: Iterable[str] = (),
) -> ApplicantProfile:
    base = default_applicant_profile(agents_path=agents_path) if include_defaults else ApplicantProfile(facts={}, resume_path=None)
    excluded = {str(key).strip().lower() for key in exclude_facts if str(key).strip()}
    selected_resume_path = resume_path or base.resume_path
    if path is None:
        facts = {key: value for key, value in base.facts.items() if key.lower() not in excluded}
        return ApplicantProfile(facts=with_resume_facts(facts, selected_resume_path), resume_path=selected_resume_path)
    raw = json.loads(Path(path).expanduser().read_text())
    if not isinstance(raw, dict):
        raise ValueError("Applicant profile JSON must be an object")
    facts = {**base.facts, **plain_profile_facts(raw), **sensitive_profile_facts(raw)}
    facts = {key: value for key, value in facts.items() if key.lower() not in excluded}
    return ApplicantProfile(facts=with_resume_facts(facts, selected_resume_path), resume_path=selected_resume_path)


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
    block_linkedin_jobs: bool = False,
    llm_enabled: bool = True,
) -> tuple[int, StepStatus]:
    if max_pages < 1:
        raise ValueError("max_pages must be at least 1")
    run_id = start_application_run(connection, job_id=int(job["id"]), started_at=now())
    final_status = StepStatus.FAILED
    action_records: list[ExecutorActionRecord] = []
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
        if block_linkedin_jobs and is_linkedin_url(url):
            final_status = StepStatus.BLOCKED
            finish_application_run(
                connection,
                run_id=run_id,
                status=final_status,
                reason=f"LinkedIn job URL blocked by --block-linkedin-jobs: {url}",
                finished_at=now(),
                final_url=url,
                actions=[],
            )
            return run_id, final_status


        page.goto(url, wait_until="domcontentloaded")
        try:
            page.wait_for_load_state("networkidle")
        except Exception:
            pass
        for page_index in range(max_pages):
            snapshot = observe_page(page)
            if hasattr(action_target, "set_selectors"):
                field_selectors = {field.id: field.selector for field in snapshot.fields if field.selector}
                button_selectors = {button.id: button.selector for button in snapshot.buttons if button.selector}
                field_frames = {field.id: field.frame for field in snapshot.fields if field.frame}
                button_frames = {button.id: button.frame for button in snapshot.buttons if button.frame}
                action_target.set_selectors(field_selectors, button_selectors, field_frames=field_frames, button_frames=button_frames)
            resolved = resolve_snapshot(
                snapshot,
                facts=profile.facts,
                resume_path=profile.resume_path,
                job_description=job_description_from_row(job),
                llm_client=llm_client,
                llm_enabled=llm_enabled,
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
                if decision.status in {StepStatus.DRY_RUN_READY, StepStatus.NEEDS_REVIEW}:
                    _executed, records = execute_actions_with_records(action_target, decision, resume_path=profile.resume_path)
                    action_records.extend(records)
                    failed_record = next((record for record in records if record.status == "failed"), None)
                    if failed_record is not None:
                        finish_application_run(
                            connection,
                            run_id=run_id,
                            status=StepStatus.FAILED,
                            reason=failed_record.reason or "executor_action_failed",
                            finished_at=now(),
                            final_url=snapshot.url,
                            actions=action_records,
                        )
                        return run_id, StepStatus.FAILED
                finish_application_run(
                    connection,
                    run_id=run_id,
                    status=decision.status,
                    reason=decision.reason,
                    finished_at=now(),
                    final_url=snapshot.url,
                    actions=action_records,
                )
                return run_id, decision.status

            _executed, records = execute_actions_with_records(action_target, decision, resume_path=profile.resume_path)
            action_records.extend(records)
            failed_record = next((record for record in records if record.status == "failed"), None)
            if failed_record is not None:
                finish_application_run(
                    connection,
                    run_id=run_id,
                    status=StepStatus.FAILED,
                    reason=failed_record.reason or "executor_action_failed",
                    finished_at=now(),
                    final_url=snapshot.url,
                    actions=action_records,
                )
                return run_id, StepStatus.FAILED
            page.wait_for_load_state("domcontentloaded")

        final_status = StepStatus.NEEDS_REVIEW
        finish_application_run(
            connection,
            run_id=run_id,
            status=final_status,
            reason=f"max pages reached without final submit boundary: {max_pages}",
            finished_at=now(),
            final_url=str(getattr(page, "url", None) or ""),
            actions=action_records,
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
            actions=action_records,
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
    block_linkedin_jobs: bool = False,
    llm_enabled: bool = True,
) -> dict[str, int | list[int]]:
    summary = ApplicationRunSummary()
    try:
        while summary.attempted < limit:
            jobs = next_backlog_jobs(connection, limit=max(limit * 5, 10))
            if not jobs:
                break
            saw_unattempted_job = False
            for job in jobs:
                url = job_application_url(job)
                if block_linkedin_jobs and url and is_linkedin_url(url):
                    run_id = start_application_run(connection, job_id=int(job["id"]), started_at=now())
                    status = StepStatus.BLOCKED
                    finish_application_run(
                        connection,
                        run_id=run_id,
                        status=status,
                        reason=f"LinkedIn job URL blocked by --block-linkedin-jobs: {url}",
                        finished_at=now(),
                        final_url=url,
                        actions=[],
                    )
                    summary.run_ids.append(run_id)
                    summary.record(status)
                    saw_unattempted_job = True
                    continue
                if summary.attempted >= limit:
                    break
                summary.attempted += 1
                saw_unattempted_job = True
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
                    block_linkedin_jobs=block_linkedin_jobs,
                    llm_enabled=llm_enabled,
                )
                summary.run_ids.append(run_id)
                summary.record(status)
                if handoff_callback is not None and status in HANDOFF_STATUSES:
                    handoff_callback(run_id, status, page)
            if not saw_unattempted_job:
                break
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
    block_linkedin_jobs: bool = False,
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
            llm_enabled=use_llm,
            block_linkedin_jobs=block_linkedin_jobs,
        )
