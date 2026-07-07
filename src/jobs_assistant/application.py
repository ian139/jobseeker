from __future__ import annotations

import json
import asyncio
import base64
import os
import re
from dataclasses import dataclass, field, replace
from html.parser import HTMLParser
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any
from uuid import uuid4

import httpx

from .backlog import job_application_url
from .db import decode_json, encode_json, utc_now

BLOCKED_STATUS = "blocked"
MANUAL_STATUS = "manual"
COMPLETED_STATUS = "completed"
FAILED_STATUS = "failed"

UNSAFE_RE = re.compile(r"\b(submit|final|captcha|assessment|payment|credit card|ssn|social security|work authorization|authorization|visa|sponsorship|eeo|gender|race|ethnicity|demographic|veteran|disability|signature|consent|legal|clearance|salary|availability|identity|sign in|login|log in|password)\b", re.I)
FINAL_RE = re.compile(r"\b(submit|send application|finish|complete application|final)\b", re.I)
DEFAULT_LLM_MODEL = "deepseek-v4-flash"
DEFAULT_LLM_THINK = "low"
RESUME_METADATA_KEYS = ("skills", "jobs", "research", "leadership", "education")



@dataclass(frozen=True)
class ObservedField:
    kind: str
    name: str | None
    label: str
    selector: str
    required: bool = False
    options: tuple[str, ...] = ()


@dataclass(frozen=True)
class ObservedButton:
    text: str
    selector: str
    safe: bool


@dataclass(frozen=True)
class PageObservation:
    url: str
    title: str | None
    fields: tuple[ObservedField, ...]
    buttons: tuple[ObservedButton, ...]
    errors: tuple[str, ...] = ()


@dataclass(frozen=True)
class FieldAnswer:
    selector: str
    value: str
    confidence: float
    reason: str = ""


@dataclass(frozen=True)
class AutofillPlan:
    answers: tuple[FieldAnswer, ...] = ()
    safe_button_selector: str | None = None
    status: str = MANUAL_STATUS
    reason: str = "needs review"
    skipped_fields: tuple[str, ...] = ()
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ActionEvidence:
    action: str
    selector: str
    status: str
    reason: str
    kind: str | None = None
    confidence: float | None = None
    value_length: int | None = None


GREENHOUSE_ITERATION_PATH: tuple[str, ...] = (
    "discover_queued_job",
    "observe_page",
    "resolve_profile_resume_and_llm_plan",
    "execute_guarded_non_final_actions",
    "persist_run_evidence",
    "human_review_manual_submit",
)


class _FormHTMLParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.fields: list[ObservedField] = []
        self.buttons: list[ObservedButton] = []
        self._select: dict[str, Any] | None = None
        self._textarea: dict[str, str] | None = None
        self._button: dict[str, Any] | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attr = {k: v or "" for k, v in attrs}
        if tag == "input":
            input_type = (attr.get("type") or "text").lower()
            if input_type in {"hidden", "submit", "button", "reset", "image"}:
                if input_type in {"submit", "button"}:
                    text = attr.get("value") or attr.get("aria-label") or input_type
                    selector = _selector(tag, attr)
                    self.buttons.append(ObservedButton(text, selector, _button_is_safe(input_type, text, selector, attr)))
                return
            self.fields.append(ObservedField(input_type, attr.get("name") or attr.get("id"), _label(attr), _selector(tag, attr), "required" in attr))
        elif tag == "textarea":
            self._textarea = attr
        elif tag == "select":
            self._select = {"attr": attr, "options": []}
        elif tag == "option" and self._select is not None:
            value = attr.get("value") or ""
            if value:
                self._select["options"].append(value)
        elif tag == "button":
            self._button = {"attr": attr, "text": attr.get("aria-label") or attr.get("name") or attr.get("value") or ""}
    def handle_data(self, data: str) -> None:
        if self._button is not None and not self._button["text"]:
            self._button["text"] = data.strip()

    def handle_endtag(self, tag: str) -> None:
        if tag == "button" and self._button is not None:
            attr = self._button["attr"]
            text = self._button["text"] or "button"
            button_type = (attr.get("type") or "submit").lower()
            selector = _selector("button", attr)
            self.buttons.append(ObservedButton(text, selector, _button_is_safe(button_type, text, selector, attr)))
            self._button = None
        elif tag == "textarea" and self._textarea is not None:
            attr = self._textarea
            self.fields.append(ObservedField("textarea", attr.get("name") or attr.get("id"), _label(attr), _selector("textarea", attr), "required" in attr))
            self._textarea = None
        elif tag == "select" and self._select is not None:
            attr = self._select["attr"]
            self.fields.append(ObservedField("select", attr.get("name") or attr.get("id"), _label(attr), _selector("select", attr), "required" in attr, tuple(self._select["options"])))
            self._select = None


