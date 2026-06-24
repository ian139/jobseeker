from __future__ import annotations

import html
from urllib.parse import quote

from fastapi import FastAPI, File, Form, UploadFile
from fastapi.responses import HTMLResponse

from job_scraper.config import AppSettings
from job_scraper.resume_uploads import ResumeUploadError, analyze_resume_upload, build_tailored_resume_prompt
from job_scraper.storage import JobRecord, JobStorage


def create_app(settings: AppSettings | None = None) -> FastAPI:
    settings = settings or AppSettings()
    storage = JobStorage(settings.job_scraper_db_path)
    app = FastAPI(title="Job Scraper Resume UI")

    @app.get("/", response_class=HTMLResponse)
    def index() -> HTMLResponse:
        jobs = storage.list_jobs(limit=100)
        if not jobs:
            body = "<p>No scraped jobs found. Run job-scraper run-once first.</p>"
        else:
            body = "\n".join(_job_card(job) for job in jobs)
        return HTMLResponse(_page("Scraped Jobs", f"<h1>Scraped Jobs</h1>{body}"))

    @app.get("/jobs/{job_id}/prompt", response_class=HTMLResponse)
    def prompt_form(job_id: str) -> HTMLResponse:
        job = storage.get_job(job_id)
        if job is None:
            return _unknown_job_response(job_id)
        return HTMLResponse(_page("Create Tailored Prompt", _prompt_form(job)))

    @app.post("/jobs/{job_id}/prompt", response_class=HTMLResponse)
    def create_prompt(
        job_id: str,
        industry: str = Form(...),
        resume_file: UploadFile = File(...),
    ) -> HTMLResponse:
        job = storage.get_job(job_id)
        if job is None:
            return _unknown_job_response(job_id)

        content = resume_file.file.read()
        try:
            analysis = analyze_resume_upload(resume_file.filename or "", content)
            prompt = build_tailored_resume_prompt(job=job, industry=industry, analysis=analysis)
        except ResumeUploadError as exc:
            return HTMLResponse(_page("Upload Error", f"<h1>Upload Error</h1><p>{html.escape(str(exc))}</p>"), status_code=400)

        return HTMLResponse(_page("Tailored Resume Prompt", _prompt_result(job, industry, analysis.facts_markdown, prompt)))

    return app


def run_web_ui(*, host: str = "127.0.0.1", port: int = 8000, settings: AppSettings | None = None) -> None:
    import uvicorn

    uvicorn.run(create_app(settings), host=host, port=port)


def _page(title: str, body: str) -> str:
    escaped_title = html.escape(title)
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{escaped_title}</title>
  <style>
    :root {{ color-scheme: dark; background: #0f172a; color: #e5e7eb; }}
    body {{ background: #0f172a; color: #e5e7eb; font-family: ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; margin: 2rem auto; max-width: 72rem; padding: 0 1rem; line-height: 1.5; }}
    .console-note, article, form, .panel {{ background: #111827; border: 1px solid #334155; border-radius: 0.75rem; padding: 1rem; margin: 1rem 0; box-shadow: 0 1px 2px #0006; }}
    .console-note {{ background: #1e3a5f; }}
    .muted {{ color: #cbd5e1; }}
    label {{ display: block; color: #f8fafc; font-weight: 650; margin-top: 0.75rem; }}
    input[type="text"], input[type="file"], textarea {{ background: #020617; color: #f8fafc; border: 1px solid #475569; border-radius: 0.4rem; width: 100%; box-sizing: border-box; margin-top: 0.25rem; padding: 0.5rem; }}
    input[type="file"]::file-selector-button {{ background: #2563eb; color: #ffffff; border: 0; border-radius: 0.35rem; padding: 0.45rem 0.7rem; margin-right: 0.75rem; cursor: pointer; }}
    textarea {{ min-height: 34rem; font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; }}
    button {{ background: #2563eb; color: #ffffff; border: 0; border-radius: 0.45rem; margin-top: 1rem; padding: 0.55rem 0.85rem; font-weight: 650; cursor: pointer; }}
    button:hover, input[type="file"]::file-selector-button:hover {{ background: #1d4ed8; }}
    pre {{ background: #020617; color: #e5e7eb; border: 1px solid #334155; border-radius: 0.5rem; padding: 1rem; white-space: pre-wrap; overflow-wrap: anywhere; }}
    a {{ color: #93c5fd; }}
  </style>
</head>
<body>
  <section class="console-note">
    <strong>Local resume prompt console.</strong> Browse scraped jobs, upload a current resume, and generate a prompt locally from SQLite plus the uploaded file.
  </section>
  {body}
</body>
</html>"""


def _job_card(job: JobRecord) -> str:
    job_id = html.escape(job.theirstack_id)
    job_path_id = quote(job.theirstack_id, safe="")
    title = html.escape(job.title or "Untitled role")
    company = html.escape(job.company or "Unknown company")
    country = html.escape(job.country_code or "")
    date_posted = html.escape(job.date_posted or "")
    url = job.final_url or job.url or ""
    escaped_url = html.escape(url)
    link = f'<p><a href="{escaped_url}">{escaped_url}</a></p>' if url else ""
    return f"""<article>
  <h2>{title}</h2>
  <p><strong>Company:</strong> {company}</p>
  <p><strong>Country:</strong> {country}</p>
  <p><strong>Date posted:</strong> {date_posted}</p>
  {link}
  <form method="get" action="/jobs/{job_path_id}/prompt">
    <input type="hidden" name="job_id" value="{job_id}">
    <button type="submit">Create tailored prompt</button>
  </form>
</article>"""


def _prompt_form(job: JobRecord) -> str:
    job_path_id = quote(job.theirstack_id, safe="")
    title = html.escape(job.title or "")
    company = html.escape(job.company or "")
    return f"""<h1>Create tailored prompt</h1>
<section class="panel">
  <h2>{title}</h2>
  <p><strong>Company:</strong> {company}</p>
</section>
<form method="post" enctype="multipart/form-data" action="/jobs/{job_path_id}/prompt">
  <label for="industry">Target industry</label>
  <input id="industry" name="industry" type="text" required>
  <label for="resume_file">Current resume</label>
  <input id="resume_file" name="resume_file" type="file" accept=".pdf,.tex,.latex,.txt,.md" required>
  <button type="submit">Create tailored resume prompt</button>
</form>"""


def _prompt_result(job: JobRecord, industry: str, facts_markdown: str, prompt: str) -> str:
    job_path_id = quote(job.theirstack_id, safe="")
    title = html.escape(job.title or "")
    company = html.escape(job.company or "")
    escaped_industry = html.escape(industry.strip())
    escaped_prompt = html.escape(prompt)
    escaped_facts = html.escape(facts_markdown)
    return f"""<h1>Tailored Resume Prompt</h1>
<section class="panel">
  <p><strong>Job:</strong> {title}</p>
  <p><strong>Company:</strong> {company}</p>
  <p><strong>Target industry:</strong> {escaped_industry}</p>
</section>
<label for="tailored_prompt">Generated prompt</label>
<textarea id="tailored_prompt" readonly>{escaped_prompt}</textarea>
<h2>Uploaded Resume Analysis</h2>
<pre>{escaped_facts}</pre>
<p><a href="/jobs/{job_path_id}/prompt">Back to upload form</a></p>"""


def _unknown_job_response(job_id: str) -> HTMLResponse:
    escaped_id = html.escape(job_id)
    return HTMLResponse(_page("Unknown Job", f"<h1>Unknown job</h1><p>Unknown job id: {escaped_id}</p>"), status_code=404)
