# Jobs app runbook

All app commands are run from `scraper/` after activating the virtualenv.

```bash
cd scraper
source .venv/bin/activate
```

## Install or repair dependencies

```bash
python -m pip install -e '.[dev]'
job-scraper --help
```

If you prefer not to activate the shell, call the venv binary directly from the repository root:

```bash
scraper/.venv/bin/job-scraper --help
```

## Required local files

Create these once:

```bash
cp .env.example .env
cp config/filters.example.yaml config/filters.yaml
```

Minimum `.env` for live scraping:

```dotenv
THEIRSTACK_API_KEY=<your key>
JOB_SCRAPER_DB_PATH=data/jobs.sqlite3
OPENAI_API_KEY=
OPENAI_MODEL=gpt-4o-mini
APPLICATION_PACK_DIR=data/application_packs
APPLICATION_BROWSER_HEADLESS=false
APPLICATION_TIMEOUT_MS=30000
```

## Scraper commands

| Goal | Command |
| --- | --- |
| Create tables | `job-scraper init` |
| Estimate TheirStack matches | `job-scraper preview-count --filters config/filters.yaml` |
| Run one sync | `job-scraper run-once --filters config/filters.yaml` |
| Run immediately, then every 24 hours | `job-scraper daemon --filters config/filters.yaml` |
| Import supplemental public JSON jobs | `job-scraper import-public-json` |
| Start local web UI | `job-scraper webui --host 127.0.0.1 --port 8000` |

## Safe first live run

1. Make `config/filters.yaml` intentionally narrow.
2. Run `job-scraper preview-count --filters config/filters.yaml`.
3. If the result count is acceptable, run `job-scraper run-once --filters config/filters.yaml`.
4. Open the UI with `job-scraper webui --host 127.0.0.1 --port 8000`.
5. Review <http://127.0.0.1:8000>.
6. Only then start `job-scraper daemon --filters config/filters.yaml` for recurring pulls.

TheirStack charges by returned job. `preview-count` is the low-risk estimate step.
Current TheirStack plan limit observed here: keep `search.limit` at `25` or lower. A higher per-page limit can return `403` with `Premium functionality limitation`.

## Resume and application commands

Create a profile:

```bash
cp config/resume-profile.example.yaml config/resume-profile.yaml
```

Generate a deterministic resume without OpenAI:

```bash
job-scraper generate-resume \
  --job-id <THEIRSTACK_JOB_ID> \
  --profile config/resume-profile.yaml \
  --output data/resumes/<job-id>.md \
  --no-llm
```

Prepare an application pack:

```bash
job-scraper prepare-application \
  --job-id <THEIRSTACK_JOB_ID> \
  --profile config/resume-profile.yaml \
  --notes "Applied via company site" \
  --no-llm
```

Install Chromium and fill the saved job application:

```bash
python -m playwright install chromium
job-scraper apply \
  --job-id <THEIRSTACK_JOB_ID> \
  --profile config/resume-profile.yaml \
  --resume-path data/resumes/<resume>.pdf
job-scraper apply \
  --job-id <THEIRSTACK_JOB_ID> \
  --profile config/resume-profile.yaml \
  --resume-path data/resumes/<resume>.pdf \
  --submit
```

Without `--submit`, Chromium fills the form and leaves final review to you; with `--submit`, the row is marked `applied` only after a submission confirmation is detected. Generated Markdown resumes can be uploaded only if the target form accepts them; for real ATS submissions, pass a PDF/DOCX via `--resume-path` until resume rendering exists.

Track application state:

```bash
job-scraper list-applications
job-scraper list-applications --status applied
job-scraper update-application --job-id <THEIRSTACK_JOB_ID> --status applied --applied-at 2026-06-23
```

## Outreach commands

```bash
cp config/outreach.example.yaml config/outreach.yaml
job-scraper outreach init
job-scraper outreach import-contacts --csv config/contacts.csv
job-scraper outreach queue --config config/outreach.yaml
job-scraper outreach next --limit 5 --open
job-scraper outreach mark <ACTION_ID> --status sent
job-scraper outreach mark-contact --linkedin-url <PROFILE_URL> --status connected
```

Allowed outreach action statuses: `sent`, `skipped`, `replied`, `blocked`.
Allowed contact statuses: `connected`, `replied`, `skipped`, `do_not_contact`.

## Troubleshooting

- `ModuleNotFoundError: No module named 'job_scraper'`: dependencies are not installed in the active Python. Activate `.venv` and run `python -m pip install -e '.[dev]'`.
- `Filter file not found`: copy `config/filters.example.yaml` to `config/filters.yaml` or pass `--filters <path>`.
- Empty web UI: SQLite has no saved jobs yet. Run `job-scraper run-once` or `job-scraper import-public-json`.
- Live sync fails immediately: confirm `THEIRSTACK_API_KEY` is set in `scraper/.env`.
- `TheirStack returned 403` with `Premium functionality limitation`: set `search.limit: 25` or lower in `config/filters.yaml`.
- Resume generation contacts OpenAI only when `OPENAI_API_KEY` is set and `--no-llm` is not passed.
