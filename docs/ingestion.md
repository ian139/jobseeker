# Job ingestion and backlog

[Back to the project README](../README.md)

This guide covers the local ingestion surfaces and the SQLite backlog. Ingestion
only discovers, normalizes, filters, and stores job records. It does not open a
browser, claim a job, prepare an application, or submit one.

## Safety and local data

- The backlog is local SQLite. The default is `data/jobs.sqlite3`, or the path
  in `DATABASE_URL`; pass `--db PATH` to choose another database.
- TheirStack **preview** is the credit-safe operation. A full TheirStack fetch
  is paid and requires an explicit authorization on that invocation.
- A paid request is never automatically replayed. A timeout or ambiguous
  response can still have consumed credits; decide whether to run a new command
  manually.
- Raw source payloads and job descriptions are stored in the local database.
  Treat the database, sync audit records, and command output as private project
  data. Do not paste them into issues, logs, or public summaries.

Initialize the database before an ingestion run:

```bash
uv run --frozen jobs-assistant --db "$DB" init-db
```

`$DB` is an example shell variable, not a value to copy from this repository.

## TheirStack

### Source profiles

A TheirStack source profile is a search preset. It is **not** an application
profile JSON file and is unrelated to the profile used for resume generation or
application autofill.

| Profile | Search intent |
| --- | --- |
| `new_grad_cs` | Computer-science software, data, and platform roles with early-career signals. |
| `new_grad_non_coop_cs` | The same broad CS role family and early-career signals, while excluding co-op signals. |
| `fall_coop_swe_data` | Co-op software and data roles with co-op signals. |
| `default` | No profile-specific title/description additions; the shared search exclusions still apply. |

`theirstack-preview` and `theirstack-sync` default to `new_grad_cs`. Both
accept `--source-profile NAME` and the compatibility alias `--profile NAME`.

The shared search payload asks for open, direct-employer US jobs from the last
seven days, orders by posting/discovery recency, and excludes configured senior,
management, recruiting, sales, experience, clearance, and commission patterns.
The profile adds the role and early-career/co-op terms shown above.

### Credit-safe preview

`theirstack-preview` calls TheirStack with blurred company data,
`include_total_results=true`, and `limit=1`. The response is a match count only;
it does not fetch full descriptions or save jobs. Preview requests retain bounded
retries because they use the credit-safe payload.

The preview's `--ats` option is descriptive only. A blurred count has no
application URL, so `--ats greenhouse` or `--ats lever` still reports the
unfiltered total and marks the filter as not applied. Use a paid sync when an
ATS-filtered backlog is required.

```bash
THEIRSTACK_API_KEY="$THEIRSTACK_API_KEY" \
  uv run --frozen jobs-assistant --db "$DB" theirstack-preview \
  --source-profile new_grad_cs

# A pinned ATS value here describes the count; it does not filter the preview.
uv run --frozen jobs-assistant --db "$DB" theirstack-preview \
  --source-profile new_grad_cs --ats greenhouse
```

`THEIRSTACK_API_KEY` must already contain the private key. Do not put a real
key, profile facts, or returned job data in a script checked into the repo.

### Paid full sync

`theirstack-sync` fetches full job records and then writes normalized results to
the backlog. It requires either `--paid-fetch` on the command or
`THEIRSTACK_ENABLE_PAID_FETCH=true` in the environment. The command defaults to
`--source-profile new_grad_cs`, `--ats auto`, and `--limit 25`; `--limit` must
be 1–100 and is the page size requested from TheirStack. Pagination may make
additional paid requests, capped at 1,000 pages per sync and stopping earlier
when the validated result set is complete. `--limit` is therefore not a promise
that only that many total records will be billed or returned.

```bash
THEIRSTACK_API_KEY="$THEIRSTACK_API_KEY" \
  uv run --frozen jobs-assistant --db "$DB" theirstack-sync \
  --source-profile new_grad_cs --paid-fetch --limit 25 --ats auto
```

Paid pagination validates every page before returning the aggregate. If a
later page is malformed, inconsistent, unavailable, or would exceed the
1,000-page cap, no jobs from the aggregate are written. Requests completed
before that failure may already have consumed credits. Each paid page is
attempted once:
there is no automatic retry or replay after HTTP errors, timeouts, or an
ambiguous response. A manual rerun is a new credit decision, not an automatic
continuation.

