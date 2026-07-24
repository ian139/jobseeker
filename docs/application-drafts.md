# Application drafts and guarded autofill

[Back to the project README](../README.md) · [Application RPC](application-rpc.md)

This document covers the local workflow that prepares one Greenhouse or Lever
application draft. It does not submit an application. Use the RPC guide when a
long-lived OMP coordinator is required.

## Supported initial routes

`autofill --ats auto` (the default) selects the adapter whose exact route
matches the queued job URL. `--ats greenhouse` and `--ats lever` pin the route
policy; a mismatch fails closed. The initial automated request is an HTTPS
`GET` on one of these routes:

### Greenhouse

- Hosted job page: `https://boards.greenhouse.io/<board>/jobs/<job-id>` or
  `https://job-boards.greenhouse.io/<board>/jobs/<job-id>`, where `<board>` is
  an ASCII slug (`A-Z`, `a-z`, `0-9`, `_`, or `-`).
- Hosted embed: `https://boards.greenhouse.io/embed/job_app?for=<board>&token=<job-id>`.
  `gh_src` is the only optional attribution query key and is ignored for job
  identity.

`<job-id>` is a decimal integer from `1` through `9007199254740991`; leading
zeros are accepted. Zero, negative, non-decimal, or larger values are denied.

- Short link: `https://grnh.se/<slug>`, where `<slug>` is the same ASCII slug
  shape and no query key is accepted. It is only an approved initial
  short-link route; redirects are still checked by the route and network gates.

Only the documented query keys are accepted. Duplicate or malformed query
parameters, userinfo, fragments, non-443 ports, private/local destinations,
path escapes, and final-like path or query values are denied. Static requests
are limited to the approved Greenhouse asset hosts and `/assets/` paths.

### Lever

- Hosted job page: `https://jobs.lever.co/<account>/<uuid>` or
  `https://jobs.eu.lever.co/<account>/<uuid>`.
- Apply page: either host with the same path plus `/apply`.

`<account>` is an ASCII slug. `<uuid>` must be a canonical lowercase UUID
(`8-4-4-4-12` hexadecimal groups). Lever initial routes reject query strings
(including a bare `?`), fragments, credentials, non-443 ports,
percent-encoded or backslash paths, non-canonical UUID casing, path escapes,
final-like route values, redirects to another identity or host, and all other
hosts and paths. Static requests are limited to `/assets/` on the two Lever
hosts.

Both adapters reject unsupported frames, unapproved cross-origin or unsafe
network requests, authentication pages, CAPTCHA, assessment pages, and page
validation failures. A route decision never grants permission to submit.

## Gates and workflow boundary

The workflow is deliberately staged:

1. Claim one queued job from the local SQLite backlog and bind the run to the
   validated ATS route.
2. Observe the current page and persist an immutable observation. Every browser
   mutation must pass a deterministic allow/deny gate against that observation,
   its ATS policy, the target frame, and the current field/control state.
3. Resolve deterministic profile, resume, and preference answers. At most one
   safe action is dispatched for an observation iteration; a new observation is
   required before another action.
4. Optionally ask the guarded resolver about safe unresolved fields. Its output
   is bounded, strict-schema JSON and is revalidated for field identity, value
   type, sensitivity, current observation, and privacy before it can influence
   an action. Raw model output never drives a browser mutation.
5. Persist evidence and commit a review handoff. The run stops at the
   final-submit boundary.

Automated actions are limited to validated text/email/tel/url/number/date/
textarea fields, select options, checkbox/radio controls, the one configured
resume upload, and an allow-listed non-final progress control. Existing,
readonly, hidden, disabled, stale, ambiguous, or identity-colliding targets
are not action targets.

Sensitive, legal, protected-class, financial, authentication, password,
CAPTCHA, assessment, opaque, and final-like fields are always manual. A file
field is only eligible for the configured resume upload and is never an LLM
answer target. Unresolved required fields, conflicting facts, unsupported
frames, validation errors, and blockers stop for manual handling; they are not
bypassed.

The invariant is permanent: no CLI, resolver, browser adapter, OMP tool, or
review helper clicks or automates a final-submit control. A final-like control
is a terminal stop. `--outcome submitted` only records that a human already
submitted outside the tool.

## Setup and autofill flags

Install the local browser dependencies before a live or headed run:

```bash
npm install
npm run install-browser
```

Initialize a private SQLite backlog once:

```bash
uv run --frozen jobs-assistant --db "$DB_PATH" init-db
```

