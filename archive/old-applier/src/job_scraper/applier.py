from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Literal, Protocol

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import Locator, Page, TimeoutError as PlaywrightTimeoutError, sync_playwright

from job_scraper.resume import ResumeProfile, load_resume_profile
from job_scraper.storage import ApplicationAttemptRecord, ApplicationRecord, JobStorage

BrowserApplyStatus = Literal["prepared", "submitted", "blocked", "failed"]


@dataclass(frozen=True)
class BrowserApplyOutcome:
    target_url: str
    status: BrowserApplyStatus
    submitted: bool
    message: str
    fields_filled: tuple[str, ...]
    resume_uploaded: bool


class BrowserApplier(Protocol):
    def apply(
        self,
        *,
        target_url: str,
        profile: ResumeProfile,
        resume_path: Path,
        submit: bool,
    ) -> BrowserApplyOutcome: ...


@dataclass(frozen=True)
class ApplyResult:
    application: ApplicationRecord
    attempt: ApplicationAttemptRecord


class PlaywrightBrowserApplier:
    def __init__(self, *, headless: bool = False, timeout_ms: int = 30000) -> None:
        self.headless = headless
        self.timeout_ms = timeout_ms

    def apply(
        self,
        *,
        target_url: str,
        profile: ResumeProfile,
        resume_path: Path,
        submit: bool,
    ) -> BrowserApplyOutcome:
        fields_filled: list[str] = []
        resume_uploaded = False
        upload_error = ""
        try:
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch(headless=self.headless)
                try:
                    context = browser.new_context()
                    page = context.new_page()
                    page.set_default_timeout(self.timeout_ms)
                    page.goto(target_url, wait_until="domcontentloaded")
                    _click_first_named(page, _APPLY_ACTION_RE, roles=("button", "link"))
                    try:
                        page.wait_for_load_state("domcontentloaded", timeout=self.timeout_ms)
                    except PlaywrightTimeoutError:
                        pass

                    if _visible_text_exists(page, _BLOCKED_TEXT_RE):
                        return BrowserApplyOutcome(
                            target_url=target_url,
                            status="blocked",
                            submitted=False,
                            message="Application page requires login or human verification",
                            fields_filled=tuple(fields_filled),
                            resume_uploaded=resume_uploaded,
                        )

                    values = _profile_field_values(profile)
                    for field_name, label_pattern, css_selector in _FIELD_MAPPINGS:
                        value = values[field_name]
                        if value and _fill_standard_field(page, label_pattern, css_selector, value):
                            fields_filled.append(field_name)

                    try:
                        file_input = page.locator("input[type='file']").first
                        file_input.set_input_files(str(resume_path))
                        resume_uploaded = True
                    except (PlaywrightError, PlaywrightTimeoutError) as exc:
                        upload_error = str(exc)[:200]

                    if not submit:
                        message = "Filled application form without submitting"
                        if upload_error:
                            message = f"{message}; resume upload failed: {upload_error}"
                        return BrowserApplyOutcome(
                            target_url=target_url,
                            status="prepared",
                            submitted=False,
                            message=message,
                            fields_filled=tuple(fields_filled),
                            resume_uploaded=resume_uploaded,
                        )

                    if _has_empty_required_fields(page):
                        message = "Application form still has required empty fields"
                        if upload_error:
                            message = f"{message}; resume upload failed: {upload_error}"
                        return BrowserApplyOutcome(
                            target_url=target_url,
                            status="blocked",
                            submitted=False,
                            message=message,
                            fields_filled=tuple(fields_filled),
                            resume_uploaded=resume_uploaded,
                        )

                    _click_first_named(page, _SUBMIT_ACTION_RE, roles=("button",))
                    try:
                        page.get_by_text(_CONFIRMATION_TEXT_RE).first.wait_for(state="visible", timeout=self.timeout_ms)
                        return BrowserApplyOutcome(
                            target_url=target_url,
                            status="submitted",
                            submitted=True,
                            message="Application submitted",
                            fields_filled=tuple(fields_filled),
                            resume_uploaded=resume_uploaded,
                        )
                    except PlaywrightTimeoutError:
                        message = "Submit click completed but no confirmation was detected"
                        if upload_error:
                            message = f"{message}; resume upload failed: {upload_error}"
                        return BrowserApplyOutcome(
                            target_url=target_url,
                            status="prepared",
                            submitted=False,
                            message=message,
                            fields_filled=tuple(fields_filled),
                            resume_uploaded=resume_uploaded,
                        )
                finally:
                    browser.close()
        except (PlaywrightError, PlaywrightTimeoutError) as exc:
            return BrowserApplyOutcome(
                target_url=target_url,
                status="failed",
                submitted=False,
                message=str(exc)[:500],
                fields_filled=tuple(fields_filled),
                resume_uploaded=resume_uploaded,
            )


