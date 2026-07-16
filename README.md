# jobs-assistant

Local job ingestion and guarded Greenhouse+Lever application-draft assistant.

The application imports and deduplicates listings into a local SQLite backlog. The guarded autofill workflow is active for Greenhouse and Lever: it claims queued jobs, fills only safe supported fields, records private evidence, and leaves review/submission to a person. It never clicks or automates a final-submit control.

## Active workflow

1. Discover listings with TheirStack, a JSON fixture, or a normalized `/v1/jobs` feed.
2. Normalize, filter, quality-gate, and deduplicate into the SQLite backlog.
3. Run `autofill` against queued jobs. Each run atomically claims one job, selects the validated Greenhouse or Lever adapter, resolves explicit profile/resume context plus the job description, and performs at most one safe action per observation iteration. An explicit reviewed retry may requeue the job for a new run.
4. Inspect handoffs with `autofill-review list`. A headed handoff is a draft for a human to review; the human decides whether to submit, skip, or retry.
5. Record that human decision with `autofill-review complete` or queue an explicit `autofill-review retry`.

The hard boundary is permanent: the CLI does not submit applications. `--outcome submitted` records a submission the human already made; it does not perform one.

## Supported ATS scope

`autofill --ats auto` (the default) selects the adapter whose exact route matches the queued URL. `--ats greenhouse` and `--ats lever` pin the route policy; a mismatch fails closed. Both adapters use the same public-HTTPS, network, safe-action, private-artifact, and no-final-submit gates.

Every browser mutation or action MUST pass a deterministic allow/deny gate against the current observed page/frame snapshot. LLM output MUST be schema- and safety-validated before it can influence an action; raw model output never drives browser mutations. Sensitive, legal, protected-class, financial, authentication, CAPTCHA, and assessment questions MUST never be inferred or automated and always stop for manual handling. Inference is limited to safe non-sensitive noncanonical fields with an explicit deterministic source of truth.

Greenhouse initial routes are limited to:

- Hosted job pages: `boards.greenhouse.io/<company>/jobs/<positive-id>` or `job-boards.greenhouse.io/<company>/jobs/<positive-id>`.
- Hosted embed pages: `boards.greenhouse.io/embed/job_app?for=<company>&token=<positive-id>` (optional `gh_src` attribution is ignored for identity).
- Greenhouse short links: `grnh.se/<slug>`.

Direct Lever initial routes are limited to `https://jobs.lever.co/<company>/<canonical-lowercase-UUID>` and `https://jobs.eu.lever.co/<company>/<canonical-lowercase-UUID>`, with an optional `/apply` suffix. The company segment is an ASCII slug; the job segment must be a canonical lowercase UUID. Lever routes reject query strings (including `?`), fragments, credentials, non-HTTPS ports, percent-encoded or backslash paths, non-canonical UUID casing, final-like path words, redirects to another identity/host, and all other hosts or paths.

Both route guards reject private/local destinations, malformed or duplicate query parameters, credentials/fragments, and final-like routes or query values (`submit`, `complete`, `confirm`, `finish`, `send`, `final`). Authentication, CAPTCHA, assessment, unsupported frames, validation failures, and unresolved required/sensitive fields stop for manual handling; they are not bypassed.

## Quick start

```bash
uv run --frozen jobs-assistant init-db
THEIRSTACK_API_KEY=... uv run --frozen jobs-assistant theirstack-preview
THEIRSTACK_API_KEY=... uv run --frozen jobs-assistant theirstack-sync --paid-fetch --limit 25
THEIRSTACK_API_KEY=... THEIRSTACK_ENABLE_PAID_FETCH=true uv run --frozen job-scrape --profile new_grad_cs --count 1
```

The preview is credit-safe and reports the unfiltered total for the selected
TheirStack source profile. It has no application URLs, so `--ats greenhouse`
or `--ats lever` does not filter that count. Pinned ATS filtering happens only
after a paid fetch returns full job data and before any job is upserted. The
`auto` sync mode preserves the historical unfiltered ingestion behavior.

Paid sync requests are one-shot: a timeout, `429`, or `5xx` fails closed and
is not replayed automatically. If the outcome is ambiguous, inspect the
failed run and explicitly authorize a new sync. Credit-safe previews retain
their bounded retry behavior.