`DB_PATH` and the artifact root should point to owner-private locations. Keep
configuration values in a private environment file when appropriate; see
[`.env.example`](../.env.example). Optional guarded inference uses the
configured OLLAMA key; without a key, deterministic answers still run and
unresolved safe fields remain manual.

A guarded run can be started with explicit, owner-controlled inputs:

```bash
uv run --frozen jobs-assistant --db "$DB_PATH" autofill \
  --limit 1 \
  --ats auto \
  --resume-file "$RESUME_FILE" \
  --application-profile-json "$PROFILE_JSON" \
  --application-preferences "$PREFERENCES_JSON" \
  --applicant-description-file "$DESCRIPTION_FILE" \
  --artifact-root "$ARTIFACT_ROOT" \
  --headed
```

Use either `--application-profile-json` (alias `--profile-json`) or the named
preset pair, never both. The relevant flags are:

| Flag | Contract |
|---|---|
| `--db PATH` | Global SQLite path; put it before `autofill`. The default is the local project database. |
| `--limit 1-10` | Maximum queued jobs to claim; default `1`. `--generated-resume-id` requires `--limit 1`. |
| `--ats auto\|greenhouse\|lever` | Route policy; `auto` selects the exact validated queued URL. |
| `--resume-file PATH` | One owned regular PDF, TXT, or MD resume. It is read once, hashed, and staged privately for the run. It cannot be combined with `--generated-resume-id`. |
| `--generated-resume-id ID` | Pin one existing ready generated-resume artifact; use `--generated-resume-artifact-root PATH` to select its private artifact root. |
| `--application-profile-json PATH` / `--profile-json PATH` | Explicit application facts and optional safe field answers. Values are never guessed from resume text. |
| `--application-profile-preset NAME` | Select `<NAME>.json` from `--application-profile-dir`; mutually exclusive with profile JSON. |
| `--application-profile-dir PATH` | Owner-private directory for the selected named preset; required with `--application-profile-preset` and rejected by itself. |
| `--application-preferences PATH` | Separate validated v1 mappings, opt-outs, and review order. |
| `--applicant-description-file PATH` | UTF-8 resolver context only; it is never uploaded or used as an answer. |
| `--artifact-root PATH` | Private per-run evidence and review-manifest root. |
| `--headed` | After durable handoff, leave an independently owned review window open. No-submit remains enforced. Omit it for headless processing. |

The generated-resume options refer to an already-created ready artifact; they
do not generate a resume during autofill. See [Resume generation](resume-generation.md)
for the separate generation workflows.

## Input sources and safe precedence

These inputs are intentionally separate:

- A TheirStack **source profile** is a search/filter preset used by
  `theirstack-*` and `job-scrape` through `--source-profile` (alias `--profile`).
  It is not applicant identity and must never be passed as an application
  profile or preset.
- An **application profile** is the explicit fact and field-answer document
  supplied by `--application-profile-json` or a named v1 preset. It is the
  source of applicant facts for deterministic resolution.
- The configured **resume** is source material and the only upload input. Its
  recognized contact facts may be used only when they are unambiguous and
  agree with explicit profile facts when both are present. Conflicting or
  ambiguous facts remain manual; the resume is not an answer database.
- The applicant description is resolver context only. An explicit
  `--applicant-description-file` wins; otherwise `resume_summary` from the
  selected application profile is used; if neither exists, no applicant
  description is supplied. The queued listing description remains separate
  job context and is never treated as an applicant answer.

For each observed field, precedence is:

1. A matching preference opt-out wins. A required opted-out field is manual; an
   optional one is skipped.
2. A valid ATS-specific `field_answers` entry wins over a wildcard (`"*"`)
   entry. Conflicting or ambiguous configured matchers are refused.
3. Recognized canonical facts are filled only from explicit profile facts and
   unambiguous resume facts. If both provide a value, normalized values must
   agree; otherwise the field is manual.
4. A preference mapping may fill an otherwise unanswered safe field. An
   ATS-specific mapping wins over a wildcard; conflicting mappings are refused.
5. `review_order` only stable-sorts already-authorized actions. It never creates
   an answer or authorizes a new target.

Every configured value still passes the observed field's complete ATS value
validator. Preferences cannot target sensitive, final, file-upload, password,
hidden, or opaque fields, and they cannot override the deterministic browser
gate.

## Application-profile preset v1

