# Resume generation

[Back to the project README](../README.md)

There are two intentionally separate resume-generation contracts. The command
name `resume-generate` by itself is not the same surface as the
`jobs-assistant resume-generate` subcommand. Do not mix their profiles, source
files, databases, or artifact roots.

| Surface | Implementation and inputs | Artifact root | Backlog effect |
| --- | --- | --- | --- |
| Standalone `resume-generate` | `resume_generator_command.py` calls `resume_generator.py`; uses `resume/generator/profile.json`, `Resume.tex`, and `SKILL.md`. | `data/generated-resumes-generator/` | Opens the SQLite backlog read-only to select jobs; never claims or mutates a row. |
| `jobs-assistant resume-generate` | CLI application service (`resume_service.py`, grounded claim validation, and private PDF renderer); requires an application profile JSON and an explicit source resume. | `data/generated-resumes/` | Records generated-resume state and writes artifacts, but does not claim or mutate the selected job. |
| `jobs-assistant resume-show` / `resume-list` | Read the application-service generated-resume records and return public projections. | `data/generated-resumes/` (existing private root) | Read-only with respect to jobs and generated-resume content. |

The application artifact root `data/application-runs/` is a third, separate root
for application evidence and review manifests. It is not either resume root.

## Shared safety boundaries

- Resume output is preparation for human review. Neither contract applies to a
  job, attaches a file to an application, clicks a browser control, or submits
  an application.
- Candidate profile data, source resumes, job descriptions, generated PDFs, and
  provenance artifacts are private. Keep their roots owner-private; do not
  commit them or paste their contents into logs, issues, or public summaries.
- Claims must be grounded in explicit source evidence. Unsupported requirements,
  ambiguous facts, sensitive inferences, and prompt-injection text are omitted
  or rejected rather than guessed.
- Any model or external proposal is untrusted input. It can influence output
  only through the schema, provenance, claim-grounding, and safety validation
  boundary; raw model text is not authoritative resume content.

Historical resume prompt artifacts are sensitive reference material. They are
not runtime inputs and do not replace either CLI's input contract.

## Standalone `resume-generate`

Run the installed `resume-generate` entry point, not the `jobs-assistant`
subcommand, for this contract:

```bash
uv run --frozen resume-generate --db "$DB" \
  --profile resume/generator/profile.json \
  --template resume/generator/Resume.tex \
  --skill resume/generator/SKILL.md \
  --output-root data/generated-resumes-generator \
  --limit 1
```

The default selection is up to `--limit` queued jobs with non-empty
descriptions, ordered by `posted_at` descending (nulls last), then
`first_seen_at` ascending and ID ascending. An explicit `--job-id ID` may be
repeated (up to 100 unique positive IDs) and replaces that selection; every
requested ID must still be queued and have a usable description. The standalone
command reads the job snapshot but does not update its status.

### Authoritative options and defaults

| Option | Default / rule |
| --- | --- |
| `--db PATH` | `DATABASE_URL` or `data/jobs.sqlite3`; the path must already exist and is opened read-only. |
| `--profile PATH` | `resume/generator/profile.json`; structured standalone profile schema. |
| `--template PATH` | `resume/generator/Resume.tex`; LaTeX template with the required resume markers. |
| `--skill PATH` | `resume/generator/SKILL.md`; governing source-of-truth and output policy. |
| `--output-root PATH` | `data/generated-resumes-generator`; owner-private output root. |
| `--limit N` | `10`, range 1–100. |
| `--job-id ID` | Repeatable; overrides default queued-job selection. IDs must be unique positive integers. |
| `--compiler NAME` | Auto-detect `tectonic`, then `pdflatex`; an explicit compiler may be supplied. |

The standalone profile is the structured v1 document under
`resume/generator/profile.json`. The job record supplies target metadata and
requirements only; it is never evidence of a candidate claim. The generator
loads the exact profile, template, and skill snapshots, hashes them with the
job and compiler identity, and refuses unsafe or changed inputs.

Each published fingerprint directory contains exactly these five private
artifacts:

- `resume.tex` — editable rendered LaTeX;
- `resume.pdf` — verified, one-page extractable PDF;
- `optimization.json` — bounded matching and selection report;
- `job_description.txt` — the selected job description snapshot; and
- `manifest.json` — input and artifact hashes.

