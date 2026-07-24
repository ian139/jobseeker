---
name: resume-generation
description: >-
  Use when generating, customizing, or inspecting job-specific resume artifacts.
  Routes between standalone `resume-generate` and application-service
  `jobs-assistant resume-generate`, enforcing source claim grounding, one-page
  density, LaTeX/PDF rules, and owner-private artifact boundaries.
---

# Resume Generation

Use this skill when tasked with generating, customizing, showing, or listing resume artifacts for jobs in the backlog.

Core rule:

> Ground every claim in explicit source profile facts. Resume output is preparation for human review only—never submit automatically.

---

## 1. Surface Selection

The codebase has two distinct, non-interchangeable resume generation surfaces. Do not mix their profiles, source files, databases, or artifact roots.

| Surface | Command / CLI | Default Profile / Source | Default Artifact Root | Purpose |
|---|---|---|---|---|
| **Standalone Generator** | `resume-generate` | `resume/generator/profile.json`<br>`resume/generator/Resume.tex`<br>`resume/generator/SKILL.md` | `data/generated-resumes-generator/` | High-density LaTeX/PDF generator built from source-backed candidate claims and single-page fitting rules. Reads DB read-only. |
| **Application Service** | `jobs-assistant resume-generate` | Explicit `--profile-json` / `--application-profile-json` plus `--source-resume` | `data/generated-resumes/` | Application-integrated service contract that records generated-resume state in SQLite for guarded autofill workflows. |
| **Artifact Inspection** | `jobs-assistant resume-list`<br>`jobs-assistant resume-show` | N/A | `data/generated-resumes/` | Inspect and list application-service generated-resume metadata without exposing private candidate data. |

---

## 2. Standalone Generator (`resume-generate`)

### Command Shape

```bash
uv run --frozen resume-generate \
  --db "$DB" \
  --profile resume/generator/profile.json \
  --template resume/generator/Resume.tex \
  --skill resume/generator/SKILL.md \
  --output-root data/generated-resumes-generator \
  --limit 1
```

### Options & Rules
- `--job-id ID`: Target a specific positive queued job ID (repeatable up to 100).
- `--profile`: Must be a valid UTF-8 JSON document (defaults to `resume/generator/profile.json`).
- `--template`: LaTeX template file (defaults to `resume/generator/Resume.tex`).
- `--skill`: Governs generation rules, single-page density, and claim constraints (defaults to `resume/generator/SKILL.md`). **Do not move or alter this file.**
- Read-only on DB: Inspects non-empty job descriptions but does not mutate job status.

---

## 3. Application-Service Generator (`jobs-assistant resume-generate`)

### Command Shape

```bash
uv run --frozen jobs-assistant --db "$DB" resume-generate \
  --job-id <JOB_ID> \
  --profile-json path/to/application-profile.json \
  --source-resume resume/Main_Resume.pdf \
  --artifact-root data/generated-resumes
```

### Options & Rules
- `--job-id`: Exactly one positive queued job ID, or use `--next` for the next eligible queued job.
- Requires explicit candidate facts via `--profile-json` or its `--application-profile-json` alias.
- Requires an explicit source resume via `--source-resume`.
- `--artifact-root` selects the private generated-resume artifact root.
- Records generated-resume identity in SQLite for later application workflows.

---

## 4. Safety Invariants & Privacy

1. **No Automated Submission:** Resume generation produces artifacts for human review. It must not apply to a job, submit an application, or trigger browser actions.
2. **Grounding & Provenance:** Every skill, title, bullet, and accomplishment must trace back to explicit source profile facts. Never invent employers, degrees, dates, metrics, or technologies.
3. **Private Artifact Roots:** Profile data, LaTeX source, PDF outputs, and provenance manifests remain owner-private (`0700` directories, `0600` files). Do not commit generated resumes or log raw candidate data.
4. **Single-Page Constraint:** Standalone generation targets exact one-page density. Unsupported requirements are left out rather than guessed or padded with second-page overflow.

---

## 5. Next Action

1. Determine whether the request requires Standalone (`resume-generate`) or Application Service (`jobs-assistant resume-generate`).
2. Verify private source files exist (`profile.json`, source resume/PDF, template).
3. Execute the corresponding CLI command with explicit `--db` and the surface's private output/artifact-root path.
4. Confirm generated artifacts exist in the designated private output directory.