def _label(attr: dict[str, str]) -> str:
    return attr.get("aria-label") or attr.get("placeholder") or attr.get("name") or attr.get("id") or "field"


def _selector(tag: str, attr: dict[str, str]) -> str:
    if attr.get("id"):
        return f"#{attr['id']}"
    if attr.get("name"):
        return f"{tag}[name='{attr['name']}']"
    return tag


def _button_is_safe(button_type: str, text: str, selector: str, attr: dict[str, str] | None = None) -> bool:
    if button_type == "submit" or selector in {"button", "input"}:
        return False
    haystack = " ".join(
        part
        for part in [
            text,
            selector,
            *((attr or {}).get(key, "") for key in ("name", "id", "value", "aria-label")),
        ]
        if part
    )
    searchable = haystack.replace("_", " ").replace("-", " ")
    return not FINAL_RE.search(searchable) and not UNSAFE_RE.search(searchable)


def observe_html(html: str, *, url: str = "", title: str | None = None) -> PageObservation:
    parser = _FormHTMLParser()
    parser.feed(html)
    errors = tuple(text for text in re.findall(r"(?:error|required|invalid)[^<]{0,80}", html, flags=re.I))
    return PageObservation(url=url, title=title, fields=tuple(parser.fields), buttons=tuple(parser.buttons), errors=errors)


def load_resume_context(resume_dir: str | Path = "resume") -> str:
    root = Path(resume_dir)
    if not root.exists():
        raise FileNotFoundError(f"resume directory not found: {root}")
    candidates = [
        path
        for path in root.iterdir()
        if path.is_file() and (path.name.lower().startswith("main_resume.") or path.name.lower() == "profile.json")
    ]
    for path in candidates:
        if path.suffix.lower() != ".pdf":
            return path.read_text(errors="ignore")[:12000]
    for path in candidates:
        if path.suffix.lower() == ".pdf":
            try:
                from pypdf import PdfReader
            except ImportError as exc:
                raise RuntimeError("pypdf is required to read resume PDFs") from exc
            reader = PdfReader(str(path))
            text = "\n".join(page.extract_text() or "" for page in reader.pages).strip()
            if not text:
                raise ValueError(f"resume PDF has no extractable text: {path}")
            return text[:12000]
    raise FileNotFoundError(f"no main_resume text/profile/PDF found in {root}")


def load_resume_metadata(resume_dir: str | Path = "resume") -> dict[str, Any]:
    path = Path(resume_dir) / "resume.json"
    if not path.exists():
        return {}
    payload = json.loads(path.read_text())
    if not isinstance(payload, dict):
        raise ValueError("resume.json must contain a JSON object")
    return {key: payload[key] for key in RESUME_METADATA_KEYS if key in payload}


def job_description_from_row(row: Any) -> str:
    description = row["description"] if hasattr(row, "keys") and "description" in row.keys() else None
    if isinstance(description, str) and description.strip():
        return description.strip()
    raw_json = row["raw_json"] if hasattr(row, "keys") and "raw_json" in row.keys() else None
    try:
        raw = decode_json(raw_json, {}) if isinstance(raw_json, str) else {}
    except (TypeError, ValueError, json.JSONDecodeError):
        return ""
    if not isinstance(raw, dict):
        return ""
    for key in ("description", "job_description", "job_text", "body", "html"):
        value = raw.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _field_action(field: ObservedField) -> str:
    if field.kind == "file":
        return "upload"
    if field.kind == "select":
        return "select"
    return "fill"


def _action_dict(action: ActionEvidence) -> dict[str, Any]:
    return {
        "action": action.action,
        "selector": action.selector,
        "status": action.status,
        "reason": action.reason,
        "kind": action.kind,
        "confidence": action.confidence,
        "value_length": action.value_length,
    }


