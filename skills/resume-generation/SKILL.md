---
name: resume-generation
description: Generate and verify one deterministic, source-backed, job-specific resume from a direct private job snapshot.
---

# Resume generation

Use only the canonical generator in `src/resume_generation/`. It is a narrow deterministic tool: it does not claim jobs, read or mutate SQLite, open a browser, upload files, or perform application/submission actions.

For Phase 3, do not invoke this CLI independently or generate after a generic claim. Use `generateBoundResume` or, normally, `prepareOrRecoverSupportedRun` from `src/phase1/preparation.mjs`. The coordinator stages the exact normalized bound description, disables advisory/model environment, validates the returned manifest/PDF, rechecks the full job snapshot, and only then claims and binds the run. Recovery reuses the selected validated artifact without recompilation.

## Fixed inputs

Invoke:

```bash
uv run --frozen python -m resume_generation.command \
  --profile private/resume/profile.json \
  --template resume/Resume.tex \
  --skill resume/SKILL.md \
  --output-root private/generated-resumes \
  --compiler /opt/homebrew/bin/tectonic
```

Send exactly one canonical UTF-8 JSON object on stdin, at most 64 KiB, with no trailing newline or other whitespace:

```json
{"company":"Example","description":"Exact private listing text","id":1,"location":null,"posted_at":null,"schema":"resume-job-v1","title":"Engineer"}
```

The exact keys are `schema`, `id`, `title`, `company`, `description`, `location`, and `posted_at`. The schema is `resume-job-v1`; `id` is positive; title, company, and description are nonblank. Never generate from title alone.

Success is one `generated-resume-v1` JSON object with exact keys `schema`, `job_id`, `artifact_ref`, `tex_path`, `pdf_path`, `report_path`, `manifest_path`, `pages`, `field`, `graduation_date`, and `matched_keywords`. Failures are one sanitized JSON error only; do not expose private inputs, paths, tracebacks, or compiler output.

## Governing invariants

1. Load and fingerprint `resume/SKILL.md` on every generation. Its digest is part of the immutable generation identity.
2. Ground every selected claim in `private/resume/profile.json`. Job text supplies relevance only, never applicant facts.
3. Preserve `resume/Resume.tex` structure, typography, margins, sections, and markers, including the guide-style `\resumeRoleContext`, `\resumeGroupHeading`, and `\resumeTechLine` commands.
4. Compile and inspect the actual PDF. Require one to two pages with extractable text.
5. Publish exactly five owner-private files in the fingerprinted artifact directory:
   - `resume.tex`
   - `resume.pdf`
   - `optimization.json`
   - `job_description.txt`
   - `manifest.json`
6. Reopen all five files, validate the manifest self-digest, byte counts, SHA-256 values, job identity, compiler/template/skill/profile fingerprints, and PDF page count before accepting success.
7. Reuse an existing artifact only when all five files and every identity check validate. Never repair or overwrite a tampered immutable artifact.
8. Keep deterministic generation functional when optional advisor access is absent or fails. Advice may rank known evidence IDs only.
9. Phase 3 model use never participates in resume content selection or generation. The only later model lane is allowed unresolved application-response inference/oversight.

## Prohibited surfaces

Do not activate or call `resume.py`, `resume_service.py`, `resume_artifacts.py`, `jobs-assistant resume-generate`, resume-list/show application-service commands, or any second renderer/profile schema. Do not substitute the source resume, another job’s PDF, or a partially verified artifact.