Each database-backed paid sync records a redacted sync audit with the selected
profile/filter, counts, completion, and a fixed failure reason when applicable.
The audit does not contain API keys, raw responses, or job descriptions.

#### ATS filtering and checkpoints

`--ats auto` preserves the historical unfiltered ingestion behavior. It uses the
source-profile checkpoint, sends the latest successful checkpoint as a
`discovered_at` lower bound, and advances that checkpoint only after a
successful sync. `auto` may therefore retain a job whose URL is not a supported
application route; application preparation applies its own supported-ATS gate.

`--ats greenhouse` and `--ats lever` are pinned filters. For these modes:

1. TheirStack full records are fetched first.
2. Each record is normalized and checked with the canonical Greenhouse or Lever
   route validator. Hostname substring matching is not sufficient.
3. Rejected routes are discarded before one-per-company selection and SQLite
   upsert.
4. The sync does **not** advance a wall-clock checkpoint. It intentionally
   re-fetches the latest requested raw window each time, so filtering a limited
   page cannot make an eligible row permanently unreachable.

Pinned runs report `fetched`, `ats_eligible`, `ats_rejected`, `seen`, `inserted`,
and `updated` counts and set `checkpoint_advanced` to false. Repeated runs are
safe with respect to backlog identity because SQLite upsert/deduplication still
applies.

### `job-scrape` compatibility wrapper

`job-scrape` is the standalone compatibility entry point for the same paid
TheirStack sync path. It does not use a separate scraper or database contract.
Its authoritative options are:

| Option | Default / rule |
| --- | --- |
| `--db PATH` | `DATABASE_URL` or `data/jobs.sqlite3`. |
| `--source-profile NAME` / `--profile NAME` | `new_grad_cs`. |
| `--ats auto\|greenhouse\|lever` | `auto`. |
| `--count N` | `1`, range 1–100; maps to the sync page-size `limit`. |
| `--paid-fetch` | Required unless `THEIRSTACK_ENABLE_PAID_FETCH` is `true`, `1`, or `yes`. |

```bash
THEIRSTACK_API_KEY="$THEIRSTACK_API_KEY" \
  uv run --frozen job-scrape --db "$DB" \
  --profile new_grad_non_coop_cs --ats lever --count 5 --paid-fetch
```

The wrapper uses the same pinned-ATS filtering, checkpoint behavior, paid-credit
warning, no-replay rule, and SQLite deduplication as `theirstack-sync`.

## Normalized feeds and fixtures

`jobs-assistant import-feed` accepts either a local JSON file or an HTTP feed.
For HTTP, `--base-url BASE` requests `GET BASE/v1/jobs`; if `--base-url` is
omitted, `JOB_SOURCE_BASE_URL` is used. `JOB_SOURCE_API_KEY`, when set, is sent
as a bearer token. The source envelope must be one of:

- a top-level JSON list of job objects;
- an object with a `jobs` list; or
- an object with a `data` list.

Each record must be a JSON object. The normalizer accepts these common aliases:

| Normalized value | Accepted fields |
| --- | --- |
| source job ID | `id`, `external_id`, or `source_job_id` |
| title | `title` or `job_title` |
| company | `company` or `company_name` (a company object may provide `name`) |
| application/listing URL | `apply_url`, then `url`, or `listing_url` |
| description | `description`, `job_description`, `description_text`, `description_html`, or supported `details` values |
| location | `location`, `job_location`, `city`, or `country_code` |
| remote | boolean `remote` |
| posted time | `date_posted` or `posted_at` |

A record must contain a source ID or usable URL after normalization. The
`--source NAME` value is stored exactly, defaults to `job_source`, must be
non-empty after trimming, and is limited to 128 characters. It is useful for
keeping multiple feeds separate in `backlog-list`.

Example fixture shape (use your own local file; the values below are synthetic):

```json
[
  {
    "id": "fixture-job-001",
    "title": "Example Software Engineer",
    "company_name": "Example Company",
    "apply_url": "https://jobs.example.invalid/roles/fixture-job-001",
    "location": "Remote",
    "remote": true,
    "date_posted": "2026-01-15",
    "description": "Synthetic listing used for a local import check."
  }
]
```