Pinned syncs scan the latest paid raw window on every invocation; they do not
send or advance a `discovered_at_gte` checkpoint. Their JSON result reports
`"checkpoint_advanced": false`. Because ATS filtering happens after the
limited raw fetch, a pinned run may return fewer eligible jobs than the
requested raw `--limit`. Repeating the same command refetches that window and
deduplicates against the backlog. Every authorized rerun may consume credits
for up to that run's explicit `--limit`; the CLI never paginates or fetches
beyond that limit automatically. Increase `--limit` explicitly (up to the
100-job cap) when a wider raw window is desired.

For a guaranteed Greenhouse-only run when an older database may contain mixed
queued rows, use a new owner-private database and artifact root. This leaves
the old database untouched and avoids claiming its pre-existing rows:

```bash
umask 077
RUN_ROOT="$(mktemp -d "${TMPDIR:-/tmp}/jobs-assistant-greenhouse.XXXXXX")"
chmod 700 "$RUN_ROOT"
DB="$RUN_ROOT/jobs.sqlite3"
ARTIFACT_ROOT="$RUN_ROOT/artifacts"
mkdir "$ARTIFACT_ROOT"
chmod 700 "$ARTIFACT_ROOT"

uv run --frozen jobs-assistant --db "$DB" init-db
THEIRSTACK_API_KEY=... uv run --frozen jobs-assistant --db "$DB" \
  theirstack-preview --source-profile new_grad_cs --ats greenhouse
THEIRSTACK_API_KEY=... uv run --frozen jobs-assistant --db "$DB" \
  theirstack-sync --source-profile new_grad_cs --ats greenhouse \
  --paid-fetch --limit 25
uv run --frozen jobs-assistant --db "$DB" autofill \
  --ats greenhouse --headed --artifact-root "$ARTIFACT_ROOT" \
  --resume-file path/to/configured-resume.pdf \
  --application-profile-json path/to/application-profile.json
uv run --frozen jobs-assistant --db "$DB" autofill-review \
  --artifact-root "$ARTIFACT_ROOT" list --limit 10
```

Keep the new root for review and evidence; this runbook does not require
destructive cleanup or sanitization. An existing mixed database is not
sanitized by an ingestion filter. In particular, `autofill --ats` is a
browser route policy for already queued jobs, not an ingestion-selection
filter. Pinned Greenhouse and Lever syncs scan the latest requested paid
window each time; `auto` continues using the legacy source-profile
checkpoint.

Default database:

```text
data/jobs.sqlite3
```

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
| `backlog-list` | List backlog jobs without claiming or mutating them; opens an existing SQLite database read-only and fails with `database_error` without creating a missing path |
| `backlog-archive` | Archive explicitly named queued job IDs without deleting rows; requires `--confirm` and never claims or requeues jobs |
| `theirstack-preview` | Preview the unfiltered TheirStack total for a source profile without saving jobs; an ATS flag is descriptive only |
| `theirstack-sync` | Paid-fetch full jobs, then filter pinned Greenhouse/Lever routes before upsert; requires `--paid-fetch` or `THEIRSTACK_ENABLE_PAID_FETCH=true` |
| `job-scrape` | Compatibility wrapper for `theirstack-sync`; `--count` maps to TheirStack paid-fetch `limit`, and SQLite dedupe still removes duplicates by source job ID and canonical URL |
| `import-feed` | Import normalized jobs from a JSON fixture or `GET /v1/jobs` feed |
| `autofill` | Claim up to ten queued Greenhouse or Lever jobs, fill safe non-final fields, persist evidence, and optionally release a guarded headed window |
| `autofill-review list` | List the latest unreviewed application runs |
| `autofill-review complete` | Record the human's `submitted` or `skipped` decision |
| `autofill-review retry` | Queue an explicit retry for a reviewed run |

`backlog-list` accepts `--status {queued,in_progress,archived}` and `--limit 1-100`. It never initializes or writes the database, so a missing `--db` path remains absent.

`backlog-archive JOB_ID... --confirm` accepts 1-100 positive, unique IDs. The command uses an all-or-nothing queued-only compare-and-set: missing, `in_progress`, or already archived IDs reject the entire request without changing any job row. URL-less queued rows are eligible. Its JSON output contains only the sorted archived IDs and count; `backlog-list` remains read-only and reports the updated pending count.

