from __future__ import annotations

import html
import re
from urllib.parse import quote, urlparse

from fastapi import FastAPI, File, Form, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse, Response

from job_scraper.config import AppSettings
from job_scraper.llm import ChatCompletionsResumeLLM, ResumeLLMError
from job_scraper.matching import build_improvement_prompt, build_improvement_report, score_jobs, summarize_categories
from job_scraper.resume_uploads import (
    ResumeUploadError,
    UploadedResumeAnalysis,
    analyze_resume_upload,
    build_tailored_resume_prompt,
    facts_markdown_for_text,
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

    @app.get("/api/jobs")
    def jobs_api() -> JSONResponse:
        jobs = storage.list_jobs(limit=250)
        return JSONResponse({"jobs": [_job_detail_payload(job) for job in jobs]})

    @app.get("/api/jobs/{job_id}")
    def job_api(job_id: str) -> JSONResponse:
        job = storage.get_job(job_id)
        if job is None:
            return JSONResponse({"error": "unknown_job", "job_id": job_id}, status_code=404)
        return JSONResponse(_job_detail_payload(job))

    @app.get("/jobs/{job_id}", response_class=HTMLResponse)
    def job_detail(job_id: str) -> HTMLResponse:
        job = storage.get_job(job_id)
        if job is None:
            return _unknown_job_response(job_id)
        return HTMLResponse(_page(job.title or "Job Detail", _job_detail_page(job)))

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

    @app.post("/jobs/{job_id}/improvement-prompt")
    def legacy_improvement_prompt(job_id: str) -> RedirectResponse:
        return RedirectResponse(f"/jobs/{quote(job_id, safe='')}/improvement-report", status_code=307)

    @app.post("/jobs/{job_id}/improvement-report", response_class=PlainTextResponse)
    def download_improvement_report(
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
        if settings.llm_api_key.strip():
            try:
                report = ChatCompletionsResumeLLM(
                    settings.llm_api_key,
                    settings.llm_model,
                    base_url=settings.llm_base_url,
                ).review(prompt_markdown=prompt)
            except ResumeLLMError as exc:
                report = build_improvement_report(
                    scored,
                    analysis,
                    target_roles=roles,
                    target_industries=industries,
                    generation_note=f"LLM report generation failed ({exc}); deterministic local fallback used.",
                )
        else:
            report = build_improvement_report(
                scored,
                analysis,
                target_roles=roles,
                target_industries=industries,
                generation_note="LLM_API_KEY is not configured; deterministic local report generated from extracted resume text and job metadata.",
            )
        filename = f"{_download_slug(job.title or job.theirstack_id)}-resume-review-report.md"
        return PlainTextResponse(
            report,
            media_type="text/markdown; charset=utf-8",
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
    main {{ max-width: 118rem; margin: 0 auto; padding: 1.25rem; width: min(118rem, 100%); }}
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
    .grid > *, .intake-grid > *, .results-shell > * {{ min-width: 0; }}
    .intake-grid {{ grid-template-columns: minmax(24rem, 0.85fr) minmax(0, 1.15fr); align-items: start; }}
    .resume-primary {{ background: #161C23; border: 1px solid #D8A657; border-radius: 0.75rem; padding: 0.9rem; }}
    .secondary-filters {{ border-top: 1px solid #202832; margin-top: 1rem; padding-top: 0.75rem; }}
    .secondary-filters summary {{ color: #D8A657; cursor: pointer; font-weight: 850; }}
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
    .results-shell {{ display: grid; grid-template-columns: minmax(0, 1.45fr) minmax(24rem, 0.85fr); gap: 1rem; align-items: start; margin-top: 1rem; }}
    .table-panel {{ background: #11161D; border: 1px solid #2B3440; border-radius: 0.85rem; max-width: 100%; min-width: 0; overflow-x: auto; }}
    .table-tools {{ align-items: end; background: #161C23; border-bottom: 1px solid #202832; display: grid; gap: 0.75rem; grid-template-columns: repeat(4, minmax(0, 1fr)); padding: 0.85rem; }}
    table {{ border-collapse: collapse; max-width: 100%; table-layout: fixed; width: 100%; }}
    th, td {{ border-bottom: 1px solid #202832; overflow-wrap: anywhere; padding: 0.65rem 0.75rem; text-align: left; vertical-align: top; word-break: break-word; }}
    th {{ color: #BDAE93; font-size: 0.75rem; letter-spacing: 0.1em; text-transform: uppercase; }}
    tr:hover td {{ background: #161C23; }}
    .data-table th:nth-child(1), .data-table td:nth-child(1) {{ width: 4.25rem; }}
    .data-table th:nth-child(4), .data-table td:nth-child(4) {{ width: 24%; }}
    .data-table th:nth-child(7), .data-table td:nth-child(7) {{ width: 5.5rem; }}
    .score, .score-badge.high {{ color: #A9B665; font-weight: 900; }}
    .gap, .missing-field {{ color: #EA6962; }}
    .badge {{ border: 1px solid #2B3440; border-radius: 999px; color: #BDAE93; display: inline-block; font-size: 0.74rem; margin: 0.15rem 0.15rem 0 0; max-width: 100%; overflow-wrap: anywhere; padding: 0.15rem 0.42rem; white-space: normal; }}
    .job-row.gold td:first-child {{ border-left: 3px solid #D8A657; }}
    .job-row.silver td:first-child {{ border-left: 3px solid #928374; }}
    .job-row.bronze td:first-child {{ border-left: 3px solid #E78A4E; }}
    .category-block {{ border-top: 1px solid #202832; padding-top: 0.5rem; }}
    .category-block:first-child {{ border-top: 0; }}
    .detail-stack {{ position: sticky; top: 1rem; }}
    .detail-card {{ display: none; }}
    .detail-card:first-child, .detail-card.is-selected, .detail-card:target {{ display: block; }}
    .detail-stack.has-selection .detail-card:first-child {{ display: none; }}
    .detail-stack.has-selection .detail-card.is-selected {{ display: block; }}
    .detail-stack:has(.detail-card:target) .detail-card {{ display: none; }}
    .detail-stack .detail-card:target {{ display: block; }}
    .parse-quality {{ background: #0B0F14; border: 1px solid #202832; border-radius: 0.65rem; margin: 0.75rem 0; padding: 0.65rem; }}
    .parse-quality details {{ margin-top: 0.35rem; }}
    .detail-meta {{ display: grid; gap: 0.35rem; margin: 0.75rem 0; }}
    .detail-card .detail-actions {{ background: #11161D; border-top: 1px solid #202832; display: flex; flex-wrap: wrap; gap: 0.5rem; margin: 0.75rem 0 1rem; padding-top: 0.75rem; position: sticky; top: 0; z-index: 2; }}
    .table-actions {{ display: flex; flex-wrap: wrap; gap: 0.35rem; }}
    .action-row {{ align-items: center; }}
    .copy-status {{ color: #A9B665; font-size: 0.8rem; margin-top: 1rem; }}
    .description {{ background: #0B0F14; border: 1px solid #202832; border-radius: 0.55rem; color: #BDAE93; overflow-wrap: anywhere; padding: 0.75rem; white-space: pre-wrap; }}
    .external-url {{ overflow-wrap: anywhere; }}
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
      <p class="lede">Dark, data-dense job intelligence for selecting target sectors, scoring resume fit, and generating Markdown resume-review reports from local SQLite data.</p>
    </section>
    {body}
    {_detail_selection_script()}
    {_copy_report_script()}
  </main>
</body>
</html>"""


def _detail_selection_script() -> str:
    return """<script>
(() => {
  const panel = document.getElementById("detail-panel");
  if (!panel) return;
  const cards = Array.from(panel.querySelectorAll(".detail-card[id]"));
  if (!cards.length) return;
  const links = Array.from(document.querySelectorAll("[data-detail-target]"));
  const cardsById = new Map(cards.map((card) => [card.id, card]));

  function selectCard(id) {
    const card = cardsById.get(id) || cards[0];
    panel.classList.toggle("has-selection", card !== cards[0]);
    for (const item of cards) {
      item.classList.toggle("is-selected", item === card);
      item.setAttribute("aria-hidden", item === card ? "false" : "true");
    }
    for (const link of links) {
      const isActive = link.getAttribute("data-detail-target") === card.id;
      link.setAttribute("aria-current", isActive ? "true" : "false");
      const row = link.closest("[data-job-id]");
      if (row) row.classList.toggle("is-selected", isActive);
    }
  }

  function selectFromHash() {
    const id = window.location.hash.slice(1);
    selectCard(cardsById.has(id) ? id : cards[0].id);
  }

  for (const link of links) {
    link.addEventListener("click", (event) => {
      const id = link.getAttribute("data-detail-target");
      if (!id || !cardsById.has(id)) return;
      event.preventDefault();
      selectCard(id);
      if (window.location.hash !== `#${id}`) history.pushState(null, "", `#${id}`);
    });
  }
  window.addEventListener("hashchange", selectFromHash);
  window.addEventListener("popstate", selectFromHash);
  selectFromHash();
})();
</script>"""

def _copy_report_script() -> str:
    return """<script>
document.addEventListener('click', async (event) => {
  const target = event.target;
  const button = target instanceof Element ? target.closest('[data-copy-report]') : null;
  if (!button) return;
  const form = document.getElementById('resume-prompt-payload');
  const status = document.getElementById(button.getAttribute('aria-describedby'));
  if (!form) return;
  button.disabled = true;
  if (status) status.textContent = 'Generating Markdown...';
  try {
    const response = await fetch(button.dataset.copyReport, {
      method: 'POST',
      body: new FormData(form),
      headers: { 'Accept': 'text/markdown' },
    });
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    const markdown = await response.text();
    await navigator.clipboard.writeText(markdown);
    if (status) status.textContent = 'Markdown copied to clipboard.';
  } catch (error) {
    if (status) status.textContent = `Copy failed: ${error.message}`;
  } finally {
    button.disabled = false;
  }
});
</script>"""


def _intake_page(jobs: list[JobRecord]) -> str:
    job_count = len(jobs)
    latest = html.escape(jobs[0].discovered_at or jobs[0].date_posted or "No stored jobs") if jobs else "No stored jobs"
    ready_count = sum(1 for job in jobs if _job_storage_state(job) == "Stored structured record")
    review_count = max(job_count - ready_count, 0)
    recent_rows = "\n".join(_compact_job_row(job) for job in jobs[:8]) or '<tr><td colspan="9">No scraped jobs found. Run job-scraper run-once first.</td></tr>'
    return f"""<section id="input-strip" class="grid intake-grid">
  <form id="resume-upload" method="post" enctype="multipart/form-data" action="/matches" aria-labelledby="strategy-title">
    <span class="eyebrow">First-time action</span>
    <h2 id="strategy-title">Import a resume first</h2>
    <p class="muted">Upload the resume to parse candidate evidence, then score stored jobs. Filters are optional refinements after the resume is understood.</p>
    <section class="resume-primary" aria-label="Resume import">
      <label for="resume-file">Resume file</label>
      <input id="resume-file" name="resume_file" type="file" accept=".pdf,.tex,.latex,.txt,.md" required>
      <span class="hint">PDF, LaTeX, text, or Markdown. The parser extracts roles, skills, projects, metrics, education, certifications, gaps, and ambiguous claims before report generation.</span>
      <button id="resume-upload-btn" type="submit">Import resume and score stored jobs</button>
    </section>
    <details class="secondary-filters">
      <summary>Optional filters and scoring boosts</summary>
      <label for="target_roles">Target roles</label>
      <input id="target_roles" name="target_roles" type="text" placeholder="Frontend Engineer, Product Engineer, Data Platform">
      <span class="hint">Optional; comma-separated role titles or functions.</span>
      <label for="target_industries">Industries or subsectors</label>
      <input id="target_industries" name="target_industries" type="text" placeholder="Healthcare, Fintech, Climate, Developer Tools">
      <span class="hint">Optional; used for category tabs and industry-fit scoring.</span>
      <label for="keywords">Filter keywords</label>
      <input id="keywords" name="keywords" type="text" placeholder="React, TypeScript, Python, remote, platform">
      <span class="hint">Optional; boosts and filters the top match list using stored structured job details.</span>
    </details>
  </form>
  <section class="panel">
    <span class="eyebrow">Scrape and storage status</span>
    <div class="grid metrics">
      <div class="metric"><span class="muted">Jobs stored</span><strong>{job_count}</strong></div>
      <div class="metric"><span class="muted">Structured records</span><strong>{ready_count}</strong></div>
      <div class="metric"><span class="muted">Need review</span><strong>{review_count}</strong></div>
      <div class="metric"><span class="muted">Latest signal</span><strong>{latest}</strong></div>
    </div>
    <p class="hint">Filters, detail cards, prompt generation, Markdown reports, and API consumers read from the same stored job records.</p>
    <h3>Recent scraped roles</h3>
    <table>
      <thead><tr><th>Role</th><th>Company</th><th>Domain</th><th>Work model</th><th>Country</th><th>Posted</th><th>Parse state</th><th>Source</th><th>Actions</th></tr></thead>
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
  <p class="muted">Scoring brief: {html.escape(terms)}. Top match score: <span class="score">{top_score}</span>. Job details and Markdown actions remain pinned beside the result list.</p>
  <nav id="category-tabs" class="tabs tab-bar" aria-label="Industry categories">{category_tabs}</nav>
  <div class="grid analysis-grid">{analysis_cards}</div>
</section>
<section class="results-shell" aria-label="Scored job results and details">
  <section id="job-table-container" class="table-panel">
    <form class="table-tools" method="post" enctype="multipart/form-data" action="/matches">
      <div><label for="resume_refine">Resume</label><input id="resume_refine" name="resume_file" type="file" accept=".pdf,.tex,.latex,.txt,.md" required><button type="submit">Re-score</button></div>
      <div><label for="target_roles_refine">Optional roles</label><input id="target_roles_refine" name="target_roles" type="text" value="{html.escape(', '.join(target_roles))}"></div>
      <div><label for="target_industries_refine">Optional industries</label><input id="target_industries_refine" name="target_industries" type="text" value="{html.escape(', '.join(target_industries))}"></div>
      <div><label for="keywords_refine">Optional keywords</label><input id="keywords_refine" name="keywords" type="text" value="{html.escape(', '.join(keywords))}"></div>
    </form>
    {category_tables}
  </section>
  <aside id="detail-panel" class="detail-stack" aria-label="Selected job details">
    {detail_cards}
  </aside>
</section>
<form id="resume-prompt-payload" hidden aria-hidden="true" method="post">{hidden_resume}</form>"""


def _table_value(job: JobRecord, keys: tuple[str, ...], value: object) -> str:
    return _format_detail_text(value) or _missing_field_reason(job, keys)


def _compact_job_row(job: JobRecord) -> str:
    job_path_id = quote(job.theirstack_id, safe="")
    quality = _job_parse_quality(job)
    parse_state = f"{quality['storage_state']} · {quality['present_fields']}/{quality['total_fields']} fields"
    return f"""<tr>
  <td><a href="/jobs/{job_path_id}">{html.escape(_table_value(job, ("job_title", "title"), job.title))}</a></td>
  <td>{html.escape(_table_value(job, ("company_name", "company"), job.company))}</td>
  <td>{html.escape(_table_value(job, ("company_domain", "domain"), _job_company_domain(job)))}</td>
  <td>{html.escape(_table_value(job, ("employment_statuses", "workplace", "work_model", "remote"), _job_work_model(job)))}</td>
  <td>{html.escape(_table_value(job, ("job_country_code", "country_code"), job.country_code))}</td>
  <td>{html.escape(_table_value(job, ("date_posted", "discovered_at", "posted_at"), job.date_posted or job.discovered_at))}</td>
  <td>{html.escape(parse_state)}</td>
  <td>{html.escape(_job_source_label(job))}</td>
  <td class="table-actions"><a class="button ghost-button" href="/jobs/{job_path_id}">Details</a><a class="button ghost-button" href="/jobs/{job_path_id}/prompt">Tailor prompt</a></td>
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
    <thead><tr><th>Score</th><th>Role</th><th>Company</th><th>Metadata</th><th>Matched</th><th>Missing</th><th>Details</th></tr></thead>
    <tbody>{rows}</tbody>
  </table>
</section>"""


def _result_row(scored: object) -> str:
    job = getattr(scored, "job")
    detail_id = _job_detail_id(job)
    matched_values = getattr(scored, "matched_terms", ()) or getattr(scored, "key_strengths", ())
    missing_values = getattr(scored, "missing_requirements", ()) or getattr(scored, "missing_terms", ())
    matched = "".join(f'<span class="badge">{html.escape(str(term))}</span>' for term in matched_values[:4])
    missing = "".join(f'<span class="badge gap">{html.escape(str(term))}</span>' for term in missing_values[:4])
    return f"""<tr class="job-row {_score_tier(float(getattr(scored, "score", 0.0)))}" data-job-id="{html.escape(job.theirstack_id)}">
  <td class="score">{int(round(float(getattr(scored, "score", 0.0))))}</td>
  <td><a href="#{detail_id}" data-detail-target="{detail_id}" aria-controls="{detail_id}">{html.escape(_table_value(job, ("job_title", "title"), job.title))}</a></td>
  <td>{html.escape(_table_value(job, ("company_name", "company"), job.company))}</td>
  <td>{_job_metadata_badges(job, scored)}</td>
  <td>{matched or '<span class="muted">None</span>'}</td>
  <td>{missing or '<span class="muted">None</span>'}</td>
  <td><a class="button ghost-button" href="#{detail_id}" data-detail-target="{detail_id}" aria-controls="{detail_id}">Open</a></td>
</tr>"""


def _open_source_action(job: JobRecord) -> str:
    url = _job_listing_url(job)
    if not url:
        return ""
    return f'<a class="button ghost-button" href="{html.escape(url)}" target="_blank" rel="noopener noreferrer">Open source</a>'


def _job_detail_page(job: JobRecord) -> str:
    job_path_id = quote(job.theirstack_id, safe="")
    actions = f"""<div class="detail-actions">
  <a class="button ghost-button" href="/">Back to dashboard</a>
  <a class="button" href="/jobs/{job_path_id}/prompt">Tailor prompt</a>
  {_open_source_action(job)}
  <span class="badge">Saved in structured storage</span>
</div>"""
    return f"""<article class="detail-card detail-pane is-selected" id="{_job_detail_id(job)}">
  {_job_detail_body(job, scored=None, actions=actions)}
</article>"""


def _detail_card(scored: object) -> str:
    job = getattr(scored, "job")
    action = f"/jobs/{quote(job.theirstack_id, safe='')}/improvement-report"
    status_id = f"copy-status-{_dom_id(job.theirstack_id)}"
    actions = f"""<div class="detail-actions action-row">
  <button class="download-prompt" type="submit" form="resume-prompt-payload" formaction="{action}" formmethod="post">Download Markdown review report</button>
  <button class="ghost-button copy-report" type="button" data-copy-report="{action}" aria-describedby="{status_id}">Copy Markdown report</button>
  <span id="{status_id}" class="copy-status" role="status" aria-live="polite"></span>
  {_open_source_action(job)}
  <a class="button ghost-button" href="/jobs/{quote(job.theirstack_id, safe='')}">Open full details</a>
  <span class="badge">Saved in structured storage</span>
</div>"""
    return f"""<article class="detail-card detail-pane" id="{_job_detail_id(job)}">
  {_job_detail_body(job, scored=scored, actions=actions)}
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


def _job_detail_payload(job: JobRecord) -> dict[str, object]:
    quality = _job_parse_quality(job)
    fields = {
        "title": _field_payload(job, "Job title", ("job_title", "title"), normalized=job.title),
        "company": _field_payload(job, "Company", ("company_name", "company"), normalized=job.company),
        "location": _value_payload(job, "Location", _job_location(job), ("location", "job_location", "workplace", "job_country_code", "remote")),
        "compensation": _value_payload(job, "Compensation", _job_compensation_label(job), ("compensation", "salary", "min_annual_salary_usd", "max_annual_salary_usd")),
        "employment_type": _field_payload(job, "Employment type", ("employment_statuses", "employment_type", "employment_status")),
        "seniority": _field_payload(job, "Seniority level", ("job_seniority", "seniority", "seniority_level")),
        "responsibilities": _field_payload(job, "Core responsibilities", ("responsibilities", "job_responsibilities", "core_responsibilities")),
        "required_qualifications": _field_payload(job, "Required qualifications", ("requirements", "job_requirements", "required_qualifications", "minimum_qualifications", "qualifications", "candidate_requirements")),
        "preferred_qualifications": _field_payload(job, "Preferred qualifications", ("preferred_qualifications", "nice_to_have", "preferred_skills", "bonus_points")),
        "technologies": _field_payload(job, "Technologies and keywords", ("technologies", "technology_stack", "tech_stack", "tools", "skills", "keywords", "tools_mentioned", "job_categories")),
        "source_url": _value_payload(job, "Source URL", _job_listing_url(job), ("source_url", "url", "link")),
        "scraped_timestamp": _value_payload(job, "Scraped timestamp", job.discovered_at or job.date_posted or "", ("discovered_at", "date_posted", "posted_at")),
    }
    return {
        "id": job.theirstack_id,
        "storage_state": _job_storage_state(job),
        "parse_quality": quality,
        "fields": fields,
        "raw": job.raw,
    }


def _field_payload(
    job: JobRecord,
    label: str,
    keys: tuple[str, ...],
    *,
    normalized: object | None = None,
) -> dict[str, object]:
    value = _format_detail_text(normalized) if normalized not in (None, "", [], {}) else _format_detail_text(_job_raw_value(job, keys))
    return _value_payload(job, label, value, keys)


def _value_payload(job: JobRecord, label: str, value: object, keys: tuple[str, ...]) -> dict[str, object]:
    text = _format_detail_text(value)
    if text:
        return {"label": label, "status": "parsed", "value": text, "message": ""}
    message = _missing_field_reason(job, keys)
    return {"label": label, "status": _missing_status_class(message), "value": None, "message": message}


def _detail_fact(job: JobRecord, label: str, keys: tuple[str, ...], *, value: object | None = None, normalized: object | None = None) -> str:
    payload = _value_payload(job, label, value, keys) if value is not None else _field_payload(job, label, keys, normalized=normalized)
    display = payload["value"] if payload["status"] == "parsed" else _missing_badge(str(payload["message"]))
    return f"<p><strong>{html.escape(label)}:</strong> {html.escape(str(display)) if payload['status'] == 'parsed' else display}</p>"


def _detail_block(job: JobRecord, label: str, keys: tuple[str, ...], *, value: object | None = None) -> str:
    payload = _value_payload(job, label, value, keys) if value is not None else _field_payload(job, label, keys)
    if payload["status"] == "parsed":
        body = html.escape(str(payload["value"]))
    else:
        body = _missing_badge(str(payload["message"]))
    return f"""<h3>{html.escape(label)}</h3>
  <div class="description">{body}</div>"""


def _missing_badge(message: str) -> str:
    return f'<span class="missing-field">{html.escape(message)}</span>'


def _missing_status_class(message: str) -> str:
    normalized = message.casefold()
    if "pending" in normalized or "not yet" in normalized:
        return "pending"
    if "failed" in normalized:
        return "parser_failed"
    if "not separately parsed" in normalized:
        return "not_separately_parsed"
    return "absent_in_source"


def _missing_field_reason(job: JobRecord, keys: tuple[str, ...]) -> str:
    status = _parse_status(job).casefold()
    if status in {"pending", "queued", "not_parsed", "not parsed", "unparsed"}:
        return "Not yet parsed"
    if status in {"failed", "error"} or _job_raw_value(job, ("parser_error", "parse_error", "extraction_error")):
        return "Parser failed"
    diagnostics = _job_raw_value(job, ("missing_field_diagnostics", "parser_warnings", "parse_warnings", "missing_fields"))
    if _diagnostics_mentions(diagnostics, keys):
        return "Parser failed"
    if _is_detail_subfield(keys) and _job_description(job):
        return "Not separately parsed from listing description"
    if _is_detail_subfield(keys) and _job_source(job) == "public_json":
        return "Not included in public source feed"
    return "Not present in source"


def _is_detail_subfield(keys: tuple[str, ...]) -> bool:
    detail_keys = {
        "responsibilities",
        "job_responsibilities",
        "core_responsibilities",
        "role_responsibilities",
        "requirements",
        "job_requirements",
        "required_qualifications",
        "minimum_qualifications",
        "qualifications",
        "candidate_requirements",
        "preferred_qualifications",
        "nice_to_have",
        "preferred_skills",
        "bonus_points",
    }
    return any(key in detail_keys for key in keys)


def _diagnostics_mentions(diagnostics: object, keys: tuple[str, ...]) -> bool:
    if diagnostics in (None, "", [], {}):
        return False
    text = _normalize_space(_format_detail_text(diagnostics)).casefold()
    return any(key.casefold() in text for key in keys)


def _parse_status(job: JobRecord) -> str:
    raw_status = _job_raw_value(job, ("parse_status", "parser_status", "extraction_status"))
    if raw_status not in (None, "", [], {}):
        return str(raw_status)
    if _job_raw_value(job, ("parser_error", "parse_error", "extraction_error")):
        return "failed"
    return "parsed"


def _job_storage_state(job: JobRecord) -> str:
    status = _parse_status(job).casefold()
    if status in {"pending", "queued", "not_parsed", "not parsed", "unparsed"}:
        return "Stored; parse pending"
    if status in {"failed", "error"}:
        return "Stored; parser needs review"
    return "Stored structured record"


def _job_parse_quality(job: JobRecord) -> dict[str, object]:
    checks: tuple[tuple[str, str, tuple[str, ...]], ...] = (
        ("Job title", _format_detail_text(job.title), ("job_title", "title")),
        ("Company", _format_detail_text(job.company), ("company_name", "company")),
        ("Location", _job_location(job), ("location", "job_location", "workplace", "job_country_code", "remote")),
        ("Compensation", _job_compensation_label(job), ("compensation", "salary", "min_annual_salary_usd", "max_annual_salary_usd")),
        ("Employment type", _job_text_value(job, ("employment_statuses", "employment_type", "employment_status")), ("employment_statuses", "employment_type", "employment_status")),
        ("Seniority level", _job_text_value(job, ("job_seniority", "seniority", "seniority_level")), ("job_seniority", "seniority", "seniority_level")),
        ("Core responsibilities", _job_responsibilities(job), ("responsibilities", "job_responsibilities", "core_responsibilities")),
        ("Required qualifications", _job_requirements(job), ("requirements", "job_requirements", "required_qualifications", "minimum_qualifications", "qualifications", "candidate_requirements")),
        ("Preferred qualifications", _job_preferred_qualifications(job), ("preferred_qualifications", "nice_to_have", "preferred_skills", "bonus_points")),
        ("Technologies and keywords", _job_technologies(job), ("technologies", "technology_stack", "tech_stack", "tools", "skills", "keywords", "tools_mentioned", "job_categories")),
        ("Source URL", _job_listing_url(job), ("source_url", "url", "link")),
        ("Scraped timestamp", job.discovered_at or job.date_posted or "", ("discovered_at", "date_posted", "posted_at")),
    )
    missing = [
        {"field": label, "reason": _missing_field_reason(job, keys)}
        for label, value, keys in checks
        if not _format_detail_text(value)
    ]
    present = len(checks) - len(missing)
    computed_confidence = round(present / len(checks), 2)
    confidence = _job_parser_confidence(job, fallback=computed_confidence)
    return {
        "status": _parse_status(job),
        "storage_state": _job_storage_state(job),
        "present_fields": present,
        "total_fields": len(checks),
        "confidence": confidence,
        "missing_fields": missing,
    }


def _job_parser_confidence(job: JobRecord, *, fallback: float) -> float:
    value = _job_raw_value(job, ("parser_confidence", "parse_confidence", "confidence"))
    if value in (None, "", [], {}):
        confidence = fallback
    else:
        try:
            raw_confidence = float(value)
        except (TypeError, ValueError):
            confidence = fallback
        else:
            confidence = raw_confidence / 100.0 if raw_confidence > 1 else raw_confidence
    return round(min(max(confidence, 0.0), 1.0), 2)


def _parse_quality_panel(job: JobRecord) -> str:
    quality = _job_parse_quality(job)
    missing = quality["missing_fields"]
    missing_items = ""
    if isinstance(missing, list) and missing:
        missing_items = "".join(
            f"<li><strong>{html.escape(str(item['field']))}:</strong> {html.escape(str(item['reason']))}</li>"
            for item in missing[:8]
            if isinstance(item, dict)
        )
    else:
        missing_items = "<li>All required detail fields are populated.</li>"
    percent = int(round(float(quality["confidence"]) * 100))
    return f"""<section class="parse-quality" aria-label="Parse and storage quality">
    <span class="badge">Storage: {html.escape(str(quality["storage_state"]))}</span>
    <span class="badge">Parse status: {html.escape(str(quality["status"]))}</span>
    <span class="badge">Parser confidence: {percent}%</span>
    <span class="badge">Fields parsed: {quality["present_fields"]}/{quality["total_fields"]}</span>
    <details>
      <summary>Missing-field diagnostics</summary>
      <ul>{missing_items}</ul>
    </details>
  </section>"""


def _job_detail_body(job: JobRecord, *, scored: object | None, actions: str) -> str:
    description = _job_description(job)
    title = _format_detail_text(job.title) or f"Stored job {job.theirstack_id}"
    listing_url = _job_listing_url(job)
    application_url = _job_application_url(job)
    application_link = ""
    if application_url and application_url != listing_url:
        application_link = f'<p><strong>Application link:</strong> {_job_link(application_url)}</p>'
    return f"""<span class="eyebrow">Selected role</span>
  <h2 class="detail-title">{html.escape(title)}</h2>
  {_parse_quality_panel(job)}
  <div class="detail-meta">
    {_detail_fact(job, "Company", ("company_name", "company"), normalized=job.company)}
    {_detail_fact(job, "Company domain", ("company_domain", "domain"), value=_job_company_domain(job))}
    {_detail_fact(job, "Location", ("location", "job_location", "workplace", "job_country_code", "remote"), value=_job_location(job))}
    {_detail_fact(job, "Work model", ("employment_statuses", "workplace", "work_model", "remote"), value=_job_work_model(job, scored))}
    {_detail_fact(job, "Country", ("job_country_code", "country_code"), normalized=job.country_code)}
    {_detail_fact(job, "Scraped timestamp", ("discovered_at", "date_posted", "posted_at"), value=job.discovered_at or job.date_posted or "")}
    {_detail_fact(job, "Seniority level", ("job_seniority", "seniority", "seniority_level"))}
    {_detail_fact(job, "Employment type", ("employment_statuses", "employment_type", "employment_status"))}
    {_detail_fact(job, "Compensation", ("compensation", "salary", "min_annual_salary_usd", "max_annual_salary_usd"), value=_job_compensation_label(job))}
    {_score_metadata(scored)}
    <p><strong>Source:</strong> {html.escape(_job_source_label(job))}</p>
    <p><strong>Original listing:</strong> {_job_link(listing_url)}</p>
    {application_link}
  </div>
  {actions}
  {_detail_block(job, "Description", ("job_description", "description", "job_text", "summary", "overview"), value=description)}
  {_detail_block(job, "Core responsibilities", ("responsibilities", "job_responsibilities", "core_responsibilities"), value=_job_responsibilities(job))}
  {_detail_block(job, "Required qualifications / Requirements", ("requirements", "job_requirements", "required_qualifications", "minimum_qualifications", "qualifications", "candidate_requirements"), value=_job_requirements(job))}
  {_detail_block(job, "Preferred qualifications", ("preferred_qualifications", "nice_to_have", "preferred_skills", "bonus_points"), value=_job_preferred_qualifications(job))}
  {_detail_block(job, "Technologies, tools, domains, and keywords", ("technologies", "technology_stack", "tech_stack", "tools", "skills", "keywords", "tools_mentioned", "job_categories"), value=_job_technologies(job))}
  {_source_signal_block(job)}"""


def _score_metadata(scored: object | None) -> str:
    if scored is None:
        return '<p><strong>Score / match:</strong> <span class="muted">Not scored in this view. Upload a resume to see match metadata.</span></p>'
    matched_values = getattr(scored, "matched_terms", ()) or getattr(scored, "key_strengths", ())
    missing_values = getattr(scored, "missing_requirements", ()) or getattr(scored, "missing_terms", ())
    matched = "".join(f'<span class="badge">{html.escape(str(term))}</span>' for term in matched_values[:8])
    missing = "".join(f'<span class="badge gap">{html.escape(str(term))}</span>' for term in missing_values[:8])
    return f"""<p><strong>Category:</strong> {html.escape(str(getattr(scored, "category", "Uncategorized")))}</p>
    <p><strong>Category fit:</strong> {html.escape(str(getattr(scored, "category_fit", "")))}</p>
    <p><strong>Score:</strong> <span class="score">{int(round(float(getattr(scored, "score", 0.0))))}</span></p>
    <p><strong>Region:</strong> {html.escape(str(getattr(scored, "region", "") or getattr(scored, "remote_label", "")))}</p>
    <p><strong>Why this rank:</strong> {html.escape(str(getattr(scored, "explanation", "")))}</p>
    <p><strong>Matched:</strong> {matched or '<span class="muted">None</span>'}</p>
    <p><strong>Missing:</strong> {missing or '<span class="muted">None</span>'}</p>
    <h3>Key strengths</h3>
    <ul>{_list_items(getattr(scored, "key_strengths", ()), "No supported strengths found.")}</ul>
    <h3>Missing requirements</h3>
    <ul>{_list_items(getattr(scored, "missing_requirements", ()), "No missing requirements detected.")}</ul>
    <h3>Relevant resume evidence</h3>
    <ul>{_list_items(getattr(scored, "relevant_resume_evidence", ()), "No direct resume evidence found.")}</ul>
    <h3>Concerns</h3>
    <ul>{_list_items(getattr(scored, "concerns", ()), "No major concerns detected.")}</ul>"""


def _job_description(job: JobRecord) -> str:
    return _format_detail_text(_job_raw_value(job, ("job_description", "description", "job_text", "summary", "overview")))


def _job_responsibilities(job: JobRecord) -> str:
    return _format_detail_text(
        _job_raw_value(job, ("responsibilities", "job_responsibilities", "core_responsibilities", "role_responsibilities"))
    )


def _job_requirements(job: JobRecord) -> str:
    return _format_detail_text(
        _job_raw_value(
            job,
            (
                "requirements",
                "job_requirements",
                "required_qualifications",
                "minimum_qualifications",
                "qualifications",
                "candidate_requirements",
                "skills",
            ),
        )
    )


def _job_preferred_qualifications(job: JobRecord) -> str:
    return _format_detail_text(
        _job_raw_value(job, ("preferred_qualifications", "nice_to_have", "preferred_skills", "bonus_points"))
    )


def _job_technologies(job: JobRecord) -> str:
    return _format_detail_text(
        _job_raw_value(job, ("technologies", "technology_stack", "tech_stack", "tools", "skills", "keywords", "tools_mentioned", "job_categories"))
    )


def _source_signal_block(job: JobRecord) -> str:
    signals = _source_signals(job)
    if not signals:
        return ""
    return f"""<h3>Source-provided role signals</h3>
  <div class="description">{html.escape(_format_detail_text(signals))}</div>"""


def _source_signals(job: JobRecord) -> dict[str, object]:
    signals: dict[str, object] = {}
    for label, keys in (
        ("Title group", ("title_group",)),
        ("Job categories", ("job_categories",)),
        ("Seniority level", ("seniority_level", "job_seniority", "seniority")),
        ("Employment type", ("employment_type", "employment_statuses")),
        ("Tools mentioned", ("tools_mentioned", "tools", "technologies")),
        ("Cloud providers mentioned", ("cloud_providers_mentioned",)),
        ("Pain points detected", ("pain_points_detected",)),
        ("Job language", ("job_language",)),
    ):
        value = _job_raw_value(job, keys)
        if value not in (None, "", [], {}):
            signals[label] = value
    return signals


def _job_location(job: JobRecord) -> str:
    raw_location = _job_raw_value(job, ("location", "job_location", "workplace"))
    if isinstance(raw_location, dict):
        parts = [
            str(raw_location[key]).strip()
            for key in ("city", "state", "region", "country")
            if raw_location.get(key) not in (None, "")
        ]
    else:
        parts = [str(raw_location).strip()] if raw_location not in (None, "") else []
    if job.country_code and job.country_code not in parts:
        parts.append(job.country_code)
    if job.remote == 1 and "Remote" not in parts:
        parts.append("Remote")
    elif job.remote == 0 and not any(part.casefold() in {"on-site", "onsite", "hybrid"} for part in parts):
        parts.append("On-site")
    return ", ".join(part for part in parts if part)


def _job_source(job: JobRecord) -> str:
    source = _job_raw_value(job, ("source", "ats", "job_source"))
    if source:
        return str(source)
    if isinstance(job.raw.get("public_json"), dict):
        return "public_json"
    return "Stored job"


def _job_listing_url(job: JobRecord) -> str:
    return _clean_url(job.source_url or job.url or _job_raw_value(job, ("source_url", "url", "link")))


def _job_application_url(job: JobRecord) -> str:
    return _clean_url(job.final_url or _job_raw_value(job, ("final_url", "link_final_url", "application_url")))

def _job_company_domain(job: JobRecord) -> str:
    return job.company_domain or _job_text_value(job, ("company_domain", "domain"))


def _job_text_value(job: JobRecord, keys: tuple[str, ...]) -> str:
    value = _job_raw_value(job, keys)
    if isinstance(value, (list, tuple)):
        return ", ".join(str(item).strip() for item in value if str(item).strip())
    return _format_detail_text(value).replace("\n", ", ")


def _job_source_label(job: JobRecord) -> str:
    for url in (_job_listing_url(job), _job_application_url(job)):
        host = urlparse(url).netloc.removeprefix("www.")
        if host:
            return host
    domain = _job_company_domain(job)
    if domain:
        return domain
    return _job_source(job)


def _job_work_model(job: JobRecord, scored: object | None = None) -> str:
    remote_label = str(getattr(scored, "remote_label", "") or "").strip() if scored is not None else ""
    if remote_label and remote_label != "Unknown":
        return remote_label
    raw_model = _job_text_value(job, ("employment_statuses", "workplace", "work_model", "remote")).casefold()
    if "remote" in raw_model:
        return "Remote"
    if "hybrid" in raw_model:
        return "Hybrid"
    if "on-site" in raw_model or "onsite" in raw_model:
        return "On-site"
    if job.remote == 1:
        return "Remote"
    if job.remote == 0:
        return "On-site"
    return ""


def _job_salary_label(job: JobRecord) -> str:
    min_salary = _salary_thousands(_job_raw_value(job, ("min_annual_salary_usd", "min_salary_usd", "salary_min_usd")))
    max_salary = _salary_thousands(_job_raw_value(job, ("max_annual_salary_usd", "max_salary_usd", "salary_max_usd")))
    if min_salary and max_salary:
        return f"${min_salary}k-${max_salary}k"
    if min_salary:
        return f"${min_salary}k+"
    if max_salary:
        return f"Up to ${max_salary}k"
    return ""


def _job_compensation_label(job: JobRecord) -> str:
    raw_compensation = _job_raw_value(job, ("compensation", "salary", "salary_range", "pay"))
    if raw_compensation not in (None, "", [], {}):
        text = _format_detail_text(raw_compensation).replace("\n", ", ")
        confidence = _job_raw_value(job, ("compensation_confidence", "salary_confidence"))
        if confidence not in (None, "", [], {}):
            text = f"{text} (confidence: {confidence})"
        return text
    salary = _job_salary_label(job)
    if salary:
        return f"{salary} USD/year"
    return ""


def _salary_thousands(value: object) -> int:
    if value is None or isinstance(value, bool):
        return 0
    try:
        return int(round(float(value) / 1000))
    except (TypeError, ValueError):
        return 0


def _job_metadata_badges(job: JobRecord, scored: object | None = None) -> str:
    badge_values: list[tuple[str, str]] = []
    work = _job_work_model(job, scored)
    badge_values.append(("Work", work or _missing_field_reason(job, ("employment_statuses", "workplace", "work_model", "remote"))))
    region = str(getattr(scored, "region", "") or "").strip() if scored is not None else ""
    if region and region != "Unknown":
        badge_values.append(("Region", region))
    badge_values.append(("Country", job.country_code or _missing_field_reason(job, ("job_country_code", "country_code"))))
    badge_values.append(("Posted", job.date_posted or job.discovered_at or _missing_field_reason(job, ("date_posted", "discovered_at", "posted_at"))))
    seniority = _job_text_value(job, ("job_seniority", "seniority", "seniority_level"))
    if seniority:
        badge_values.append(("Seniority", seniority))
    status = _job_text_value(job, ("employment_statuses", "employment_type", "employment_status"))
    if status:
        badge_values.append(("Status", status))
    salary = _job_salary_label(job)
    if salary:
        badge_values.append(("Salary", salary))
    badge_values.append(("Source", _job_source_label(job)))
    return "".join(
        f'<span class="badge">{html.escape(label)}: {html.escape(value)}</span>'
        for label, value in badge_values
    )


def _job_link(url: str | None) -> str:
    if not url:
        return '<span class="muted">No URL found</span>'
    escaped_url = html.escape(url)
    return f'<a class="external-url" href="{escaped_url}" target="_blank" rel="noopener noreferrer">{escaped_url}</a>'


def _job_raw_value(job: JobRecord, keys: tuple[str, ...]) -> object:
    for raw in _job_raw_sources(job):
        for key in keys:
            value = raw.get(key)
            if value not in (None, "", [], {}):
                return value
    return None


def _job_raw_sources(job: JobRecord) -> list[dict[str, object]]:
    sources: list[dict[str, object]] = [job.raw]
    public_json = job.raw.get("public_json")
    if isinstance(public_json, dict):
        sources.append(public_json)
        nested_raw = public_json.get("raw")
        if isinstance(nested_raw, dict):
            sources.append(nested_raw)
            extra_fields = nested_raw.get("extra_public_fields")
            if isinstance(extra_fields, dict):
                sources.append(extra_fields)
    return sources


def _format_detail_text(value: object) -> str:
    if value in (None, "", [], {}):
        return ""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, (list, tuple)):
        return "\n".join(f"- {_format_detail_text(item)}" for item in value if _format_detail_text(item))
    if isinstance(value, dict):
        lines = []
        for key, item in value.items():
            text = _format_detail_text(item)
            if text:
                lines.append(f"{str(key).replace('_', ' ').title()}: {text}")
        return "\n".join(lines)
    return str(value).strip()


def _normalize_space(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


def _clean_url(value: object) -> str:
    if not isinstance(value, str):
        return ""
    return value.strip()


def _list_items(values: object, fallback: str) -> str:
    if not isinstance(values, (list, tuple)) or not values:
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
    values = _ranked_values(
        strength
        for job in getattr(category, "top_jobs", ())
        for strength in getattr(job, "key_strengths", ()) or getattr(job, "matched_terms", ())
    )
    return values


def _category_gaps(category: object) -> list[str]:
    values = _ranked_values(
        gap
        for job in getattr(category, "top_jobs", ())
        for gap in getattr(job, "missing_requirements", ()) or getattr(job, "missing_terms", ())
    )
    return values


def _ranked_values(values: object) -> list[str]:
    counts: dict[str, int] = {}
    for value in values:
        text = str(value).strip()
        if text:
            counts[text] = counts.get(text, 0) + 1
    return [value for value, _count in sorted(counts.items(), key=lambda item: (-item[1], item[0]))[:4]]


def _resume_fact_summary(*, filename: str, kind: str, text: str) -> str:
    return facts_markdown_for_text(filename=filename, kind=kind, text=text)