def apply_to_job(
    storage: JobStorage,
    *,
    job_id: str,
    profile_path: Path,
    resume_path: Path | None = None,
    submit: bool = False,
    browser: BrowserApplier | None = None,
    headless: bool = False,
    timeout_ms: int = 30000,
) -> ApplyResult:
    job = storage.get_job(job_id)
    if job is None:
        raise ValueError(f"Unknown job id: {job_id}")

    target_url = job.final_url or job.url
    if not target_url:
        raise ValueError(f"Job has no application URL: {job_id}")

    profile = load_resume_profile(profile_path)
    if not profile.contact.email or not profile.contact.email.strip():
        raise ValueError("Application profile requires contact.email")

    application = storage.ensure_application(job_id)
    selected_resume_path = resume_path or (Path(application.resume_path) if application.resume_path else None)
    if selected_resume_path is None:
        raise ValueError("Application has no resume_path; run prepare-application or pass --resume-path")
    if not selected_resume_path.exists():
        raise ValueError(f"Resume file not found: {selected_resume_path}")

    applier = browser or PlaywrightBrowserApplier(headless=headless, timeout_ms=timeout_ms)
    outcome = applier.apply(target_url=target_url, profile=profile, resume_path=selected_resume_path, submit=submit)
    attempt = storage.record_application_attempt(
        job_id,
        target_url=outcome.target_url,
        status=outcome.status,
        submitted=outcome.submitted,
        message=outcome.message,
        fields_filled=outcome.fields_filled,
        resume_uploaded=outcome.resume_uploaded,
    )
    if outcome.status == "submitted":
        application = storage.update_application(
            job_id,
            status="applied",
            applied_at=date.today().isoformat(),
            resume_path=str(selected_resume_path),
        )
    return ApplyResult(application=application, attempt=attempt)


_APPLY_ACTION_RE = re.compile(r"^(apply|apply now|start application|continue|easy apply)$", re.IGNORECASE)
_BLOCKED_TEXT_RE = re.compile(r"captcha|verify you are human|sign in|log in|login", re.IGNORECASE)
_SUBMIT_ACTION_RE = re.compile(r"^(submit application|submit|send application|apply)$", re.IGNORECASE)
_CONFIRMATION_TEXT_RE = re.compile(r"thank you|application submitted|we received your application|success", re.IGNORECASE)
_FIELD_MAPPINGS = (
    ("first_name", re.compile(r"first name|given name", re.IGNORECASE), "input[name*='first' i], input[id*='first' i], input[name*='given' i], input[id*='given' i]"),
    ("last_name", re.compile(r"last name|family name|surname", re.IGNORECASE), "input[name*='last' i], input[id*='last' i], input[name*='surname' i], input[id*='surname' i]"),
    ("full_name", re.compile(r"full name|name", re.IGNORECASE), "input[name='name' i], input[id='name' i], input[name*='full' i], input[id*='full' i]"),
    ("email", re.compile(r"email|e-mail", re.IGNORECASE), "input[type='email'], input[name*='email' i], input[id*='email' i]"),
    ("phone", re.compile(r"phone|mobile|telephone", re.IGNORECASE), "input[type='tel'], input[name*='phone' i], input[id*='phone' i], input[name*='mobile' i], input[id*='mobile' i]"),
    ("location", re.compile(r"location|city|address", re.IGNORECASE), "input[name*='location' i], input[id*='location' i], input[name*='city' i], input[id*='city' i]"),
    ("linkedin_url", re.compile(r"linkedin|linked in", re.IGNORECASE), "input[name*='linkedin' i], input[id*='linkedin' i]"),
    ("portfolio_url", re.compile(r"website|portfolio|github|personal site", re.IGNORECASE), "input[name*='website' i], input[id*='website' i], input[name*='portfolio' i], input[id*='portfolio' i], input[name*='github' i], input[id*='github' i]"),
)


