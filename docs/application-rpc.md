# Application RPC coordinator

[Back to the project README](../README.md) · [Application drafts and autofill](application-drafts.md)

`application-rpc` is a local stdin/stdout JSONL boundary for one persistent,
OMP-owned application-draft coordinator. It uses the same SQLite claim,
observation, resolver, guarded-action, evidence, and headed-handoff workflow as
`autofill`; it does not add a second browser planner. Final submission is never
exposed.

## Setup

Install the project-locked native OMP package, Puppeteer, and the local browser
before starting the service:

```bash
npm install
npm run install-browser
```

Initialize the owner-private SQLite backlog before the first run:

```bash
uv run --frozen jobs-assistant --db "$DB_PATH" init-db
```

Start the service with immutable, explicit startup inputs:

```bash
uv run --frozen jobs-assistant application-rpc \
  --db "$DB_PATH" \
  --artifact-root "$ARTIFACT_ROOT" \
  --resume-file "$RESUME_FILE" \
  --application-profile-json "$PROFILE_JSON" \
  --application-preferences "$PREFERENCES_JSON" \
  --omp-runtime-root "$OMP_RUNTIME_ROOT" \
  --ats auto
```

Replace each shell variable with an owner-private location or validated input.
The service is headed-only: there is no headless RPC mode and no `--headed`
flag to add. Use `--application-profile-preset NAME` together with
`--application-profile-dir PATH` instead of `--application-profile-json` when
using a named preset. `--applicant-description-file PATH` is optional resolver
context.
`--omp-executable PATH` is optional and must identify a trusted
owner-private executable; otherwise the project-locked package is used.
`--omp-profile NAME` optionally selects a validated native OMP profile. Keep
OMP runtime state in a private `--omp-runtime-root`.

Only explicitly configured OMP authentication variables are forwarded to the
native child. See [`.env.example`](../.env.example) for names and keep any
`.env` file private:

- `OMP_AUTH_BROKER_URL`
- `OMP_AUTH_BROKER_TOKEN`
- `OPENAI_API_KEY`

Do not put credentials in JSONL payloads, profile IDs, annotations, or command
responses. Ambient shell variables, proxies, and the ambient `PATH` are not
forwarded to the child; the service builds a trusted executable path instead.

## JSONL boundary

The service reads one bounded UTF-8 JSON object per stdin line and writes one
JSON object per stdout line. Responses and durable redacted progress events can
be interleaved, so a caller must parse every line independently. A malformed,
too-large, or unknown-field request fails closed without reaching the browser.
The JSON request object is bounded to 512 KiB and has exactly these envelope
keys:

```json
{
  "protocol_version": 1,
  "request_id": "<canonical-lowercase-uuid>",
  "operation": "run.start",
  "deadline_unix_ms": 1780000000000,
  "run_id": null,
  "payload": {}
}
```

`deadline_unix_ms` is an absolute Unix-millisecond deadline no more than five
minutes after the receiver's current time. A start request has `run_id: null`;
all other lifecycle requests carry a positive run ID. `request_id` must be a
canonical lowercase UUID. Unknown envelope or payload keys are rejected; do not
add tracing, path, secret, or model fields.

Every response uses this exact envelope shape:

```json
{
  "protocol_version": 1,
  "request_id": "<canonical-lowercase-uuid>",
  "operation": "run.status",
  "ok": true,
  "run_id": 1,
  "state": "running",
  "action_sequence": 0,
  "event_sequence": 1,
  "result": {
    "ats": "greenhouse",
    "job_url": "https://boards.greenhouse.io/acme/jobs/123",
    "reason_code": null,
    "current_step": null,
    "coordinator_state": "prompting",
    "browser_state": "owned",
    "last_observation_sha256": null,
    "artifact_manifest_sha256": null,
    "human_review_ready": false,
    "handoff_committed": false,
    "automated_submission": false
  },
  "error": null
}
```