## Autofill flags and inputs

`autofill` defaults are stable and can be overridden as follows:

| Flag | Default | Meaning |
|---|---|---|
| `--limit` | `1` (range `1-10`) | Maximum queued jobs to process |
| `--resume-file` | `resume/Main_Resume.pdf` | One owned regular PDF, TXT, or MD file; it is opened once, hashed, and staged privately for each run |
| `--application-profile-json` (`--profile-json`) | unset | Optional explicit application facts and ATS-scoped `field_answers`; values are not guessed from a resume for opaque or sensitive questions |
| `--application-profile-preset` | unset | Select one named v1 preset from `--application-profile-dir`; mutually exclusive with profile JSON |
| `--application-profile-dir` | unset | Private directory containing `<name>.json` v1 application-profile presets; required with `--application-profile-preset` |
| `--applicant-description-file` | unset | Optional UTF-8 applicant/job-context description for the guarded resolver |
| `--artifact-root` | `data/application-runs` | Private per-run evidence and review-manifest root |
| `--ats` | `auto` (`auto\|greenhouse\|lever`) | Route policy; `auto` selects the adapter matching the exact queued URL |
| `--application-preferences` | unset | Private, validated v1 preferences JSON; exact mappings/opt-outs/order only |
| `--headed` | off | Release an independently owned review window after evidence and handoff state are durable; no-final-submit remains enforced |


### Application-profile presets (v1)

Presets are named application profiles, not TheirStack source profiles. A preset directory contains a file named `<name>.json`; names are portable ASCII letters/digits/`_`/`-` (maximum 64 characters). The exact v1 document shape is:

```json
{
  "schema_version": 1,
  "name": "default",
  "profile": {
    "first_name": "Ada",
    "last_name": "Lovelace",
    "email": "ada@example.test",
    "resume_summary": "Short resolver context only.",
    "field_answers": [
      {"ats": "*", "kind": "email", "name": "email", "value": "ada@example.test"}
    ]
  }
}
`profile` holds explicit facts plus optional `resume_summary` and `field_answers`. Each answer requires `ats` (`greenhouse`, `lever`, or `*`), a safe non-file `kind` (`text`, `email`, `tel`, `url`, `number`, `date`, `textarea`, `select`, `checkbox`, or `radio`), `name` or `label`, and a validated string (or boolean for checkbox/radio). Unknown keys, duplicate matchers, non-finite/deep/oversized JSON, and sensitive/opaque answers are rejected. Explicit profile JSON and preset JSON are each read, parsed, and hashed from one exact byte snapshot; the private `run.json` records only source kind and SHA-256 provenance (preset name/schema metadata is also retained), never profile values.


Select a preset with both flags (they are mutually exclusive with `--application-profile-json`):

```bash
uv run --frozen jobs-assistant autofill \
  --application-profile-dir profiles \
  --application-profile-preset default \
  --ats auto
```

### Application preferences (v1)

`--application-preferences PATH` loads a separate, private v1 JSON document with exactly these top-level keys:

```json
{
  "schema_version": 1,
  "mappings": [
    {"ats": "lever", "kind": "email", "name": "email", "label": null, "value": "ada@example.test"}
  ],
  "opt_outs": [
    {"ats": "*", "kind": "textarea", "name": "cover_letter", "label": null}
  ],
  "review_order": [
    {"ats": "*", "kind": "email", "name": "email", "label": null}
  ]
}
```

Every matcher is exact on `kind` and `name` or `label`; `ats` is `greenhouse`, `lever`, or the safe wildcard `*`. Mappings contain one scalar `value`; checkbox/radio values are booleans. The loader rejects unknown keys, duplicates/conflicts, traversal/symlinks, non-owned files, and oversized/deep JSON. Sensitive, final, file-upload, password, hidden, and opaque fields/matchers/values are never preference targets. A matching opt-out wins and leaves a required field manual (or skips an optional field). For a field not opted out, deterministic profile/resume answers take precedence over a preference mapping; mappings only fill an otherwise unanswered safe field, and `review_order` stable-sorts existing actions without creating actions. Resolver description precedence is `--applicant-description-file`, then profile `resume_summary`, then job description; description is never uploaded.

Use the atomic editor (each write is mode `0600`, owner-only, and fsynced before replacement):

```bash
uv run --frozen jobs-assistant application-preferences init preferences.json
uv run --frozen jobs-assistant application-preferences show preferences.json
uv run --frozen jobs-assistant application-preferences set-mapping preferences.json \
  --ats lever --kind email --name email --value ada@example.test