A preset is named application data, not a source-search profile. Store it as
`<name>.json` in a private preset directory. The filename stem and `name` must
match and must contain 1–64 portable ASCII characters matching
`[A-Za-z0-9][A-Za-z0-9_-]*`.

Every path component must be free of symlinks, and the final preset directory
must be owned by the invoking user. Directory mode bits are not enforced, so
set the directory to `0700` before use. The preset JSON must be an owned regular
file that is not a symlink or group/world-writable; use mode `0600` for private
profile data. Traversal and oversized or overly deep documents are rejected
before a run can claim a job.

The preset document has exactly these top-level keys:

```json
{
  "schema_version": 1,
  "name": "<preset-name>",
  "profile": {
    "first_name": "<first-name>",
    "last_name": "<last-name>",
    "email": "<safe-email>",
    "resume_summary": "<resolver-only applicant context>",
    "field_answers": [
      {
        "ats": "*",
        "kind": "email",
        "name": "email",
        "value": "<safe-email>"
      }
    ]
  }
}
```

`profile` contains explicit facts plus optional `resume_summary` (at most
12,000 characters) and `field_answers`. A direct `--application-profile-json`
file contains the `profile` object itself, without the preset wrapper.

Each `field_answers` entry uses only safe non-file kinds: `text`, `email`,
`tel`, `url`, `number`, `date`, `textarea`, `select`, `checkbox`, or `radio`.
`ats` is `greenhouse`, `lever`, or `*`; `name` and/or `label` identifies the
field and at least one is required. String kinds use string values; `select`
accepts a string or a list of strings; `checkbox` and `radio` use booleans.
Duplicate matchers, invalid observed-field values, sensitive/opaque answers,
non-finite numbers, duplicate JSON keys, and oversized/deep documents fail
closed. Keep the exact source bytes private; the run records only source kind
and SHA-256 provenance (plus preset name/schema metadata), never profile
values.

Select a preset as follows:

```bash
uv run --frozen jobs-assistant --db "$DB_PATH" autofill \
  --application-profile-dir "$PROFILE_DIR" \
  --application-profile-preset "$PRESET_NAME" \
  --resume-file "$RESUME_FILE" \
  --artifact-root "$ARTIFACT_ROOT" \
  --ats auto
```

## Application-preferences v1

Preferences are a separate private JSON document with exactly these top-level
keys:

```json
{
  "schema_version": 1,
  "mappings": [
    {
      "ats": "lever",
      "kind": "email",
      "name": "email",
      "label": null,
      "value": "<safe-email>"
    }
  ],
  "opt_outs": [
    {
      "ats": "*",
      "kind": "textarea",
      "name": "cover_letter",
      "label": null
    }
  ],
  "review_order": [
    {
      "ats": "*",
      "kind": "email",
      "name": "email",
      "label": null
    }
  ]
}
```

Each matcher requires `ats` (`greenhouse`, `lever`, or `*`), a safe kind, and
at least one of `name` or `label`; if both are present, both must match. Safe
kinds are the same ten kinds listed for presets. Mapping entries additionally
require `value`: strings for scalar kinds, a string or string list for
`select`, and a boolean for `checkbox`/`radio`. Unknown keys, duplicates,
conflicts, sensitive descriptors or values, and opaque/generated field
identities are rejected. A mapping and opt-out may not share a matcher.

Use the atomic editor for the forms it supports: a matcher with exactly one of
`--name` or `--label`, plus a scalar string value or a parsed boolean for
`checkbox`/`radio`. The editor cannot author a matcher containing both
identifiers or a list-valued multi-select mapping. For either form, create a new
v1 JSON document, set mode `0600`, and validate it with
`application-preferences show` before supplying it to a workflow. Do not
hand-edit a preferences file while a workflow may be reading it.

The preferences path must stay beneath the current user-owned working
directory; traversal, symlinks, non-owned files, and non-regular files are
rejected. Existing file mode bits are not enforced, so set the file to `0600`
before use. Atomic editor writes use mode `0600` and fsync before replacement.

