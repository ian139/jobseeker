# Jobs workspace

This workspace currently centers on the `scraper/` Python app: a TheirStack job scraper with SQLite dedupe, a 24-hour daemon, a local resume-prompt web UI, application-pack helpers, and a manual LinkedIn outreach queue.

## Fast path

Run commands from `scraper/`.

```bash
cd scraper
source .venv/bin/activate  # if the local venv exists
job-scraper --help
```

If `.venv/` is missing or the command is unavailable:

```bash
cd scraper
python -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[dev]'
```

## First-time setup

```bash
cd scraper
cp .env.example .env
cp config/filters.example.yaml config/filters.yaml
```

Edit `.env`:

- `JOB_SOURCE` defaults to `public-json`, which imports the public JSON feed without a TheirStack API key.
- `PUBLIC_JSON_BASE_URL` defaults to `https://doomersareretardedcommunists.com/`.
- `THEIRSTACK_API_KEY` is required only for `preview-count` or when `JOB_SOURCE=theirstack`.
- `JOB_SCRAPER_DB_PATH` defaults to `data/jobs.sqlite3`.
- `LLM_API_KEY` is optional. Without it, resume generation uses deterministic local output or can be forced with `--no-llm`.
- To use DeepSeek V4 Pro through OpenRouter, set `LLM_API_KEY=<openrouter key>`, `LLM_BASE_URL=https://openrouter.ai/api/v1`, and `LLM_MODEL=deepseek/deepseek-v4-pro`.

Edit `config/filters.yaml` only for TheirStack API pulls.

## Run the scraper

Initialize local SQLite:

```bash
job-scraper init
```

Estimate TheirStack matches without saving full results:

```bash
JOB_SOURCE=theirstack job-scraper preview-count --filters config/filters.yaml
```

Run one import from the configured source. By default this imports public JSON:

```bash
job-scraper run-once
```

Use TheirStack instead when desired:

```bash
JOB_SOURCE=theirstack job-scraper run-once --filters config/filters.yaml
```

Run the 24-hour puller for the configured source:

```bash
job-scraper daemon
```

## Use the local web UI

After `init` and at least one import/sync, start the browser UI:

```bash
job-scraper webui --host 127.0.0.1 --port 8000
```

Open <http://127.0.0.1:8000>. The UI lists saved jobs from SQLite and can generate a tailored-resume prompt from an uploaded resume file.

## Application workflow

Copy and edit the resume profile example:

```bash
cp config/resume-profile.example.yaml config/resume-profile.yaml
```

Generate a tailored Markdown resume for a saved job:

```bash
job-scraper generate-resume --job-id <THEIRSTACK_JOB_ID> --profile config/resume-profile.yaml --output data/resumes/<job-id>.md --no-llm
```

Create an application pack and local CRM row:

```bash
job-scraper prepare-application --job-id <THEIRSTACK_JOB_ID> --profile config/resume-profile.yaml --notes "Applied via company site" --no-llm
```

List and update application rows:

```bash
job-scraper list-applications
job-scraper update-application --job-id <THEIRSTACK_JOB_ID> --status applied --applied-at 2026-06-23
```

## Outreach workflow

Copy and edit the outreach sequence:

```bash
cp config/outreach.example.yaml config/outreach.yaml
```

Then run the manual queue commands:

```bash
job-scraper outreach init
job-scraper outreach import-contacts --csv config/contacts.csv
job-scraper outreach queue --config config/outreach.yaml
job-scraper outreach next --limit 5 --open
job-scraper outreach mark <ACTION_ID> --status sent
job-scraper outreach mark-contact --linkedin-url <PROFILE_URL> --status connected
```

This app records outreach steps; it does not automate LinkedIn clicks.

## Local files intentionally ignored

`scraper/.gitignore` ignores local secrets and generated data:

- `.env`
- `.venv/`
- `data/`
- `config/filters.yaml`
- Python caches and package metadata

Keep API keys, databases, generated resumes, and personal filters out of git.

## More detail

- `RUNBOOK.md` has the command-oriented operating guide.
- `TODO.md` has the setup and usage checklist.
- `notes/scraper-theirstack.md` contains earlier implementation notes and TheirStack-specific details.