def _profile_field_values(profile: ResumeProfile) -> dict[str, str]:
    full_name = profile.name.strip()
    first_name, _, last_name = full_name.partition(" ")
    email = profile.contact.email.strip()
    phone = (profile.contact.phone or "").strip()
    location = (profile.contact.location or "").strip()
    linkedin_url = _first_matching_link(profile.contact.links, contains_linkedin=True)
    portfolio_url = _first_matching_link(profile.contact.links, contains_linkedin=False)
    return {
        "full_name": full_name,
        "first_name": first_name,
        "last_name": last_name,
        "email": email,
        "phone": phone,
        "location": location,
        "linkedin_url": linkedin_url,
        "portfolio_url": portfolio_url,
    }


def _first_matching_link(links: list[str], *, contains_linkedin: bool) -> str:
    for link in links:
        has_linkedin = "linkedin.com" in link.lower()
        if has_linkedin == contains_linkedin:
            return link.strip()
    return ""


def _click_first_named(page: Page, name: re.Pattern[str], *, roles: tuple[str, ...]) -> bool:
    for role in roles:
        locator = page.get_by_role(role, name=name)
        if _click_first_visible(locator):
            return True
    return False


def _click_first_visible(locator: Locator) -> bool:
    try:
        count = min(locator.count(), 10)
    except PlaywrightError:
        return False
    for index in range(count):
        candidate = locator.nth(index)
        try:
            if candidate.is_visible(timeout=500) and candidate.is_enabled(timeout=500):
                candidate.click()
                return True
        except PlaywrightError:
            continue
    return False


def _visible_text_exists(page: Page, pattern: re.Pattern[str]) -> bool:
    try:
        return page.get_by_text(pattern).first.is_visible(timeout=1000)
    except PlaywrightError:
        return False


def _fill_standard_field(page: Page, label_pattern: re.Pattern[str], css_selector: str, value: str) -> bool:
    locators = (
        page.get_by_label(label_pattern),
        page.get_by_placeholder(label_pattern),
        page.locator(css_selector),
    )
    return any(_fill_first_empty(locator, value) for locator in locators)


def _fill_first_empty(locator: Locator, value: str) -> bool:
    try:
        count = min(locator.count(), 10)
    except PlaywrightError:
        return False
    for index in range(count):
        candidate = locator.nth(index)
        try:
            if not candidate.is_visible(timeout=500) or not candidate.is_enabled(timeout=500):
                continue
            if _field_has_value(candidate):
                continue
            candidate.fill(value)
            return True
        except PlaywrightError:
            continue
    return False


def _field_has_value(locator: Locator) -> bool:
    try:
        return bool(locator.input_value(timeout=500).strip())
    except PlaywrightError:
        return False


def _has_empty_required_fields(page: Page) -> bool:
    missing = page.locator("input[required], textarea[required], select[required]").evaluate_all(
        """
        elements => elements.some(element => {
            const style = window.getComputedStyle(element);
            const visible = style && style.visibility !== 'hidden' && style.display !== 'none' && element.getClientRects().length > 0;
            if (!visible || element.disabled) return false;
            if ((element.type === 'checkbox' || element.type === 'radio') && !element.checked) return true;
            return !String(element.value || '').trim();
        })
        """
    )
    return bool(missing)
