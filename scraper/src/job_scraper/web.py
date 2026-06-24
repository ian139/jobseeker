from __future__ import annotations

import html
import re
from urllib.parse import quote

from fastapi import FastAPI, File, Form, UploadFile
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse, Response

from job_scraper.config import AppSettings
from job_scraper.matching import build_improvement_prompt, score_jobs, summarize_categories
from job_scraper.resume_uploads import (
    ResumeUploadError,
    UploadedResumeAnalysis,
    analyze_resume_upload,
    build_tailored_resume_prompt,
)
from job_scraper.storage import JobRecord, JobStorage


def create_app(settings: AppSettings | None = None) -> FastAPI:
    settings = settings or AppSettings()
    storage = JobStorage(settings.job_scraper_db_path)
    app = FastAPI(title="Job Scraper Market Signal Console")

    @app.get("/", response_class=HTMLResponse)
    def index() -> HTMLResponse:
        jobs = storage.list_jobs(limit=100)
        body = _intake_page(jobs)
        return HTMLResponse(_page("Market Signal Console", body))

    @app.get("/matches")
    def matches_get() -> RedirectResponse:
        return RedirectResponse("/", status_code=303)

    @app.get("/favicon.ico")
    def favicon() -> Response:
        return Response(status_code=204)

    @app.post("/matches", response_class=HTMLResponse)
    def create_matches(
        target_roles: str = Form(""),
        target_industries: str = Form(""),
        keywords: str = Form(""),
        resume_file: UploadFile = File(...),
    ) -> HTMLResponse:
        content = resume_file.file.read()
        try:
            analysis = analyze_resume_upload(resume_file.filename or "", content)
        except ResumeUploadError as exc:
            return HTMLResponse(_page("Upload Error", f"<h1>Upload Error</h1><p>{html.escape(str(exc))}</p>"), status_code=400)

        roles = _split_terms(target_roles)
        industries = _split_terms(target_industries)
        filter_terms = _split_terms(keywords)
        scored_jobs = score_jobs(
            storage.list_jobs(limit=250),
            analysis,
            target_roles=roles,
            target_industries=industries,
            keywords=filter_terms,
        )
        body = _matches_page(
            scored_jobs=scored_jobs,
            analysis=analysis,
            target_roles=roles,
            target_industries=industries,
            keywords=filter_terms,
        )
        return HTMLResponse(_page("Scored Job Matches", body))

    @app.post("/jobs/{job_id}/improvement-prompt", response_class=PlainTextResponse)
    def download_improvement_prompt(
        job_id: str,
        resume_filename: str = Form(...),
        resume_kind: str = Form("text"),
        resume_text: str = Form(...),
        target_roles: str = Form(""),
        target_industries: str = Form(""),
        keywords: str = Form(""),
    ):
        job = storage.get_job(job_id)
        if job is None:
            return _unknown_job_response(job_id)

        kind = resume_kind if resume_kind in {"pdf", "latex", "text"} else "text"
        analysis = UploadedResumeAnalysis(
            filename=resume_filename,
            kind=kind,  # type: ignore[arg-type]
            text=resume_text,
            facts_markdown=_resume_fact_summary(filename=resume_filename, kind=kind, text=resume_text),
        )
        roles = _split_terms(target_roles)
        industries = _split_terms(target_industries)
        scored = score_jobs(
            [job],
            analysis,
            target_roles=roles,
            target_industries=industries,
            keywords=_split_terms(keywords),
        )[0]
        prompt = build_improvement_prompt(scored, analysis, target_roles=roles, target_industries=industries)
        filename = f"{_download_slug(job.title or job.theirstack_id)}-resume-improvement-prompt.txt"
        return PlainTextResponse(
            prompt,
            media_type="text/plain; charset=utf-8",
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )

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
    :root {{ color-scheme: dark; background: #0B0F14; color: #EBDBB2; }}
    * {{ box-sizing: border-box; }}
    body {{ background: #0B0F14; color: #EBDBB2; font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; margin: 0; line-height: 1.45; }}
    main {{ max-width: 118rem; margin: 0 auto; padding: 1.25rem; }}
    h1, h2, h3 {{ letter-spacing: -0.03em; line-height: 1.05; margin: 0 0 0.8rem; }}
    p {{ margin: 0.35rem 0; }}
    a {{ color: #7DAEA3; text-decoration: none; }}
    a:hover {{ color: #89B482; text-decoration: underline; }}
    .console-note, .panel, form, article, .detail-card {{ background: #11161D; border: 1px solid #2B3440; border-radius: 0.85rem; padding: 1rem; }}
    .console-note, .console-bar {{ display: grid; gap: 0.4rem; margin-bottom: 1rem; background: linear-gradient(135deg, #11161D 0%, #161C23 70%, #1A212B 100%); }}
    .eyebrow {{ color: #D8A657; font-size: 0.78rem; font-weight: 800; letter-spacing: 0.16em; text-transform: uppercase; }}
    .muted {{ color: #928374; }}
    .lede {{ color: #BDAE93; max-width: 70rem; }}
    .grid {{ display: grid; gap: 1rem; }}
    .intake-grid {{ grid-template-columns: minmax(0, 1.15fr) minmax(22rem, 0.85fr); align-items: start; }}
    .metrics {{ grid-template-columns: repeat(4, minmax(0, 1fr)); }}
    .metric {{ background: #161C23; border: 1px solid #202832; border-radius: 0.75rem; padding: 0.85rem; }}
    .metric strong {{ display: block; color: #FABD2F; font-size: 1.35rem; }}
    label {{ display: block; color: #EBDBB2; font-weight: 750; margin-top: 0.85rem; }}
    .hint {{ color: #928374; display: block; font-size: 0.8rem; margin-top: 0.2rem; }}
    input[type="text"], input[type="file"], textarea {{ background: #0B0F14; color: #EBDBB2; border: 1px solid #2B3440; border-radius: 0.45rem; width: 100%; margin-top: 0.3rem; padding: 0.65rem; font: inherit; }}
    input[type="file"]::file-selector-button {{ background: #D8A657; color: #11161D; border: 0; border-radius: 0.35rem; padding: 0.5rem 0.75rem; margin-right: 0.75rem; cursor: pointer; font-weight: 850; }}
    textarea {{ min-height: 34rem; white-space: pre-wrap; }}
    button, .button {{ background: #D8A657; color: #11161D; border: 0; border-radius: 0.45rem; display: inline-block; margin-top: 1rem; padding: 0.6rem 0.85rem; font-weight: 850; cursor: pointer; }}
    button:hover, .button:hover, input[type="file"]::file-selector-button:hover {{ background: #FABD2F; color: #11161D; text-decoration: none; }}
    .ghost-button {{ background: transparent; border: 1px solid #2B3440; color: #D8A657; }}
    .tabs {{ display: flex; flex-wrap: wrap; gap: 0.5rem; margin: 1rem 0; }}
    .tab {{ border: 1px solid #2B3440; border-radius: 999px; color: #BDAE93; padding: 0.45rem 0.7rem; }}
    .tab strong {{ color: #D8A657; }}
    .analysis-grid {{ grid-template-columns: repeat(3, minmax(0, 1fr)); }}
    .analysis-card {{ background: #161C23; border: 1px solid #202832; border-radius: 0.75rem; padding: 0.85rem; }}
    .analysis-card ul {{ margin: 0.4rem 0 0; padding-left: 1.1rem; color: #BDAE93; }}
    .results-shell {{ display: grid; grid-template-columns: minmax(0, 1.45fr) minmax(22rem, 0.85fr); gap: 1rem; align-items: start; margin-top: 1rem; }}
    .table-panel {{ background: #11161D; border: 1px solid #2B3440; border-radius: 0.85rem; overflow: hidden; }}
    .table-tools {{ align-items: end; background: #161C23; border-bottom: 1px solid #202832; display: grid; gap: 0.75rem; grid-template-columns: repeat(4, minmax(0, 1fr)); padding: 0.85rem; }}
    table {{ border-collapse: collapse; width: 100%; }}
    th, td {{ border-bottom: 1px solid #202832; padding: 0.65rem 0.75rem; text-align: left; vertical-align: top; }}
    th {{ color: #BDAE93; font-size: 0.75rem; letter-spacing: 0.1em; text-transform: uppercase; }}
    tr:hover td {{ background: #161C23; }}
    .score, .score-badge.high {{ color: #A9B665; font-weight: 900; }}
    .gap {{ color: #EA6962; }}
    .badge {{ border: 1px solid #2B3440; border-radius: 999px; color: #BDAE93; display: inline-block; font-size: 0.74rem; margin: 0.15rem 0.15rem 0 0; padding: 0.15rem 0.42rem; }}
    .job-row.gold td:first-child {{ border-left: 3px solid #D8A657; }}
    .job-row.silver td:first-child {{ border-left: 3px solid #928374; }}
    .job-row.bronze td:first-child {{ border-left: 3px solid #E78A4E; }}
    .category-block {{ border-top: 1px solid #202832; padding-top: 0.5rem; }}
    .category-block:first-child {{ border-top: 0; }}
    .detail-stack {{ position: sticky; top: 1rem; }}
    .detail-card {{ display: none; }}
    .detail-card:first-child {{ display: block; }}
    .detail-stack:has(.detail-card:target) .detail-card {{ display: none; }}
    .detail-stack .detail-card:target {{ display: block; }}
    .description {{ background: #0B0F14; border: 1px solid #202832; border-radius: 0.55rem; color: #BDAE93; max-height: 24rem; overflow: auto; padding: 0.75rem; white-space: pre-wrap; }}
    pre {{ background: #0B0F14; color: #EBDBB2; border: 1px solid #2B3440; border-radius: 0.5rem; padding: 1rem; white-space: pre-wrap; overflow-wrap: anywhere; }}
    .legacy-list {{ display: grid; gap: 0.75rem; margin-top: 1rem; }}
    @media (max-width: 980px) {{
      main {{ padding: 0.85rem; }}
      .intake-grid, .analysis-grid, .metrics, .results-shell, .table-tools {{ grid-template-columns: 1fr; }}
      .detail-stack {{ position: static; }}
    }}
  </style>
</head>
<body>
  <main>
    <section id="console-header" class="console-note console-bar">
      <span class="eyebrow">Market Signal Console</span>
      <h1>{escaped_title}</h1>
      <p class="lede">Dark, data-dense job intelligence for selecting target sectors, scoring resume fit, and generating precise resume-improvement prompts from local SQLite data.</p>
    </section>
    {body}
  </main>
</body>
</html>"""


def _intake_page(jobs: list[JobRecord]) -> str:
    job_count = len(jobs)
    latest = html.escape(jobs[0].discovered_at or jobs[0].date_posted or "n/a") if jobs else "n/a"
    recent_rows = "\n".join(_compact_job_row(job) for job in jobs[:8]) or '<tr><td colspan="5">No scraped jobs found. Run job-scraper run-once first.</td></tr>'
    return f"""<section id="input-strip" class="grid intake-grid">
  <form id="resume-upload" method="post" enctype="multipart/form-data" action="/matches" aria-labelledby="strategy-title">
    <span class="eyebrow">Start with roles, filters, or resume</span>
    <h2 id="strategy-title">Build a scored target market</h2>
    <p class="muted">Choose roles/industries first when exploring. If you already know the target, upload the resume and use filters as the search brief.</p>
    <label for="target_roles">Target roles</label>
    <input id="target_roles" name="target_roles" type="text" placeholder="Frontend Engineer, Product Engineer, Data Platform">
    <span class="hint">Optional; comma-separated role titles or functions.</span>
    <label for="target_industries">Industries or subsectors</label>
    <input id="target_industries" name="target_industries" type="text" placeholder="Healthcare, Fintech, Climate, Developer Tools">
    <span class="hint">Optional; used for category tabs and industry-fit scoring.</span>
    <label for="keywords">Filter keywords</label>
    <input id="keywords" name="keywords" type="text" placeholder="React, TypeScript, Python, remote, platform">
    <span class="hint">Optional; boosts and filters the top match list.</span>
    <label for="resume-file">Resume file</label>
    <input id="resume-file" name="resume_file" type="file" accept=".pdf,.tex,.latex,.txt,.md" required>
    <button id="resume-upload-btn" type="submit">Score jobs against resume</button>
  </form>
  <section class="panel">
    <span class="eyebrow">Scrape status</span>
    <div class="grid metrics">
      <div class="metric"><span class="muted">Jobs loaded</span><strong>{job_count}</strong></div>
      <div class="metric"><span class="muted">Latest signal</span><strong>{latest}</strong></div>
      <div class="metric"><span class="muted">Workflow</span><strong>2 paths</strong></div>
      <div class="metric"><span class="muted">Output</span><strong>Prompts</strong></div>
    </div>
    <h3>Recent scraped roles</h3>
    <table>
      <thead><tr><th>Role</th><th>Company</th><th>Country</th><th>Date</th><th>Prompt</th></tr></thead>
      <tbody>{recent_rows}</tbody>
    </table>
  </section>
</section>"""


def _matches_page(
    *,
    scored_jobs: list[object],
    analysis: UploadedResumeAnalysis,
    target_roles: list[str],
    target_industries: list[str],
    keywords: list[str],
) -> str:
    if not scored_jobs:
        return """<section class="panel"><h2>No scored jobs available</h2><p class="muted">Run the scraper, then upload a resume to build a market console.</p></section>"""

    categories = summarize_categories(scored_jobs)
    top_score = int(round(float(getattr(scored_jobs[0], "score", 0.0))))
    terms = ", ".join(target_roles + target_industries + keywords) or "resume-only scoring"
    category_tabs = "\n".join(
        f'<a class="tab" href="#category-{_dom_id(_category_name(category))}">{html.escape(_category_name(category))} <strong>{_category_count(category)}</strong></a>'
        for category in categories
    )
    analysis_cards = "\n".join(_category_analysis_card(category) for category in categories)
    category_tables = "\n".join(
        _category_table(_category_name(category), [job for job in scored_jobs if getattr(job, "category", "Uncategorized") == _category_name(category)])
        for category in categories
    )
    detail_cards = "\n".join(_detail_card(job) for job in scored_jobs)
    hidden_resume = _hidden_resume_fields(analysis, target_roles=target_roles, target_industries=target_industries, keywords=keywords)
    return f"""<section class="panel">
  <span class="eyebrow">Resume-to-market analysis</span>
  <h2>Top scored matches</h2>
  <p class="muted">Scoring brief: {html.escape(terms)}. Top match score: <span class="score">{top_score}</span>.</p>
  <nav id="category-tabs" class="tabs tab-bar" aria-label="Industry categories">{category_tabs}</nav>
  <div class="grid analysis-grid">{analysis_cards}</div>
</section>
<section class="results-shell" aria-label="Scored job results and details">
  <section id="job-table-container" class="table-panel">
    <form class="table-tools" method="post" enctype="multipart/form-data" action="/matches">
      <div><label for="target_roles_refine">Roles</label><input id="target_roles_refine" name="target_roles" type="text" value="{html.escape(', '.join(target_roles))}"></div>
      <div><label for="target_industries_refine">Industries</label><input id="target_industries_refine" name="target_industries" type="text" value="{html.escape(', '.join(target_industries))}"></div>
      <div><label for="keywords_refine">Keywords</label><input id="keywords_refine" name="keywords" type="text" value="{html.escape(', '.join(keywords))}"></div>
      <div><label for="resume_refine">Resume</label><input id="resume_refine" name="resume_file" type="file" accept=".pdf,.tex,.latex,.txt,.md" required><button type="submit">Refilter</button></div>
    </form>
    {category_tables}
  </section>
  <aside id="detail-panel" class="detail-stack" aria-label="Selected job details">
    {detail_cards}
  </aside>
</section>
<form id="resume-prompt-payload" hidden aria-hidden="true" method="post">{hidden_resume}</form>"""


def _compact_job_row(job: JobRecord) -> str:
    job_path_id = quote(job.theirstack_id, safe="")
    return f"""<tr>
  <td>{html.escape(job.title or "Untitled role")}</td>
  <td>{html.escape(job.company or "Unknown")}</td>
  <td>{html.escape(job.country_code or "")}</td>
  <td>{html.escape(job.date_posted or "")}</td>
  <td><a class="button ghost-button" href="/jobs/{job_path_id}/prompt">Tailor prompt</a></td>
</tr>"""


def _category_analysis_card(category: object) -> str:
    name = html.escape(_category_name(category))
    regions = _list_items(_category_regions(category), "No US region signals found.")
    strengths = _list_items(_category_strengths(category), "Resume has broad transferable coverage.")
    gaps = _list_items(_category_gaps(category), "No major repeated gaps detected.")
    return f"""<article class="analysis-card" id="category-{_dom_id(_category_name(category))}">
  <h3>{name}</h3>
  <p class="muted">{_category_count(category)} scored jobs in this category.</p>
  <h4>Best-supported US regions</h4><ul>{regions}</ul>
  <h4>Resume strengths</h4><ul>{strengths}</ul>
  <h4>Improve next</h4><ul>{gaps}</ul>
</article>"""


def _category_table(category_name: str, jobs: list[object]) -> str:
    rows = "\n".join(_result_row(scored) for scored in jobs)
    if not rows:
        rows = '<tr><td colspan="7">No jobs in this category.</td></tr>'
    return f"""<section class="category-block">
  <h3>{html.escape(category_name)}</h3>
  <table class="data-table">
    <thead><tr><th>Score</th><th>Role</th><th>Company</th><th>Region</th><th>Matched</th><th>Missing</th><th>Details</th></tr></thead>
    <tbody>{rows}</tbody>
  </table>
</section>"""


def _result_row(scored: object) -> str:
    job = getattr(scored, "job")
    detail_id = _job_detail_id(job)
    matched = "".join(f'<span class="badge">{html.escape(term)}</span>' for term in getattr(scored, "matched_terms", [])[:4])
    missing = "".join(f'<span class="badge gap">{html.escape(term)}</span>' for term in getattr(scored, "missing_terms", [])[:4])
    return f"""<tr class="job-row {_score_tier(float(getattr(scored, "score", 0.0)))}" data-job-id="{html.escape(job.theirstack_id)}">
  <td class="score">{int(round(float(getattr(scored, "score", 0.0))))}</td>
  <td><a href="#{detail_id}">{html.escape(job.title or "Untitled role")}</a></td>
  <td>{html.escape(job.company or "Unknown")}</td>
  <td>{html.escape(str(getattr(scored, "region", "") or getattr(scored, "remote_label", "")))}</td>
  <td>{matched or '<span class="muted">None</span>'}</td>
  <td>{missing or '<span class="muted">None</span>'}</td>
  <td><a class="button ghost-button" href="#{detail_id}">Open</a></td>
</tr>"""


def _detail_card(scored: object) -> str:
    job = getattr(scored, "job")
    url = job.final_url or job.url or ""
    description = _job_description(job)
    return f"""<article class="detail-card detail-pane" id="{_job_detail_id(job)}">
  <span class="eyebrow">Selected role</span>
  <h2 class="detail-title">{html.escape(job.title or "Untitled role")}</h2>
  <p><strong>Company:</strong> {html.escape(job.company or "Unknown company")}</p>
  <p><strong>Category:</strong> {html.escape(str(getattr(scored, "category", "Uncategorized")))}</p>
  <p><strong>Score:</strong> <span class="score">{int(round(float(getattr(scored, "score", 0.0))))}</span></p>
  <p><strong>Region:</strong> {html.escape(str(getattr(scored, "region", "") or getattr(scored, "remote_label", "")))}</p>
  <p><strong>Link:</strong> {_job_link(url)}</p>
  <h3>Description</h3>
  <div class="description">{html.escape(description or "No description available.")}</div>
  <button class="download-prompt" type="submit" form="resume-prompt-payload" formaction="/jobs/{quote(job.theirstack_id, safe='')}/improvement-prompt" formmethod="post">Download resume improvement prompt</button>
</article>"""


def _hidden_resume_fields(
    analysis: UploadedResumeAnalysis,
    *,
    target_roles: list[str],
    target_industries: list[str],
    keywords: list[str],
) -> str:
    return f"""<input type="hidden" name="resume_filename" value="{html.escape(analysis.filename)}">
<input type="hidden" name="resume_kind" value="{html.escape(analysis.kind)}">
<input type="hidden" name="resume_text" value="{html.escape(analysis.text)}">
<input type="hidden" name="target_roles" value="{html.escape(', '.join(target_roles))}">
<input type="hidden" name="target_industries" value="{html.escape(', '.join(target_industries))}">
<input type="hidden" name="keywords" value="{html.escape(', '.join(keywords))}">"""


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


def _split_terms(value: str) -> list[str]:
    seen: set[str] = set()
    terms: list[str] = []
    for term in re.split(r"[,;\n]+", value):
        normalized = re.sub(r"\s+", " ", term).strip()
        key = normalized.casefold()
        if normalized and key not in seen:
            seen.add(key)
            terms.append(normalized)
    return terms


def _dom_id(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", value.casefold()).strip("-")
    return slug or "uncategorized"


def _job_detail_id(job: JobRecord) -> str:
    return f"job-{_dom_id(job.theirstack_id)}"


def _download_slug(value: str) -> str:
    return _dom_id(value)[:80] or "job"


def _job_description(job: JobRecord) -> str:
    for key in ("job_description", "description", "job_text", "summary"):
        value = job.raw.get(key)
        if value:
            return str(value)
    return ""


def _job_link(url: str) -> str:
    if not url:
        return '<span class="muted">No URL found</span>'
    escaped_url = html.escape(url)
    return f'<a href="{escaped_url}">{escaped_url}</a>'


def _list_items(values: object, fallback: str) -> str:
    if not isinstance(values, list) or not values:
        return f"<li>{html.escape(fallback)}</li>"
    return "".join(f"<li>{html.escape(str(value))}</li>" for value in values[:4])


def _score_tier(score: float) -> str:
    if score >= 70:
        return "gold"
    if score >= 40:
        return "silver"
    return "bronze"


def _category_name(category: object) -> str:
    return str(getattr(category, "name", None) or getattr(category, "category", "Uncategorized"))


def _category_count(category: object) -> int:
    return int(getattr(category, "job_count", None) or getattr(category, "count", 0))


def _category_regions(category: object) -> list[str]:
    return _ranked_values(
        str(getattr(job, "region", "") or getattr(job, "remote_label", ""))
        for job in getattr(category, "top_jobs", ())
    )


def _category_strengths(category: object) -> list[str]:
    values = _ranked_values(term for job in getattr(category, "top_jobs", ()) for term in getattr(job, "matched_terms", ()))
    return [f"Matches {value}" for value in values]


def _category_gaps(category: object) -> list[str]:
    values = _ranked_values(term for job in getattr(category, "top_jobs", ()) for term in getattr(job, "missing_terms", ()))
    return [f"Add clearer evidence for {value}" for value in values]


def _ranked_values(values: object) -> list[str]:
    counts: dict[str, int] = {}
    for value in values:
        text = str(value).strip()
        if text:
            counts[text] = counts.get(text, 0) + 1
    return [value for value, _count in sorted(counts.items(), key=lambda item: (-item[1], item[0]))[:4]]


def _resume_fact_summary(*, filename: str, kind: str, text: str) -> str:
    preview = text[:700].strip()
    return f"**Source:** {filename}\n\n**Kind:** {kind}\n\n**Resume preview:** {preview}"