def plan_action_evidence(observation: PageObservation, plan: AutofillPlan) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    field_by_selector = {field.selector: field for field in observation.fields}
    planned: list[ActionEvidence] = []
    rejected: list[ActionEvidence] = []
    for answer in plan.answers:
        field = field_by_selector.get(answer.selector)
        if field is None:
            rejected.append(ActionEvidence("field", answer.selector, "rejected", "selector was not observed", confidence=answer.confidence, value_length=len(answer.value)))
            continue
        if _field_is_sensitive(field):
            rejected.append(ActionEvidence(_field_action(field), answer.selector, "rejected", "field is sensitive/manual", field.kind, answer.confidence, len(answer.value)))
            continue
        planned.append(ActionEvidence(_field_action(field), answer.selector, "planned", answer.reason or "safe observed field", field.kind, answer.confidence, len(answer.value)))
    if plan.safe_button_selector:
        clicked = _safe_observed_button_selector(observation, plan.safe_button_selector)
        if clicked is None:
            rejected.append(ActionEvidence("click", plan.safe_button_selector, "rejected", "button is unsafe, generic, or unobserved"))
        else:
            planned.append(ActionEvidence("click", clicked, "planned", "safe observed navigation button", "button"))
    return ([_action_dict(action) for action in planned], [_action_dict(action) for action in rejected])


def _field_is_sensitive(field: ObservedField) -> bool:
    haystack = " ".join(part for part in [field.name, field.label, field.kind] if part)
    return bool(UNSAFE_RE.search(haystack))


def _safe_observed_button_selector(observation: PageObservation, selector: str | None) -> str | None:
    if not selector:
        return None
    for button in observation.buttons:
        if button.safe and button.selector == selector and _button_is_safe("button", button.text, button.selector):
            return selector
    return None


def unresolved_required_fields(observation: PageObservation, plan: AutofillPlan) -> tuple[str, ...]:
    answered_selectors = {answer.selector for answer in plan.answers if answer.value}
    missing: list[str] = []
    for field in observation.fields:
        if not field.required or field.selector in answered_selectors:
            continue
        missing.append(field.label or field.name or field.selector)
    return tuple(missing)


def resolve_with_llm(
    observation: PageObservation,
    *,
    job: dict[str, Any],
    resume_context: str,
    job_description: str | None = None,
    resume_metadata: dict[str, Any] | None = None,
    profile_context: dict[str, Any] | None = None,
    api_key: str | None = None,
    base_url: str | None = None,
    model: str | None = None,
) -> AutofillPlan:
    safe_fields = [field for field in observation.fields if not _field_is_sensitive(field)]
    safe_selectors = {field.selector for field in safe_fields}
    skipped = [field.label for field in observation.fields if _field_is_sensitive(field)]
    blocking_sensitive = [field.label for field in observation.fields if _field_is_sensitive(field) and field.required]
    if blocking_sensitive:
        return AutofillPlan(status=MANUAL_STATUS, reason="required sensitive/manual fields need review", skipped_fields=tuple(skipped), raw={"blocking_sensitive_fields": blocking_sensitive})
    if not safe_fields:
        return AutofillPlan(status=MANUAL_STATUS, reason="no safe answerable fields", skipped_fields=tuple(skipped))
    token = api_key or os.environ.get("OLLAMA_CLOUD_API_KEY") or os.environ.get("OLLAMA_API_KEY")
    if not token:
        return AutofillPlan(status=MANUAL_STATUS, reason="missing LLM API key", skipped_fields=tuple(skipped))
    endpoint = (base_url or os.environ.get("OLLAMA_CLOUD_BASE_URL") or "https://ollama.com").rstrip("/") + "/api/chat"
    selected_model = model or os.environ.get("OLLAMA_CLOUD_MODEL") or os.environ.get("DEEPSEEK_MODEL") or DEFAULT_LLM_MODEL
    selected_think = os.environ.get("OLLAMA_CLOUD_THINK") or os.environ.get("OLLAMA_CLOUD_REASONING") or DEFAULT_LLM_THINK
    prompt = {
        "instruction": "Return strict JSON: {answers:[{selector,value,confidence,reason}], safe_button_selector:null|string, status:'ready'|'manual', reason:string}. Only answer fields inferable from resume/profile/job. Never answer sensitive/legal/identity/CAPTCHA/assessment/sign-in/payment fields. Never choose final submit. Never invent file paths; resume uploads are deterministic and configured by the executor.",
        "job": job,
        "job_description": job_description or "",
        "resume_context": resume_context,
        "resume_metadata": resume_metadata or {},
        "profile_context": profile_context or {},
        "fields": [field.__dict__ for field in safe_fields],
        "buttons": [button.__dict__ for button in observation.buttons if button.safe],
        "skipped_fields": skipped,
    }
    response = httpx.post(endpoint, headers={"Authorization": f"Bearer {token}"}, json={"model": selected_model, "messages": [{"role": "user", "content": json.dumps(prompt)}], "think": selected_think, "stream": False}, timeout=60)
    response.raise_for_status()
    data = response.json()
    content = data.get("message", {}).get("content") or data.get("choices", [{}])[0].get("message", {}).get("content") or "{}"
    raw = json.loads(content)
    answers = tuple(
        FieldAnswer(str(item["selector"]), str(item["value"]), float(item.get("confidence", 0)), str(item.get("reason", "")))
        for item in raw.get("answers", [])
        if str(item.get("selector", "")) in safe_selectors and float(item.get("confidence", 0)) >= 0.7
    )
    requested_status = str(raw.get("status") or "").lower()
    if requested_status and requested_status != "ready":
        return AutofillPlan(answers=(), safe_button_selector=None, status=MANUAL_STATUS, reason=str(raw.get("reason") or requested_status), skipped_fields=tuple(skipped), raw=raw)
    status = "ready" if answers else MANUAL_STATUS
    return AutofillPlan(answers=answers, safe_button_selector=raw.get("safe_button_selector"), status=status, reason=str(raw.get("reason") or status), skipped_fields=tuple(skipped), raw=raw)


