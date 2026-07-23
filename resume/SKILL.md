# Resume Generation Skill

Version: 1

This file is the governing instruction set for every generated resume. The
resume generator must load this file, validate it, and include its exact
SHA-256 digest in the generation fingerprint and manifest before publishing
artifacts. A missing, unreadable, symlinked, malformed, or changed skill file
invalidates generation or creates a new cache identity.

## Source-of-truth policy

- Candidate facts and claims must come only from `resume/profile.json`.
- The queued job record supplies target metadata and requirements for matching
  only; it is never evidence that the applicant possesses a requirement.
- Preserve each selected claim's source IDs and provenance.
- Never invent employers, titles, dates, degrees, tools, metrics, impact,
  permissions, or responsibilities.
- Treat public repository metadata as technology evidence, not proof of
  measurable impact or resume permission.
- Never infer sensitive, legal, protected-class, financial, authentication,
  medical, or other private information.

## Matching and prioritization

- Match explicit job title and requirement terms against source-backed profile
  claims; title signals take precedence over description-only signals.
- Select relevant experience first, then education, leadership, projects, and
  skills only when supported by the profile and useful for the job.
- Prefer concrete, truthful accomplishments over keyword repetition.
- Keep unsupported or ambiguous requirements out of the resume and report them
  as unsupported rather than guessing.

## Graduation policy

- Use December 2026 by default.
- Use May 2027 only when the combined job title and description explicitly
  contain a spring term and `co-op`, `coop`, or `internship`.
- Do not change graduation dates for any other hiring window or title.

## Output invariants

- Preserve the supplied LaTeX template's structure and style markers.
- Produce editable LaTeX and exactly one page of extractable PDF text.
- Publish only the five private artifacts: `resume.tex`, `resume.pdf`,
  `optimization.json`, `job_description.txt`, and `manifest.json`.
- Hash the job, profile, skill, template, compiler identity, and published
  artifacts. Never overwrite an existing artifact identity.
- Resume generation is preparation only. It must not apply to a job, submit an
  application, or mutate the backlog database.

## Review expectation

Every output must be suitable for human review. If source evidence is missing,
ambiguous, or insufficient to fit these rules, fail safely or omit the claim;
do not fill the gap with model judgment.