uv run --frozen jobs-assistant application-preferences remove-mapping preferences.json \
  --ats lever --kind email --name email
uv run --frozen jobs-assistant application-preferences set-opt-out preferences.json \
  --ats '*' --kind textarea --name cover_letter
uv run --frozen jobs-assistant application-preferences remove-opt-out preferences.json \
  --ats '*' --kind textarea --name cover_letter
uv run --frozen jobs-assistant application-preferences set-review-order preferences.json \
  --ats '*' --kind email --name email
uv run --frozen jobs-assistant application-preferences remove-review-order preferences.json \
  --ats '*' --kind email --name email
```

`show` reports the schema, document SHA-256, matcher metadata, and value length/hash; it never prints mapping values. `init` refuses to overwrite. `set-*` replaces an identical matcher, while `remove-*` deletes it (removing an absent review-order matcher is an error). The exact preference bytes are hashed on load and the SHA-256 is recorded privately in the run manifest.

The source-search profile remains a different concept: `theirstack-*` and `job-scrape` use `--source-profile` (alias `--profile`, default `new_grad_cs`) to choose search filters. Never pass a source profile where an application profile or preset is expected.

The resume is source material and an upload, not an application-answer database. Explicit facts and per-field answers come from either `--application-profile-json` or the named preset, never from a source-search profile. Recognized non-sensitive contact fields may be filled only when profile and resume facts agree; ambiguous/conflicting facts and all sensitive/opaque fields stay manual. The description is resolver context only: an explicit `--applicant-description-file` wins, otherwise `resume_summary` from the selected profile is used, and otherwise the job description is used. It is never uploaded or treated as a field answer.

Example:

```bash
npm install
npm run install-browser
uv run --frozen jobs-assistant --db /tmp/jobs.sqlite3 autofill \
  --limit 1 \
  --resume-file path/to/configured-resume.pdf \
  --application-profile-json path/to/application-profile.json \
  --applicant-description-file path/to/applicant-description.txt \
  --artifact-root /tmp/jobs-assistant-artifacts \
  --ats auto \
  --headed
```

Omit `--headed` for headless processing. `--headed` is host-only: after the durable handoff is committed, the CLI releases the browser owner and returns while the review window remains independently alive. Closing the browser tab/window is the user-controlled end of that handoff; it is not a timer and the parent does not reconnect through CDP. Close the tab before completing or retrying a headed run, then pass `--confirm-window-closed` to prove that cleanup is complete.

## Review commands

All review commands accept the global `--db PATH` before `autofill-review` and `--artifact-root PATH` after it. The artifact-root default is `data/application-runs`.

```bash
uv run --frozen jobs-assistant --db /tmp/jobs.sqlite3 autofill-review \
  --artifact-root /tmp/jobs-assistant-artifacts list --limit 10

uv run --frozen jobs-assistant --db /tmp/jobs.sqlite3 autofill-review \
  --artifact-root /tmp/jobs-assistant-artifacts complete \
  --run-id 42 --outcome submitted --confirm-window-closed \
  --annotation-file notes/run-42.txt

uv run --frozen jobs-assistant --db /tmp/jobs.sqlite3 autofill-review \
  --artifact-root /tmp/jobs-assistant-artifacts retry \
  --run-id 42 --confirm-window-closed \
  --annotation-file notes/retry-42.txt
```

`complete` requires `--run-id` and `--outcome {submitted,skipped}`. `retry` requires `--run-id`; both accept optional `--annotation-file` and `--confirm-window-closed`. A retry is explicit and latest-run guarded; it returns the job to the queue rather than silently rerunning a stale run.

## Private artifacts and evidence

Each claimed run has a mode-`0700` private directory under the artifact root:

```text
data/application-runs/
  run-42/
    run.json
    claim.json
    input/<resume-basename>
    review_session.json
    observation.json
    plan.json
    actions.json
    filled_state.json
    iterations/0001/{action,observation,plan,checkpoint}.json
    screenshots/                 # adapter captures, when requested
    annotations/                 # human review notes, when supplied