Import a file or `/v1/jobs` feed with a source label:

```bash
uv run --frozen jobs-assistant --db "$DB" import-feed \
  --json-file "$FEED_JSON" --source example-fixture

uv run --frozen jobs-assistant --db "$DB" import-feed \
  --base-url "$FEED_BASE_URL" --source partner-feed
```

The import validates all records before upserting. Job changes and the terminal
success audit commit together; a rejected payload rolls back job changes before
a fixed, redacted failure audit is recorded. Database-backed attempts record
source, mode (`json_file` or `http`), counts, completion, and a fixed failure
reason in the sync audit table. Audit rows do not preserve raw HTTP responses,
API keys, or descriptions.

### Dry-run and audit-safe preflight

Add `--dry-run` to `import-feed` to run the same envelope, normalization, and
SQLite identity logic without modifying the configured database or recording a
sync audit. The command fetches an HTTP source when requested, and reads an
existing database into a disposable in-memory snapshot; a missing database
stays missing. Output reports `seen`, `would_insert`, `would_update`, and at
most 100 normalized preview rows. Preview rows are allow-listed metadata only;
raw payloads and descriptions are excluded.

```bash
uv run --frozen jobs-assistant --db "$DB" import-feed \
  --json-file "$FEED_JSON" --source partner-feed --dry-run
```

A dry run is a preflight, not a backup or a write-through mode. Inspect its
counts and preview before running the same command without `--dry-run`.

## Deduplication and backlog identity

TheirStack sync first filters pinned ATS routes, then (by default) chooses at
most one role per company, preferring the configured role priority and the most
recent posting. Generic feed imports do not apply that one-per-company choice.
All sources then use the SQLite upsert identity:

1. try the same `source` plus `source_job_id` when a source ID is present;
2. if that does not identify an existing row, try the canonical URL; and
3. canonicalize absolute URLs by normalizing scheme/host, removing fragments
   and common tracking parameters (`utm_*`, `gclid`, `fbclid`, `msclkid`, and
   `gh_src`), and trimming a trailing path slash.

An existing identity is updated rather than inserted. A job must have at least
one identity. The database keeps the source payload in `raw_json` for local
provenance, but inspection commands never print that field.

## Inspecting and archiving the backlog

The listing and show commands are read-only with respect to job claims and
browser/network activity. `backlog-archive` changes only queued status after
explicit confirmation; it does not open a browser or claim a job.

| Command | Behavior and defaults |
| --- | --- |
| `backlog-list` | Lists public fields only; default `--status queued`, `--limit 25`, effective `--offset 0`. Supports `queued`, `in_progress`, and `archived`; `--limit` is 1–100 and `--offset` is 0–100,000. `--source` is an exact source filter. It opens an existing database read-only and does not create a missing path. |
| `backlog-show JOB_ID` | Requires a positive ID, opens SQLite read-only, and shows one row's allow-listed fields plus a bounded plain-text description (up to 12,000 characters). It never exposes `raw_json`. |
| `backlog-archive JOB_ID... --confirm` | Requires explicit `--confirm`, at most 100 unique positive IDs, and archives queued rows without deleting them. The compare-and-set is atomic: every requested row must still exist and be queued or the whole request is rolled back. It never claims or requeues jobs. |

`backlog-list` orders rows by posted time descending, then first-seen time and
ID. Its `total` count covers all statuses in the optional source scope, while
`pending` always counts queued rows; these counts remain useful when listing an
archived or in-progress page.

```bash
uv run --frozen jobs-assistant --db "$DB" backlog-list \
  --source partner-feed --status queued --limit 25

uv run --frozen jobs-assistant --db "$DB" backlog-show 123

# Review the IDs first; this changes only the queued status of these rows.
uv run --frozen jobs-assistant --db "$DB" backlog-archive --confirm 123 124
```

Use `backlog-show` or a bounded list to review a job before any later workflow.
Archiving is an explicit bookkeeping decision; it does not erase the raw
record or submit anything.

[Back to the project README](../README.md) · [Application drafts](application-drafts.md) · [Resume generation guide](resume-generation.md)
