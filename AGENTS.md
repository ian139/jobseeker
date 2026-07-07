# Agent Operating Notes

## Product direction

The active app remains a minimal local job scraping/backlog ingestion assistant.

Set-in-stone active features:

- scraper/backlog ingestion into SQLite;
- filtering and quality gates for source jobs;
- profile-shaped search/filter ideas;
- TheirStack preview/paid-fetch concepts and helpers, including the `job-scrape` entrypoint;
- optional normalized JSON/API feed import for fixtures and backfills.

The applier remains a future product concept until deliberately rebuilt. The target rebuild path is an OMP `workflowz` loop that discovers listings from the backlog, matches resume/profile keywords against job descriptions, prepares tailored draft context, opens supported ATS pages through a Puppeteer-backed browser-adapter layer, fills only safe supported fields, persists artifacts/fixtures/preferences, marks weird or unsafe cases manual/blocked, and leaves completed drafts ready for human review and manual submission.

Do not keep building on archived observer/resolver/executor/runner code as if it were active. Rebuild application automation deliberately through OMP `workflowz` with fresh contracts, focused tests, explicit safety gates, and Greenhouse as the first ATS adapter.

## Current active workflow

```text
TheirStack / feed / scraper output
  ↓
Normalize source job
  ↓
Filtering and quality gates
  ↓
SQLite jobs backlog
  ↓
Sync/run metadata
```

Future applier concept:

```text
SQLite queued job
  ↓
Open application URL
  ↓
Observe DOM/frames
  ↓
Resolve safe answers from profile/resume/job context
  ↓
Execute guarded non-final actions
  ↓
Persist review evidence
  ↓
Stop before final submit
```

That second workflow is concept-only until rebuilt.

## Safety policy

Hard rules:

- Never mass-submit applications.
- Never click, keypress, navigate, or script-trigger any final-submit, application-submit, mass-apply, bulk-submit, or equivalent terminal application action. Submit-like controls are stop points, not automation targets.
- Never answer sensitive, legal, demographic, disability, veteran, sponsorship, clearance, authorization, salary, availability, assessment, identity, signature, consent, or assessment fields by inference. If an explicit profile/config value is absent, leave the field unchanged and mark the run manual/blocked.
- Never bypass sign-in, CAPTCHA, assessments, identity checks, or payment gates.
- Upload only the configured resume file unless the user explicitly changes policy.
- The applier loop must always stop after persisting review evidence and before any final submission. A completed run may leave a draft/application ready for human review and manual submission only; it must not submit, queue submission, schedule submission, or expose a helper that can submit multiple applications.
- Browser adapters, including Puppeteer/browser adapters, may execute only non-final deterministic actions produced by the workflowz executor contract. Any action whose selector, accessible name, URL, form state, or page transition matches submit-like or unsafe patterns must be rejected and recorded as manual/blocked.
- Every browser action (fill, click, select, upload) must pass through a deterministic safety gate before execution: the gate checks the action's target selector against the observed page snapshot and rejects any action targeting an unsafe or unobserved element.
- The browser safety gate must be a deterministic allow/deny decision over an observed snapshot plus the planned action; it must not rely on LLM judgment at execution time, hidden DOM assumptions, or selectors that were not observed in the current page/frame snapshot.
- LLM output must be schema-validated and pass deterministic safety gates before any fill or click; never allow raw LLM output to drive browser actions unchecked.
- The `UNSAFE_RE` and `FINAL_RE` patterns in `application.py` are the canonical safety classifiers; any new ATS adapter must route through them or an equivalent gate.
- Validation artifacts (`observation.json`, `plan.json`, `actions.json`, `filled_state.json`, `job_description.txt` when available, screenshots, fixtures, logs, user annotations) must be persisted per run so Greenhouse handling and preferences improve over time; never discard evidence that could inform future safety decisions.
- Treat destructive database cleanup as requiring a clear user instruction.

These rules apply to current and future applier work even though archived implementations remain reference-only.

- Resume keyword matching from job descriptions may produce notes, answer text, or cover-letter/draft content, but the workflow must still upload only the configured resume file unless the user changes policy. Tailoring does not imply rewriting or submitting a modified resume artifact.
- Hardcoded profile/application data (name, email, phone, location, work history dates, education, URLs, demographic answers) must be provided explicitly in a profile JSON or config file and must not be inferred from the resume text. The resume is a document to upload; the profile is the structured source of truth for form fields.
- Source profiles are search/filter inputs for discovery commands such as `job-scrape`, `theirstack-preview`, and `theirstack-sync`. Application profiles are explicit applicant facts for guarded draft filling, and resume metadata is context for matching/upload selection only.

