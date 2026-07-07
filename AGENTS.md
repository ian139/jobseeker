# Agent Operating Notes

## Product direction

The active app is now a minimal local job scraping/backlog ingestion assistant.

Set-in-stone active features:

- scraper/backlog ingestion into SQLite;
- filtering and quality gates for source jobs;
- profile-shaped search/filter ideas;
- TheirStack preview/paid-fetch concepts and helpers;
- optional normalized JSON/API feed import for fixtures and backfills.

The applier is set in stone as a future product concept, but the current implementation is archived. Do not keep building on the archived observer/resolver/executor/runner code as if it were active. Rebuild future application automation deliberately through OMP `workflowz` with fresh contracts, focused tests, and explicit safety gates.

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
- Never click final submit.
- Never answer sensitive fields by inference.
- Never bypass sign-in, CAPTCHA, assessments, identity checks, or payment gates.
- Upload only the configured resume file unless the user explicitly changes policy.
- Treat destructive database cleanup as requiring a clear user instruction.

These rules apply to any future applier revival even though the current applier implementation is archived.

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
- `src/jobs_assistant/cli.py`: minimal CLI entrypoint.
- `tests/`: focused scraper/ingestion/backlog/TheirStack/CLI tests.
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
- Use the `TASK` model alias (`ollama-cloud/deepseek-v4-pro`) for OMP/workflowz implementation workers unless a task specifically needs another model family.

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

## OMP workflowz workflow

Use OMP `workflowz` for non-trivial work. It is the coordination mechanism for this repo.

Workflowz subtasks must include:

- exact target files/symbols;
- non-goals and forbidden files;
- acceptance criteria;
- focused verification command;
- owner/subagent role;
- report contract.

Delegate by need:

- Tester: high-signal tests and edge cases.
- reviewer: safety, quality, security, and scope review.
- task worker: implementation on explicit files.
- designer: UI/design work only.
- librarian: external API/library research.

The parent worktree is the integration authority. Compare worker reports/diffs, integrate only useful patches, reject unrelated edits, and verify in the parent.

Future applier revival must be decomposed through workflowz into at least:

1. source/backlog contract;
2. page observer contract;
3. resolver JSON contract;
4. executor safety contract;
5. persistence/output contract;
6. safety review and adversarial tests.

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
