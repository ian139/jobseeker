# TheirStack scraper notes

## Setup

Run these commands from `scraper/` unless noted.

1. Create or copy a TheirStack API key from TheirStack settings.
2. Copy `.env.example` to `.env` and set `THEIRSTACK_API_KEY`.
3. Copy `config/filters.example.yaml` to `config/filters.yaml`; keep personal filter edits in this untracked local file.
4. Install dependencies: `python -m pip install -e '.[dev]'`. If your Python is externally managed, create a venv first with `python -m venv .venv` and use `.venv/bin/python -m pip install -e '.[dev]'`.
5. Initialize SQLite: `job-scraper init`.
6. Estimate matches without saving jobs: `job-scraper preview-count --filters config/filters.yaml`.
7. Run one pull: `job-scraper run-once --filters config/filters.yaml`.

## Operation

Use the local 24-hour puller:

```bash
job-scraper daemon --filters config/filters.yaml
```

The daemon runs one sync immediately, then schedules the same sync every 24 hours. It uses TheirStack as the third-party job scraper source and SQLite at `data/jobs.sqlite3` for dedupe and checkpoints.

## BotDog outreach

BotDog outreach is queue/manual-send only: it prints or opens LinkedIn profiles and records outcomes; it does not automate LinkedIn browser clicks.

```bash
job-scraper outreach init
job-scraper outreach import-contacts --csv config/contacts.csv
job-scraper outreach queue --config config/outreach.yaml
job-scraper outreach next --limit 5 --open
job-scraper outreach mark ACTION_ID --status sent
job-scraper outreach mark-contact --linkedin-url https://www.linkedin.com/in/example --status connected
```

## Credits and checkpointing

TheirStack charges 1 API credit per returned job. `preview-count` uses `blur_company_data: true`, `include_total_results: true`, and `limit: 1` so you can estimate match volume before saving jobs. Repeated pulls store `last_successful_discovered_at` in SQLite and send `discovered_at_gte` with a 10-minute overlap, so already-seen jobs are deduped locally and old result windows are avoided.

## Source-domain filters

The default `url_domain_or` values target LinkedIn plus common ATS families:

- `linkedin.com`
- `myworkdayjobs.com`
- `rippling.com`
- `oraclecloud.com`
- `taleo.net`
- `greenhouse.io`
- `ashbyhq.com`
- `smartrecruiters.com`

Edit `config/filters.yaml` to add or remove source domains. Leave `url_domain_or: []` to omit source filtering and search all TheirStack sources.

## Production note

TheirStack recommends webhooks for production real-time syncing. This project intentionally starts with the requested scheduled 24-hour puller; switch to webhooks later only if real-time updates become required.