```

Legacy database migrations use `legacy-run-<id>` and keep the same private artifact rules. Every published artifact is immediately read back and SHA-256 verified; `run.json` records paths, hashes, iteration/stage, and (for a headed handoff) only the SHA-256 of the commit token. Claim snapshots, staged input, observations, plans, actions, and screenshots never go to public CLI output. CLI results expose only sanitized fields and opaque `run-<id>`/`legacy-run-<id>` references.

The browser adapter supports private screenshot slots `initial`, `after-reveal`, `blocker`, and `final`; captures carry a `screenshot:<sha256>` reference, are deduplicated by hash, and are capped at ten captures/20 MiB each/50 MiB total. Review annotations are UTF-8 text copied into the run, hashed, and indexed in `run.json` (maximum 48,000 bytes and 12,000 characters per note; ten notes/120,000 characters per run).

Input provenance is private and byte-exact: resume snapshots and published artifacts are read back and SHA-256 verified; explicit profile JSON, profile presets, and preferences hash the exact bytes parsed/read (not a reserialized object), and `run.json` records only source kind plus digest (and preset name/schema metadata). Values, claim snapshots, observations, plans, actions, and screenshots stay inside mode-`0700` run directories and are not printed in public output.

## Browser troubleshooting

A failed autofill keeps the public result `failed/browser_error`. This public
reason is intentionally coarse: it does not identify whether startup,
navigation, observation, mutation, protocol, or cleanup failed. For private
diagnostics, inspect only
`<artifact-root>/run-<id>/browser_failure.json` (and, when present, the
separate `browser_cleanup_failure.json`) inside the mode-`0700` run directory.

`browser_failure.json` contains this bounded, safe schema:

```json
{
  "version": 1,
  "stage": "startup|navigation|observation|mutation|handoff",
  "operation": "<safe operation name>",
  "code": "<allowlisted diagnostic code>",
  "iteration": 1,
  "ats_policy": "greenhouse|lever",
  "no_final_submit": true,
  "protocol": "length-prefixed-json-v1"
}
```

The fields are private diagnostic metadata only: `stage`, `operation`, and
`code` identify a bounded browser phase and error. `iteration` is `0` for
startup/navigation and starts at `1` for normal workflow operations; cleanup
uses the latest iteration. `ats_policy` records the selected adapter policy
(`greenhouse` or `lever`), never the CLI's `auto` selector.
Cleanup evidence uses the same safe fields with `stage: "cleanup"` and
`operation: "close"` in `browser_cleanup_failure.json`. These artifacts never
contain a URL, filesystem path, process identity, exception text, stderr,
secret, applicant value, or job description.

All of the following are local, no-credit checks; they make no TheirStack or
live ATS request:

```bash
uv run --frozen --extra dev python -m pytest \
  tests/test_application_workflow.py \
  tests/test_application_claims.py \
  tests/test_application_contracts.py \
  tests/test_cli_smoke.py
node --check src/jobs_assistant/puppeteer_runner.js
node src/jobs_assistant/puppeteer_runner.js --error-code-self-test
npm run puppeteer-smoke --silent
RUN_PUPPETEER_INTEGRATION=1 uv run --frozen --extra dev python -m pytest \
  tests/test_puppeteer_adapter.py
RUN_PUPPETEER_HEADED_SMOKE=1 uv run --frozen --extra dev python -m pytest -s \
  tests/test_puppeteer_adapter.py::test_headed_local_fixture_diagnostic_closes_cleanly