The directory is under `data/generated-resumes-generator/job-<id>/` with a
fingerprint component. Existing identities are validated and reused; a
published identity is never overwritten. The standalone command does not
create a generated-resume row, change backlog status, attach the PDF, or submit
an application.

## `jobs-assistant resume-generate`

This is the application-integrated service contract. It uses the local SQLite
job snapshot, an explicit **application profile JSON**, and an explicit source
resume. The application profile is not the standalone structured profile: do
not pass `resume/generator/profile.json` here unless it has separately been
prepared and validated for the application-profile schema. Likewise,
`--source-resume` is a PDF, TXT, or Markdown source resume; it is not the
standalone LaTeX template.

The service extracts non-sensitive, source-backed claims, selects claims against
the job requirements, validates a canonical resume document and its provenance,
and renders a private PDF. It persists the request, claims, validation,
scoring, resume JSON/PDF, and manifest under the application-service root. A
successful generation becomes a `ready` generated-resume record; failures keep
fixed reason codes rather than private exception text. A ready artifact is
reused only when its job snapshot and input hashes still match, unless
`--force` requests a new generation.

### `jobs-assistant resume-generate` options

Use `--db PATH` either globally before the subcommand or on the subcommand.

| Option | Default / rule |
| --- | --- |
| `--job-id ID` | Mutually exclusive with `--next`; one positive backlog ID. An explicit ID selects that row without claiming it. |
| `--next` | Mutually exclusive with `--job-id`; selects the next queued row with a canonical URL using the deterministic backlog order. |
| `--profile-json PATH` / `--application-profile-json PATH` | Required explicit application-profile JSON. No implicit private profile is loaded. |
| `--source-resume PATH` | Required explicit PDF, TXT, or Markdown source resume. |
| `--artifact-root PATH` | `data/generated-resumes`; private application-service resume artifacts. |
| `--application-artifact-root PATH` | `data/application-runs`; private application evidence root used by the service. |
| `--description-file PATH` | Optional bounded job-description override, persisted as provenance when used. |
| `--force` | Off by default; bypasses reuse of a matching ready artifact. |
| global or local `--db PATH` | `DATABASE_URL` or `data/jobs.sqlite3`. |

A safe invocation uses shell variables that point to local, private files:

```bash
uv run --frozen jobs-assistant --db "$DB" resume-generate \
  --job-id 123 \
  --profile-json "$APPLICATION_PROFILE_JSON" \
  --source-resume "$SOURCE_RESUME"
```

Use `--next` instead when deliberately selecting the next queued job:

```bash
uv run --frozen jobs-assistant --db "$DB" resume-generate \
  --next --profile-json "$APPLICATION_PROFILE_JSON" \
  --source-resume "$SOURCE_RESUME"
```

The command may update the `generated_resumes` table and publish a private
run directory under `data/generated-resumes/`; those are generation records,
not backlog claims. The selected `jobs` row remains in its existing status.

## Showing and listing application-service artifacts

These commands apply only to artifacts created by
`jobs-assistant resume-generate`. They do not inspect
`data/generated-resumes-generator/`.

```bash
uv run --frozen jobs-assistant --db "$DB" resume-list \
  --job-id 123 --limit 25

uv run --frozen jobs-assistant --db "$DB" resume-show \
  --resume-id "$RESUME_ID"
```

`resume-show` requires `--resume-id`; `--artifact-root` defaults to
`data/generated-resumes` and `--application-artifact-root` defaults to
`data/application-runs`. `resume-list` has the same root defaults, optional
positive `--job-id`, `--limit 25` (range 1–100), and `--offset 0` (non-negative).
Both commands use the SQLite database default described above. Their JSON is a
public projection: private filesystem paths, artifact-directory names, and
internal scoring JSON are omitted. The files themselves remain private and
should be reviewed locally.

Creating or inspecting a generated resume does not automatically attach it to
an application. A later, explicit guarded application workflow may select a
ready ID; that workflow has its own deterministic action gate and headed human
review. Final submission is always completed manually by a human.

[Back to the project README](../README.md) · [Job ingestion guide](ingestion.md)