`state` is one of `starting`, `running`, `manual`, `blocked`,
`review_ready`, or `failed`. On failure, `result` is `null` and `error` has a
fixed public `code` and matching `message`; it never contains an exception,
path, DOM, model text, or profile value.

Progress events are also JSONL objects with this redacted shape:

```json
{
  "run_id": 1,
  "sequence": 1,
  "request_id": "<canonical-lowercase-uuid>",
  "action_sequence": 0,
  "timestamp": "<utc-timestamp>",
  "event_type": "<event-type>",
  "summary_code": "<bounded-summary-code>",
  "observation_sha256": null
}
```

Treat event and sequence values as progress metadata, not as permission to
replay a browser action.

## Lifecycle operations

External callers may send only these four operations:

| Operation | `run_id` | Payload | Effect |
|---|---:|---|---|
| `run.start` | `null` | Exact start object below | Atomically claims one queued job, starts the guarded OMP/browser workflow, and returns a starting status. |
| `run.status` | Positive | `{}` | Reads the latest sanitized status for a run owned by this coordinator. |
| `run.resume` | Positive | `{}` | Resumes an active run paused for manual intervention when it is explicitly resume-eligible. |
| `run.cancel` | Positive | `{}` | Durably requests cancellation of an active run and tears down its work. |

`run.start` has exactly this payload shape:

```json
{
  "goal": "prepare_application_draft",
  "job_url": "<exact-supported-greenhouse-or-lever-url>",
  "candidate_profile_id": "<profile-source-sha256>",
  "configured_resume_id": "<resume-source-sha256>",
  "headed": true
}
```

The goal is fixed. `job_url` must be an exact supported Greenhouse or Lever
initial route; it is not a generic navigation target. `candidate_profile_id`
and `configured_resume_id` are opaque SHA-256 identifiers derived from the
exact immutable startup profile/preset and resume snapshots. Send those hashes,
not paths or raw values. A changed startup snapshot or mismatched identifier
returns `request_conflict`. `headed` must be `true`.

`run.start` returns a lifecycle result with these fields:

```json
{
  "ats": "greenhouse",
  "job_url": "<exact-supported-url>",
  "reason_code": null,
  "current_step": "<bounded-step-or-null>",
  "coordinator_state": "starting",
  "browser_state": "starting",
  "last_observation_sha256": null,
  "artifact_manifest_sha256": null,
  "human_review_ready": false,
  "handoff_committed": false,
  "automated_submission": false
}
```

The coordinator state is one of `starting`, `prompting`, `awaiting_resume`,
`executing`, `cancelling`, or `terminal`. Browser state is one of
`not_started`, `starting`, `owned`, `idle`, `handed_off`, `closed`, or
`failed`. Hash fields are digests only. A successful draft handoff reports
`reason_code: "draft_ready"`, `human_review_ready: true`,
`handoff_committed: true`, and `automated_submission: false`.

`run.resume` is not a submit or retry helper. It is accepted only while the
same active run is awaiting a manual fix and before handoff commit. If the run
is terminal, handed off, cancelled, or not owned by this service, it fails
closed. `run.cancel` is likewise limited to an active, owned run; a completed
or handed-off run is not re-opened.

## Opaque identity, idempotency, and deadlines

Treat all identifiers as handles:

- The configured resume ID is the SHA-256 of the exact resume snapshot used by
  the service. The candidate-profile ID is the SHA-256 of the exact profile
  JSON or preset bytes; the built-in empty profile has a deterministic canonical
  digest. Preferences are also hashed privately, but are not sent in the
  request envelope.
- The numeric `run_id` identifies a durable run owned by this coordinator. It
  does not reveal a filesystem path, database row contents, or applicant data.
- Observation, manifest, screenshot, and evidence identifiers are digests or
  observation-scoped opaque IDs. Element IDs become invalid after a new
  observation; never cache or invent them.