def record_application_run(conn: Any, *, job_id: int, url: str, status: str, reason: str, observation: PageObservation | None = None, plan: AutofillPlan | None = None) -> int:
    now = utc_now()
    cur = conn.execute(
        """
        INSERT INTO application_runs (job_id, apply_url, status, reason, started_at, finished_at, observation_json, plan_json)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (job_id, url, status, reason, now, now, encode_json(_obs_to_json(observation) if observation else {}), encode_json(_plan_to_json(plan) if plan else {})),
    )
    conn.commit()
    return int(cur.lastrowid)


def _obs_to_json(observation: PageObservation) -> dict[str, Any]:
    return {"url": observation.url, "title": observation.title, "fields": [field.__dict__ for field in observation.fields], "buttons": [button.__dict__ for button in observation.buttons], "errors": list(observation.errors)}


def _plan_to_json(plan: AutofillPlan) -> dict[str, Any]:
    return {"answers": [answer.__dict__ for answer in plan.answers], "safe_button_selector": plan.safe_button_selector, "status": plan.status, "reason": plan.reason, "skipped_fields": list(plan.skipped_fields), "raw": plan.raw}


def _write_json_artifact(root: Path, name: str, payload: Any) -> str:
    root.mkdir(parents=True, exist_ok=True)
    path = root / name
    path.write_text(json.dumps(payload, sort_keys=True, indent=2, default=str))
    return str(path)


def write_application_artifacts(
    artifact_dir: str | Path | None,
    *,
    job_id: int,
    observation: PageObservation,
    plan: AutofillPlan,
    filled_state: list[dict[str, Any]] | None = None,
    planned_actions: list[dict[str, Any]] | None = None,
    rejected_actions: list[dict[str, Any]] | None = None,
    screenshot_bytes: bytes | None = None,
    job_description: str | None = None,
) -> dict[str, str]:
    if artifact_dir is None:
        return {}
    safe_stamp = utc_now().replace(":", "").replace("+", "_")
    root = Path(artifact_dir) / f"job-{job_id}-{safe_stamp}-{uuid4().hex[:12]}"
    actions = {"planned": planned_actions or [], "rejected": rejected_actions or [], "executed": filled_state or []}
    paths = {
        "observation_json": _write_json_artifact(root, "observation.json", _obs_to_json(observation)),
        "plan_json": _write_json_artifact(root, "plan.json", _plan_to_json(plan)),
        "actions_json": _write_json_artifact(root, "actions.json", actions),
        "filled_state_json": _write_json_artifact(root, "filled_state.json", filled_state or []),
    }
    if job_description:
        description_path = root / "job_description.txt"
        description_path.write_text(job_description)
        paths["job_description_txt"] = str(description_path)
    if screenshot_bytes is not None:
        screenshot_path = root / "screenshot.png"
        screenshot_path.write_bytes(screenshot_bytes)
        paths["screenshot_png"] = str(screenshot_path)
    return paths


def next_application_jobs(conn: Any, *, limit: int) -> list[Any]:
    return list(
        conn.execute(
            """
            SELECT j.* FROM jobs j
            WHERE j.status = 'queued' AND j.canonical_url IS NOT NULL
              AND NOT EXISTS (
                SELECT 1 FROM application_runs r
                WHERE r.job_id = j.id AND r.status IN ('completed', 'manual', 'blocked')
              )
            ORDER BY j.posted_at DESC NULLS LAST, j.first_seen_at ASC, j.id ASC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
    )


