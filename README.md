# jobs-assistant

Minimal local job scraper/backlog ingestion assistant.

The current app imports and dedupes job listings into a local SQLite backlog. A guarded application-draft loop remains target/future work: it should run through OMP/workflowz, discover relevant listings, perform resume/profile keyword matching against job descriptions, and prepare supported ATS application drafts for manual review without submitting them.

## Current workflow

- Discovers listings from TheirStack helper code, JSON fixtures, or a normalized `/v1/jobs` feed.
- Dedupes and filters roles into a local SQLite backlog.
- Keeps application automation safety policy and future contracts documented without treating archived applier code as active implementation.

## Target workflow

- Greenhouse-first ATS draft filling through an extensible browser-adapter path.
- Puppeteer or an equivalent browser-adapter layer fills safe supported fields only.
- Job descriptions drive resume/profile keyword matching and draft application context.
- Run artifacts, fixtures, screenshots/log evidence, and preference notes are preserved for iteration.
- The workflow stops before final submit every time; the user manually reviews and submits any completed draft.

The old browser/applier implementation is archived; future application-draft work should be rebuilt around guarded ATS adapters.

## Quick start

```bash
uv run --frozen jobs-assistant init-db
THEIRSTACK_API_KEY=... uv run --frozen jobs-assistant theirstack-preview
THEIRSTACK_API_KEY=... uv run --frozen jobs-assistant theirstack-sync --paid-fetch --limit 25
THEIRSTACK_API_KEY=... THEIRSTACK_ENABLE_PAID_FETCH=true uv run --frozen job-scrape --profile new_grad_cs --count 1
```

Default database:

```text
data/jobs.sqlite3
```

## Import from an API feed

If a scraper/feed service exposes jobs at `/v1/jobs`:

```bash
uv run --frozen jobs-assistant import-feed --base-url https://your-feed.example
```

Or via environment:

```bash
export JOB_SOURCE_BASE_URL=https://your-feed.example
export JOB_SOURCE_API_KEY=your-key-if-needed
uv run --frozen jobs-assistant import-feed
```

## Commands

| Command | Description |
|---|---|
| `init-db` | Initialize the SQLite jobs/sync database |
| `theirstack-preview` | Preview filtered TheirStack match count without saving jobs |
| `theirstack-sync` | Pull filtered TheirStack jobs into the backlog; requires `--paid-fetch` or `THEIRSTACK_ENABLE_PAID_FETCH=true` |
| `job-scrape` | Compatibility wrapper for `theirstack-sync`; `--count` maps to TheirStack paid-fetch `limit`, and SQLite dedupe still removes duplicates by source job ID and canonical URL |
| `import-feed` | Import normalized jobs from a JSON fixture or `GET /v1/jobs` feed |
| `autofill` | Open queued job URLs with the guarded browser adapter and fill safe inferred fields only |
| `autofill-review` | Print recent manual/blocked autofill runs |

## Profiles and Resume Inputs

- Source profiles select TheirStack/source search filters, for example `job-scrape --source-profile new_grad_cs`; the compatibility alias `--profile` is retained for `job-scrape` and TheirStack commands.
- Application profiles are explicit applicant/application facts for safe form filling, passed with `autofill --application-profile-json`; compatibility alias `--profile-json` is still accepted. Identity and sensitive answers are never inferred from resume text.
- `resume/resume.json` provides structured non-sensitive resume metadata for tailoring context, limited to `skills`, `jobs`, `research`, `leadership`, and `education`. Source profiles, application profiles, and resume metadata are intentionally separate.

## Browser Adapter and Artifacts

`autofill` uses the Puppeteer browser-adapter path to observe supported ATS pages, resolve safe fields, execute guarded non-final actions, and stop before final submission. Local runs require the Python package plus the repository npm dependencies and Puppeteer-managed browser install:

```bash
npm install
npm run install-browser
uv run --frozen --extra live jobs-assistant autofill --limit 1 --application-profile-json path/to/application-profile.json --artifact-dir data/application-runs
```

When `--artifact-dir` is set, each run persists review evidence such as `observation.json`, `plan.json`, `actions.json`, `filled_state.json`, `job_description.txt` when source text is available, and `screenshot.png` when a browser screenshot is captured.

Default autofill LLM inference uses Ollama Cloud with:

```text
OLLAMA_CLOUD_MODEL=deepseek-v4-flash
OLLAMA_CLOUD_THINK=low
```

## Help

```bash
uv run --frozen jobs-assistant --help
uv run --frozen jobs-assistant theirstack-sync --help
uv run --frozen --extra live jobs-assistant autofill --help
uv run --frozen job-scrape --help
```

## Development checks

```bash
uv lock --check
uv run --frozen --extra dev python -m pytest
uv run --frozen jobs-assistant --help
```

Container smoke:

```bash
mkdir -p data
sudo chown -R "$(id -u):$(id -g)" data  # if Docker created ./data as root
docker compose build
docker compose up -d
docker compose ps
docker compose down
```

Optional browser-adapter diagnostic:

```bash
printf '{"action":"launch","headless":true}\n{"action":"close"}\n' \
  | docker compose run --rm --entrypoint node jobs-assistant src/jobs_assistant/puppeteer_runner.js \
  | python -c 'import json,sys; rows=[json.loads(line) for line in sys.stdin if line.strip()]; assert len(rows) == 3 and all(row.get("ok") for row in rows), rows; print(rows)'
```

The Docker image uses Debian `chromium-headless-shell` through Puppeteer for the browser-adapter diagnostic. The compose profile sets `PUPPETEER_NO_SANDBOX=1` because Docker Desktop can block Chromium's normal sandbox in this local smoke path. The container still runs as a non-root user. Prefer host execution or a hardened custom Docker profile for unknown/untrusted application pages.

Coding-agent practices and project policy live in `AGENTS.md`. Historical snapshots live under `archive/`.