A request's semantic intent includes protocol version, request ID, operation,
run ID, and every payload value, but excludes only the deadline. Repeating the
same `request_id` with the same intent is idempotent and replays the durable
response when available. Reusing a request ID for a different intent returns
`request_conflict`. New intent requires a new UUID. If a deadline expires
while a durable transition is being reconciled, keep the same request ID when
checking the outcome; do not issue a new start merely because a response was
slow.

Deadlines are enforced before claim, OMP startup, lifecycle transitions, and
browser dispatch. An expired request returns `deadline_exceeded` and cannot
authorize a later browser action. A request may set a fresh deadline for the
same idempotent intent, still within the five-minute window.

## Internal OMP host-tool surface

The OMP child is given exactly these eight host tools. They are internal child
calls, not additional public lifecycle operations; a caller sending any
`browser.*` request directly to `application-rpc` receives
`unsupported_operation`.

| Tool | Exact payload | Purpose and gate |
|---|---|---|
| `browser.observe` | `{}` | Observe the current supported ATS page and produce a bounded observation plus its SHA-256. |
| `browser.fill_field` | `observation_sha256`, `element_id`, `value`, `confidence`, `reason` | Fill one observed safe field. Deterministic fills use `null` for all three value/confidence/reason fields; inferred fills require a validated value, confidence `0.7–1.0`, and a reason. |
| `browser.select_option` | Same five keys as `browser.fill_field` | Select one observed enabled option or a validated list for a multi-select. |
| `browser.set_checkbox` | Same five keys as `browser.fill_field` | Set one observed checkbox/radio with a boolean value. |
| `browser.upload_configured_resume` | `observation_sha256`, `element_id` | Upload only the configured resume to the observed eligible file field; no path is supplied by OMP. |
| `browser.activate_safe_control` | `observation_sha256`, `element_id` | Activate one observed ATS-approved, non-final progress control. |
| `browser.capture_screenshot` | `observation_sha256` | Capture private evidence for the current observation and return only an evidence digest. |
| `browser.prepare_human_handoff` | `observation_sha256` | Commit durable evidence and a headed review handoff; no submission is possible. |

Mutation payloads must use the current observation SHA-256 and an opaque element
ID from that observation. The coordinator rechecks route, frame, target,
field safety, value type, stale state, and no-submit policy immediately before a
mutation. A stale hash, unknown target, sensitive target, invalid value, or
final-like control is rejected.

The OMP child has no arbitrary navigation, JavaScript evaluation, DOM or host
URI access, cookie/storage access, arbitrary file upload, shell command,
extension, subagent, or session-reconnect tool. Safe progress is performed only
through the observed allow-listed control. A final-submit control is always a
terminal stop for human review.

## Headed-only handoff

The service always starts the browser headed and requires `headed: true` in
`run.start`. Handoff is committed only after private evidence and review state
are durable. The coordinator then releases browser ownership; the human keeps
the review window, completes any manual fields, and decides whether to submit,
skip, or retry outside the service. Closing the browser tab/window is the
close action. The service does not reconnect through CDP or expose a timer.

Use the review commands from the [application-drafts guide](application-drafts.md)
after closing a handed-off window. `autofill-review complete --outcome
submitted` records a human action and never calls a submit helper.

## Teardown and no-submit invariant

EOF, SIGINT, or SIGTERM closes the JSONL loop. Shutdown admission closes first,
active runs are cancelled, native OMP work and browser processes are stopped,
and process-group absence is verified before the service releases its private
runtime lease. Private evidence remains under the configured artifact root;
public responses and events retain only bounded redacted metadata and opaque
identifiers. If child absence cannot be proven, fail closed rather than
reconnecting to or reusing that browser.

There is no `run.submit`, no `browser.submit`, and no generic browser command.
Every lifecycle and handoff projection carries `automated_submission: false`.
Only a human in the handed-off headed window can submit, and the review command
can record that outcome only after the human has done so.

Return to the [README](../README.md) for the project landing page or see
[Application drafts and autofill](application-drafts.md) for route, profile,
preference, artifact, and review details.