def sample_manual_runs(conn: Any, *, limit: int = 10) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT r.id AS run_id, r.job_id, r.apply_url, r.status, r.reason, j.title, j.company
        FROM application_runs r
        JOIN jobs j ON j.id = r.job_id
        WHERE r.status IN ('manual', 'blocked', 'failed')
        ORDER BY r.finished_at DESC, r.id DESC
        LIMIT ?
        """,
        (limit,),
    ).fetchall()
    return [dict(row) for row in rows]


class _PuppeteerSession:
    def __init__(self, process: asyncio.subprocess.Process) -> None:
        self._process = process

    @classmethod
    async def start(cls, *, headless: bool) -> "_PuppeteerSession":
        script = Path(__file__).with_name("puppeteer_runner.js")
        try:
            process = await asyncio.create_subprocess_exec(
                "node",
                str(script),
                cwd=str(Path.cwd()),
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except FileNotFoundError as exc:
            raise RuntimeError("Puppeteer browser adapter requires Node.js and the repository npm dependencies") from exc
        session = cls(process)
        hello = await session._read_response("startup")
        if not hello.get("ok"):
            raise RuntimeError(str(hello.get("error") or "Puppeteer browser adapter failed to start"))
        await session._request("launch", headless=headless)
        return session

    async def _read_response(self, action: str) -> dict[str, Any]:
        if self._process.stdout is None:
            raise RuntimeError("Puppeteer browser adapter is not connected")
        line = await self._process.stdout.readline()
        if not line:
            stderr = await self._process.stderr.read() if self._process.stderr is not None else b""
            raise RuntimeError(f"Puppeteer browser adapter stopped unexpectedly during {action}: {stderr.decode(errors='replace').strip()}")
        response = json.loads(line.decode())
        return response if isinstance(response, dict) else {}

    async def _request(self, action: str, **payload: Any) -> dict[str, Any]:
        if self._process.stdin is None:
            raise RuntimeError("Puppeteer browser adapter is not connected")
        self._process.stdin.write((json.dumps({"action": action, **payload}, separators=(",", ":")) + "\n").encode())
        await self._process.stdin.drain()
        response = await self._read_response(action)
        if not response.get("ok"):
            raise RuntimeError(str(response.get("error") or f"Puppeteer action failed: {action}"))
        data = response.get("data")
        return data if isinstance(data, dict) else {}

    async def goto(self, url: str) -> dict[str, Any]:
        return await self._request("goto", url=url)

    async def fill(self, selector: str, value: str) -> None:
        await self._request("fill", selector=selector, value=value)

    async def select(self, selector: str, value: str) -> None:
        await self._request("select", selector=selector, value=value)

    async def upload(self, selector: str, value: str) -> None:
        await self._request("upload", selector=selector, value=value)

    async def click(self, selector: str) -> None:
        await self._request("click", selector=selector)

    async def screenshot(self) -> bytes:
        data = await self._request("screenshot")
        encoded = data.get("base64")
        return base64.b64decode(encoded) if isinstance(encoded, str) else b""

    async def close(self) -> None:
        try:
            await self._request("close")
        except Exception:
            self._process.terminate()


async def run_browser_autofill(
    conn: Any,
    *,
    limit: int = 1,
    resume_dir: str | Path = "resume",
    headed: bool = False,
    ats: str = "auto",
    application_profile_json: str | Path | None = None,
    artifact_dir: str | Path | None = None,
) -> list[dict[str, Any]]:
    from .ats import ApplicationContext, classify_application_site, find_resume_file, load_application_profile, merge_plans, select_adapter

    resume_context = load_resume_context(resume_dir)
    resume_metadata = load_resume_metadata(resume_dir)
    application_profile = load_application_profile(application_profile_json, resume_dir)
    app_context = ApplicationContext(resume_text=resume_context, resume_file=find_resume_file(resume_dir), application_profile=application_profile)
    results: list[dict[str, Any]] = []
    session = await _PuppeteerSession.start(headless=not headed)
    try:
        for row in next_application_jobs(conn, limit=limit):
            url = job_application_url(row)
            if not url:
                continue
            try:
                page_state = await session.goto(url)
                html = str(page_state.get("html") or "")
                page_url = str(page_state.get("url") or url)
                page_title = str(page_state.get("title") or "")
                observation = observe_html(html, url=page_url, title=page_title)
                job_description = job_description_from_row(row)
                adapter = select_adapter(ats, url=page_url, html=html)
                site_classification = classify_application_site(adapter=adapter, url=page_url, html=html)
                with TemporaryDirectory(prefix="jobs-assistant-description-") as description_dir:
                    (Path(description_dir) / "job_description.txt").write_text(job_description)
                    llm_plan = resolve_with_llm(observation, job=dict(row), resume_context=resume_context, job_description=job_description, resume_metadata=resume_metadata, profile_context=application_profile)
                if adapter:
                    plan = merge_plans(adapter.deterministic_answers(observation, app_context), llm_plan, observation=observation)
                else:
                    plan = llm_plan
                missing_fields = unresolved_required_fields(observation, plan)
                plan = replace(
                    plan,
                    raw={
                        **plan.raw,
                        "site_classification": site_classification,
                        "ats": adapter.name if adapter else None,
                        "missing_required_fields": list(missing_fields),
                        "learning_policy": "add safe repeatable answers to application_profile_json manually; never infer sensitive/legal fields",
                    },
                )
                planned_actions, rejected_actions = plan_action_evidence(observation, plan)
                if plan.status != "ready":
                    artifact_paths = write_application_artifacts(artifact_dir, job_id=int(row["id"]), observation=observation, plan=plan, planned_actions=planned_actions, rejected_actions=rejected_actions, job_description=job_description)
                    if artifact_paths:
                        plan = replace(plan, raw={**plan.raw, "artifact_paths": artifact_paths, "planned_actions": planned_actions, "rejected_actions": rejected_actions})
                    run_id = record_application_run(conn, job_id=int(row["id"]), url=url, status=MANUAL_STATUS, reason=plan.reason, observation=observation, plan=plan)
                    results.append({"job_id": int(row["id"]), "run_id": run_id, "status": MANUAL_STATUS, "reason": plan.reason, "ats": adapter.name if adapter else None, "artifacts": artifact_paths})
                    continue
                answer_by_selector = {answer.selector: answer for answer in plan.answers}
                filled_state: list[dict[str, Any]] = []
                for action in planned_actions:
                    if action["action"] == "click":
                        continue
                    answer = answer_by_selector[action["selector"]]
                    if action["action"] == "upload":
                        await session.upload(answer.selector, answer.value)
                    elif action["action"] == "select":
                        await session.select(answer.selector, answer.value)
                    else:
                        await session.fill(answer.selector, answer.value)
                    filled_state.append(action)
                clicked = next((action["selector"] for action in planned_actions if action["action"] == "click"), None)
                if clicked is not None:
                    await session.click(clicked)
                screenshot = await session.screenshot() if artifact_dir is not None else None
                artifact_paths = write_application_artifacts(artifact_dir, job_id=int(row["id"]), observation=observation, plan=plan, filled_state=filled_state, planned_actions=planned_actions, rejected_actions=rejected_actions, screenshot_bytes=screenshot, job_description=job_description)
                if artifact_paths:
                    plan = replace(plan, raw={**plan.raw, "artifact_paths": artifact_paths, "filled_state": filled_state, "planned_actions": planned_actions, "rejected_actions": rejected_actions})
                reason = "safe field autofill completed" if clicked is None else "safe field autofill completed; safe navigation clicked"
                run_id = record_application_run(conn, job_id=int(row["id"]), url=url, status=COMPLETED_STATUS, reason=reason, observation=observation, plan=plan)
                results.append({"job_id": int(row["id"]), "run_id": run_id, "status": COMPLETED_STATUS, "reason": reason, "ats": adapter.name if adapter else None, "artifacts": artifact_paths})
            except Exception as exc:  # bounded per job: mark and continue
                run_id = record_application_run(conn, job_id=int(row["id"]), url=url, status=BLOCKED_STATUS, reason=str(exc)[:500])
                results.append({"job_id": int(row["id"]), "run_id": run_id, "status": BLOCKED_STATUS, "reason": str(exc)[:500]})
    finally:
        await session.close()
    return results