## Applicant/profile reference

Default applicant artifacts remain reference data, not permission to infer sensitive/legal answers.

- Resume file: `archive/old-applier/data/Main_Resume.pdf`
- LinkedIn: `https://www.linkedin.com/in/ianrapko`
- Personal site: `https://immemorized.com`
- Profile JSON examples may live under `scraper/data/` or `data/`.

## Protected files and data

Do not touch casually:

- `archive/` code and data.
- Runtime SQLite data under `data/` or `scraper/data/` unless explicitly instructed.
- Applicant profile/resume data except through documented profile workflows.

## Active code layout

Active source:

- `src/jobs_assistant/contracts.py`: ingestion/domain dataclasses.
- `src/jobs_assistant/db.py`: jobs/sync SQLite schema and helpers.
- `src/jobs_assistant/backlog.py`: queue upsert, dedupe, backlog counts.
- `src/jobs_assistant/theirstack.py`: TheirStack payload/client/sync helpers.
- `src/jobs_assistant/job_source.py`: JSON/API feed normalization and import.
- `src/jobs_assistant/application.py`: guarded browser-adapter/LLM-assisted autofill primitives and run persistence.
- `src/jobs_assistant/cli.py`: minimal CLI entrypoints, including `jobs-assistant` and `job-scrape`.
- `tests/`: focused scraper/ingestion/backlog/TheirStack/CLI/autofill tests.
- `scripts/smoke.sh`: repository smoke check script.

Archived applier source:

- `archive/minimized-20260706/applier/`: removed first-principles active applier implementation; reference only and not a runnable package snapshot.
- `archive/old-scraper/`: older scraper/application pipeline reference.
- `archive/old-applier/`: older monolithic applier/reference applicant data.

Archived code belongs under `archive/`. Do not import archived modules from active code.

## Development rules

- Keep business logic as pure functions over typed data where possible.
- Keep side effects at boundaries: CLI, HTTP, SQLite, filesystem.
- Add tests for every new filtering, ingestion, dedupe, profile, or TheirStack branch.
- Prefer boring schemas and explicit JSON/SQLite contracts.
- Do not add a second convention beside an existing one.
- Use cheap Ollama Cloud DeepSeek aliases for OMP/workflowz by default: `TASK` (`ollama-cloud/deepseek-v4-pro`) for implementation, resolver, and browser workers; `COMMIT` (`ollama-cloud/deepseek-v4-flash`) for low-risk summaries or commit-style synthesis; escalate to another model family only when the subtask contract explains why.

## Model aliases

| Model | Role alias | Reasoning |
|---|---|---|
| `openai-codex/gpt-5.5` | `DEFAULT` | low |
| `openai-codex/gpt-5.5` | `SLOW` | xhigh |
| `openai-codex/gpt-5.5` | `PLAN` | high |
| `openai-codex/gpt-5.5` | `ADVISOR` | xhigh |
| `ollama-cloud/kimi-k2.7-code` | `SMOL` | high |
| `ollama-cloud/glm-5.2` | `VISION` | xhigh |
| `ollama-cloud/glm-5.2` | `DESIGNER` | xhigh |
| `ollama-cloud/deepseek-v4-flash` | `COMMIT` | low |
| `openrouter/google/gemini-3.5-flash` | `TINY` | inherit |
| `ollama-cloud/deepseek-v4-pro` | `TASK` | high |

## Metrics-first goals and minimal tooling

Every substantial task should name:

- goal;
- metric;
- target threshold or invariant;
- measurement source;
- stop condition.

When Prometheus/Grafana are not wired for the touched path, use focused tests, CLI output, fixture counts, SQLite queries, or markdown checklists as temporary metrics.

## Orca/OMP orchestration workflow

Use the Orca/OMP orchestration command surface for non-trivial implementation work. The installed CLI exposes this as `orca orchestration ...`; implementation subagents must be coordinated through tracked orchestration tasks and dispatches.

If a project-local wrapper named `omp orchestrate` exists during execution, use it only when it creates the same tracked orchestration tasks and dispatches. Otherwise use:

```bash
orca orchestration task-create --spec <spec> --json
orca terminal create --worktree active --title <role> --command "codex" --json
orca terminal wait --terminal <handle> --for tui-idle --timeout-ms 60000 --json
orca orchestration dispatch --task <task_id> --to <handle> --inject --json
orca orchestration check --wait --types worker_done,escalation,decision_gate --timeout-ms 900000 --json
```

Orchestration subtasks must include:

- exact target files/symbols;
- non-goals and forbidden files;
- acceptance criteria;
- focused verification command;
- owner/subagent role;
- report contract.

Role/model mapping for this project:

- Coordinator / integration owner: `PLAN` (`openai-codex/gpt-5.5`, high) or the current session model.
- Implementation worker: `TASK` (`ollama-cloud/deepseek-v4-pro`, high).
- Tester worker: `TASK` (`ollama-cloud/deepseek-v4-pro`, high), because tests require reasoning about safety branches.
- Safety/review worker: `ADVISOR` (`openai-codex/gpt-5.5`, xhigh).
- Low-risk docs/status summarizer only: `COMMIT` (`ollama-cloud/deepseek-v4-flash`, low).

Worker completion contract:

- Each worker sends exactly one `worker_done` message.
- The message body lists modified files, tests run, and blockers.
- Workers use `ask` for blocking questions.
- Workers never send group lifecycle messages.

Delegate by need:

- Tester: high-signal tests and edge cases.
- reviewer: safety, quality, security, and scope review.
- task worker: implementation on explicit files.
- designer: UI/design work only.
- librarian: external API/library research.

The parent worktree is the integration authority. Compare worker reports/diffs, integrate only useful patches, reject unrelated edits, and verify in the parent.

Future applier revival must be decomposed through orchestration into at least:

1. source/backlog contract;
2. page observer contract;
3. resolver JSON contract;
4. executor safety contract;
5. persistence/output contract;
6. safety review and adversarial tests.


The orchestrated applier loop runs over durable artifacts, not a one-shot browser script:

```text
discover -> observe -> resolve -> execute guarded non-final actions -> persist evidence -> human review/manual submission
```

Practice rules:

- Use the Puppeteer browser-adapter boundary for page observation and guarded field actions; workers must not invent ad hoc browser-control paths outside observer/executor contracts. The current local install commands for the Puppeteer-backed path are `npm install` and `npm run install-browser`.
- Build Greenhouse first and keep ATS-specific selectors, field normalization, and quirks behind the ATS adapter protocol so Lever, Workday, and other ATS support can be added without changing resolver or safety contracts.
- Persist observed page fixtures/snapshots, resolver JSON, planned actions, rejected actions, filled-state evidence, `job_description.txt` when available, screenshots when available, and user preference annotations. These artifacts feed future adapter improvements and fixture tests; they are not permission to relax safety gates.
- Resolve application fields from explicit sources in priority order: profile JSON/config for identity and structured applicant facts, user preferences/annotations for repeated answer choices, resume/job-description keyword matching for non-sensitive draft wording, then manual/blocked status when no safe source exists.
- Keep resume keyword matching scoped to job-description analysis, fit notes, answer suggestions, and tailored draft text for review. It must not rewrite the configured resume artifact, infer protected/profile fields, or turn a draft into permission to submit.

No worker may add final-submit behavior until a separate submit policy exists and is tested.
## Verification

From a clean checkout with the committed `uv.lock`:

```bash
uv lock --check
uv run --frozen --extra dev python -m pytest
uv run --frozen jobs-assistant --help
```

Container verification:

```bash
docker compose build
docker compose up -d
docker compose ps
docker compose down
```

Never claim verification unless the command was actually run.

## Containerization contract

Everything should be able to run in containers by default. Host execution is a developer convenience, not the only verified path.

For every CLI entrypoint or service boundary added or changed, ensure:

- dependencies are declared in project files;
- required environment variables have examples or safe defaults;
- tests run from a clean checkout;
- Docker/compose wiring is updated when runtime behavior changes.

## Review policy

Prefer executable evidence over subjective review.

Useful findings cite:

- failing tests;
- missing tests;
- broken contracts;
- unclear ownership boundaries;
- containerization gaps;
- safety-sensitive logic;
- diffs outside scope.

## Verification contract for completed tasks

Final reports must include:

1. Summary of behavior changed.
2. Files changed.
3. Tests/checks run.
4. Container commands actually run or skipped reason.
5. Known risks or skipped checks.
6. Suggested next patch, if any.
