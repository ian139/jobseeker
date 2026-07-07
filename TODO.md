# jobs-assistant minimal roadmap

The active app is intentionally minimized to scraper/backlog ingestion, filtering/quality gates, profiles, and TheirStack-related work. The applier concept remains important, but the current implementation is archived and should be rebuilt later through OMP `workflowz`.

## Active scope

```text
TheirStack / source feed / scraper output
  ↓
Normalize to JobInput
  ↓
Filtering and quality gates
  ↓
SQLite jobs backlog
  ↓
Sync metadata and reviewable source payloads
```

## Set-in-stone active features

- [ ] Keep SQLite job backlog schema small and stable.
- [ ] Preserve canonical URL and source job ID dedupe.
- [ ] Preserve raw source payloads in `raw_json`.
- [ ] Preserve profile-shaped TheirStack search ideas.
- [ ] Keep TheirStack credit-safe preview before paid fetch.
- [ ] Keep paid fetch gated by explicit code/config opt-in before any credit-consuming call.
- [ ] Keep feed/fixture import for deterministic tests and backfills.
- [ ] Keep filtering/quality gates explicit and testable.

## Minimal next patches

- [ ] Add a first-class named application-profile config loader only when it is consumed by `autofill --application-profile-json` without overloading source filter profiles.
- [ ] Add non-API web scrapers only behind the existing `JobInput`/`import_source_jobs` boundary; LinkedIn must not bypass sign-in, CAPTCHA, or anti-abuse gates.
- [ ] Implement `scrape-url` for public, no-auth job pages by parsing `JobPosting` JSON-LD and common apply links into `JobInput`; block on sign-in/CAPTCHA; persist via `import_source_jobs(source="web_scraper")`; add fixtures for Greenhouse, Lever, and a generic company page.
- [ ] Add freshness/active-job checks only as injectable pure functions with fixtures.

## Archived applier concept

The prior active applier implementation was moved to:

```text
archive/minimized-20260706/applier/
```

It is reference-only and not a runnable package snapshot. It depended on root `contracts.py` and `db.py` before minimization.

Future applier rebuild must use OMP `workflowz` and separate subtasks for observer, resolver, executor, persistence, and safety review. No final-submit behavior may be added until a submit policy exists and is tested.

## Verification

```bash
uv lock --check
uv run --frozen --extra dev python -m pytest
uv run --frozen jobs-assistant --help
docker compose build
docker compose up -d
docker compose ps
docker compose down
```