```bash
uv run --frozen jobs-assistant application-preferences init "$PREFERENCES_JSON"
uv run --frozen jobs-assistant application-preferences show "$PREFERENCES_JSON"
uv run --frozen jobs-assistant application-preferences set-mapping "$PREFERENCES_JSON" \
  --ats lever --kind email --name email --value '<safe-value>'
uv run --frozen jobs-assistant application-preferences remove-mapping "$PREFERENCES_JSON" \
  --ats lever --kind email --name email
uv run --frozen jobs-assistant application-preferences set-opt-out "$PREFERENCES_JSON" \
  --ats '*' --kind textarea --name cover_letter
uv run --frozen jobs-assistant application-preferences remove-opt-out "$PREFERENCES_JSON" \
  --ats '*' --kind textarea --name cover_letter
uv run --frozen jobs-assistant application-preferences set-review-order "$PREFERENCES_JSON" \
  --ats '*' --kind email --name email
uv run --frozen jobs-assistant application-preferences remove-review-order "$PREFERENCES_JSON" \
  --ats '*' --kind email --name email
```

`init` does not overwrite. `set-*` replaces an identical matcher and
`remove-*` removes it. `show` reports schema, matcher metadata, and hashed or
length-only value metadata; it does not print mapping values. Preference bytes
are read and hashed exactly and the digest is retained privately in the run
manifest.

## Headed handoff and review

`--headed` is a handoff, not a submit mode. Once the evidence and review state
are durable, the CLI releases browser ownership and returns while the headed
window remains independently alive. The user closes the tab/window to end the
handoff; there is no timer and the parent does not reconnect through CDP. After
closing it, pass `--confirm-window-closed` to a completing or retrying review
transition.

```bash
uv run --frozen jobs-assistant --db "$DB_PATH" autofill-review \
  --artifact-root "$ARTIFACT_ROOT" list --limit 10

uv run --frozen jobs-assistant --db "$DB_PATH" autofill-review \
  --artifact-root "$ARTIFACT_ROOT" show "$RUN_ID"

uv run --frozen jobs-assistant --db "$DB_PATH" autofill-review \
  --artifact-root "$ARTIFACT_ROOT" complete \
  --run-id "$RUN_ID" --outcome skipped --confirm-window-closed

# This records a human submission; it does not submit.
uv run --frozen jobs-assistant --db "$DB_PATH" autofill-review \
  --artifact-root "$ARTIFACT_ROOT" complete \
  --run-id "$RUN_ID" --outcome submitted --confirm-window-closed \
  --annotation-file "$ANNOTATION_FILE"

uv run --frozen jobs-assistant --db "$DB_PATH" autofill-review \
  --artifact-root "$ARTIFACT_ROOT" retry \
  --run-id "$RUN_ID" --confirm-window-closed
```

`retry` is explicit, latest-run guarded, and requeues the job for a new run; it
never silently repeats a paid fetch or a browser mutation. A review annotation
is copied into the private run and bounded before indexing.

## Private artifacts and diagnostics

Each run has an owner-only (`0o700`) directory below the configured artifact
root. The names below are a shape, not a path to copy from runtime data:

```text
<artifact-root>/
  run-<id>/
    run.json
    claim.json
    input/<configured-resume-basename>
    review_session.json
    observation.json
    plan.json
    actions.json
    filled_state.json
    job_description.txt                 # when the listing provides one
    iterations/0001/{action,observation,plan,checkpoint}.json
    screenshots/                         # optional private captures
    annotations/                         # optional human notes
    browser_failure.json                 # optional bounded diagnostic
    browser_cleanup_failure.json        # optional bounded cleanup diagnostic
```

Artifacts are read back and SHA-256 verified. Exact profile and preference
bytes remain at their owner-supplied source locations; `run.json` records their
digests and preset metadata rather than copying them. The configured resume is
staged under `input/`, while observations, plans, actions, screenshots, notes,
and other generated evidence remain inside the private run directory. Public
CLI output exposes only sanitized status fields and opaque run references.

A browser diagnostic is intentionally coarse and private:

```json
{
  "version": 1,
  "stage": "startup|navigation|observation|mutation|handoff",
  "operation": "<allowlisted-operation>",
  "code": "<allowlisted-diagnostic-code>",
  "iteration": 1,
  "ats_policy": "greenhouse|lever",
  "no_final_submit": true,
  "protocol": "length-prefixed-json-v1"
}
```

`browser_cleanup_failure.json` uses the same bounded fields with cleanup
metadata. Diagnostics contain no URL, filesystem path, process identity,
exception text, stderr, credential, applicant value, or job description. A
public `browser_error` does not identify a more specific cause; inspect only
the private run directory when diagnosing a local failure.

For a local browser-backed verification, use the project wrapper rather than a
raw integration-test command:

```bash
npm run puppeteer-verify
```

See [Application RPC](application-rpc.md) for the persistent JSONL coordinator,
and return to the [README](../README.md) for the project landing page.