```

The headed diagnostic requires a local desktop display and performs no click,
handoff, or submission. A paid sync is never replayed after failure. A browser
retry never authorizes another paid request; any new paid sync requires its own
explicit authorization.

Retained diagnostic reports may include only the opaque run path,
`<artifact-root>/run-<id>/`; never copy or expose the applicant contents of
that directory. The supplied retained runs establish no final submission and
no observation, but do not establish a specific Chromium or live-site cause.
Do not infer a root cause from `browser_error`, a bounded private code, or the
absence of an observation.

SQLite paths are fail-closed for privacy. The database is created mode `0600`, its parent directory must be owned by the invoking user and have no group/world permissions, and any `-wal`, `-shm`, or `-journal` sidecar must be an owner-private regular file. `:memory:` is the only non-file mode. Do not place `data/jobs.sqlite3` or its sidecars on a shared/group-writable path.

## Host and container execution

Run headed review only on the host with a working desktop display. The container profile is headless-only and runs the image's non-root `app` user. Compose maps that user to the invoking host identity:

```bash
export HOST_UID="$(id -u)"
export HOST_GID="$(id -g)"
mkdir -p data resume
chmod 0700 data
find data -type f -exec chmod 0600 {} +
# If an earlier container created data as root, repair ownership once:
sudo chown -R "$HOST_UID:$HOST_GID" data
HOST_UID="$HOST_UID" HOST_GID="$HOST_GID" docker compose build
HOST_UID="$HOST_UID" HOST_GID="$HOST_GID" docker compose up -d
HOST_UID="$HOST_UID" HOST_GID="$HOST_GID" docker compose ps
HOST_UID="$HOST_UID" HOST_GID="$HOST_GID" docker compose down
```

Compose performs `${VAR:-default}` interpolation from the invoking shell and (when present) the project `.env`; `.env.example` is documentation only and is never injected. Supported interpolated values include `DATABASE_URL`, `JOB_SOURCE_BASE_URL`, `JOB_SOURCE_API_KEY`, `THEIRSTACK_API_KEY`, `THEIRSTACK_ENABLE_PAID_FETCH`, `THEIRSTACK_BASE_URL`, `OLLAMA_CLOUD_API_KEY`, `OLLAMA_CLOUD_BASE_URL`, `OLLAMA_CLOUD_MODEL`, `OLLAMA_CLOUD_THINK`, and `HOST_UID`/`HOST_GID`. Set credentials in the shell or a private `.env`, inspect the resolved configuration with `docker compose config`, and never commit secrets.

Compose bind-mounts existing `./data` read/write at `/app/data` and existing `./resume` read-only at `/app/resume`; it does not copy or invent application data. `/home/app` is a mode-`0700` UID/GID-matched tmpfs. The image uses Debian `chromium-headless-shell`; compose sets `JOBS_ASSISTANT_CONTAINER_NO_SANDBOX=1` for this local Docker smoke path. Do not use `--headed` in the container.

The deterministic container check (including UID/GID, bind mounts, non-root execution, packaged policy, and headless Chromium) is:

```bash
sh scripts/container-smoke.sh
```

## Help and verification

Install the browser-adapter dependencies before any local browser run:

```bash
npm install
npm run install-browser
```

Exact CLI help checks:

```bash
uv run --frozen jobs-assistant --help
uv run --frozen jobs-assistant init-db --help
uv run --frozen jobs-assistant backlog-list --help
uv run --frozen jobs-assistant import-feed --help
uv run --frozen jobs-assistant theirstack-preview --help
uv run --frozen jobs-assistant theirstack-sync --help
uv run --frozen jobs-assistant autofill --help
uv run --frozen jobs-assistant application-preferences --help
uv run --frozen jobs-assistant application-preferences init --help
uv run --frozen jobs-assistant application-preferences show --help
uv run --frozen jobs-assistant application-preferences set-mapping --help
uv run --frozen jobs-assistant application-preferences remove-mapping --help
uv run --frozen jobs-assistant application-preferences set-opt-out --help
uv run --frozen jobs-assistant application-preferences remove-opt-out --help
uv run --frozen jobs-assistant application-preferences set-review-order --help
uv run --frozen jobs-assistant application-preferences remove-review-order --help
uv run --frozen jobs-assistant autofill-review --help
uv run --frozen jobs-assistant autofill-review list --help
uv run --frozen jobs-assistant autofill-review complete --help
uv run --frozen jobs-assistant autofill-review retry --help
uv run --frozen job-scrape --help
```

Focused smoke checks:

```bash
npm run puppeteer-smoke
uv run --frozen --extra dev python -m pytest tests/test_cli_smoke.py
sh scripts/container-smoke.sh
```

For the complete Python gate, run `uv run --frozen --extra dev python -m pytest`; headed survival is a manual host check requiring a physical benign click/tab close. The review workflow has no final-submit automation, no user-facing timer, and no CDP attach/reconnect path.

Coding-agent practices and project policy live in `AGENTS.md`. Historical snapshots live under `archive/`.
