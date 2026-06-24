# TODO: running the Jobs app

Use this as the setup checklist for the `scraper/` app.

## Environment

- [ ] `cd scraper`
- [ ] Create or activate the virtualenv:
  - Existing local env: `source .venv/bin/activate`
  - Fresh env: `python -m venv .venv && source .venv/bin/activate`
- [ ] Install the app if needed: `python -m pip install -e '.[dev]'`
- [ ] Confirm the CLI loads: `job-scraper --help`

## Secrets and local config

- [ ] Copy `.env.example` to `.env`.
- [ ] Set `THEIRSTACK_API_KEY` in `.env` before live TheirStack calls.
- [ ] Decide whether to set `OPENAI_API_KEY`; leave blank or use `--no-llm` for deterministic resume output.
- [ ] Copy `config/filters.example.yaml` to `config/filters.yaml`.
- [ ] Review `config/filters.yaml` before running a paid sync.
- [ ] Keep `search.limit` at `25` or lower unless your TheirStack plan allows larger pages.

## Database and scraping

- [ ] Initialize SQLite: `job-scraper init`.
- [ ] Estimate volume: `job-scraper preview-count --filters config/filters.yaml`.
- [ ] Run one import: `job-scraper run-once --filters config/filters.yaml`.
- [ ] If the one-time run looks good, start the 24-hour daemon: `job-scraper daemon --filters config/filters.yaml`.

## Web UI

- [ ] Start the local UI: `job-scraper webui --host 127.0.0.1 --port 8000`.
- [ ] Open <http://127.0.0.1:8000>.
- [ ] If no jobs appear, run `job-scraper run-once` first or import supplemental public JSON jobs.

## Resume and application packs

- [ ] Copy `config/resume-profile.example.yaml` to `config/resume-profile.yaml`.
- [ ] Fill in real contact info, skills, experience, and bullet metadata.
- [ ] Generate a resume for a saved job with `job-scraper generate-resume --job-id <ID> --profile config/resume-profile.yaml --no-llm`.
- [ ] Prepare an application pack with `job-scraper prepare-application --job-id <ID> --profile config/resume-profile.yaml --no-llm`.
- [ ] Track state with `job-scraper list-applications` and `job-scraper update-application`.

## Outreach

- [ ] Copy `config/outreach.example.yaml` to `config/outreach.yaml`.
- [ ] Prepare `config/contacts.csv` with LinkedIn contacts.
- [ ] Initialize outreach tables: `job-scraper outreach init`.
- [ ] Import contacts: `job-scraper outreach import-contacts --csv config/contacts.csv`.
- [ ] Queue actions: `job-scraper outreach queue --config config/outreach.yaml`.
- [ ] Review due actions: `job-scraper outreach next --limit 5 --open`.
- [ ] Record outcomes with `job-scraper outreach mark` and `job-scraper outreach mark-contact`.

## Checks before relying on it

- [ ] Keep `.env`, `data/`, `.venv/`, and personal YAML/CSV files out of git.
- [ ] Run the scraper with a narrow filter first to limit API-credit usage.
- [ ] Confirm dedupe by running a second sync and checking inserted vs skipped counts.
- [ ] Back up `data/jobs.sqlite3` before large manual edits or migrations.
