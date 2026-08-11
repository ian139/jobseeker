# Resume Generation Skill

Version: 2

This file is the governing instruction set for every generated resume. The
resume generator must load this file, validate it, and include its exact
SHA-256 digest in the generation fingerprint and manifest before publishing
artifacts. A missing, unreadable, symlinked, malformed, or changed skill file
invalidates generation or creates a new cache identity.

## Source-of-truth policy

- Candidate facts and claims must come only from `resume/generator/profile.json`.
- The queued job record supplies target metadata and requirements for matching
  only; it is never evidence that the applicant possesses a requirement.
- Preserve each selected claim's source IDs and provenance.
- Treat public repository metadata as technology evidence, not proof of
  measurable impact or resume permission.
- Never infer sensitive, legal, protected-class, financial, authentication,
  medical, or other private information.
- Exclude any job-application automation agent or application-submission workflow from generated resumes; it is outside this profile's resume scope.

## Matching and prioritization

- Match explicit job title and requirement terms against source-backed profile
  claims; title signals take precedence over description-only signals.
- Select relevant experience first, then education, leadership, projects, and
  skills only when supported by the profile and useful for the job.
- Prefer concrete, truthful accomplishments over keyword repetition.
- Keep unsupported or ambiguous requirements out of the resume and report them
  as unsupported rather than guessing.

## Industry framing

- Describe financial-software work in industry-neutral fintech vocabulary.
  Name the mechanism (programmatic settlement, treasury management, balance
  snapshots, claim processing, payment routing) rather than the industry
  label. Keep every underlying fact, metric, date, and employer unchanged;
  only the vocabulary framing changes.
- Never introduce crypto-industry jargon (blockchain, Web3, DeFi, tokens,
  staking, minting, specific chains) into generated resume text. Translate to
  the equivalent financial-software concept or omit.

## Presentation style (guide resume)

Model layout and density on the approved guide resume:

- Under each experience role, include a one-line role context statement
  (mandate, scale, scope) before the bullets when the profile supports it.
- When a role has four or more selected bullets, cluster them under bold
  competency group headings (for example Platform & Product, Infrastructure &
  Performance) instead of one flat list.
- End each substantial role with a per-role technology line naming the stack
  evidenced by that role's bullets.
- Lead bullets with the outcome metric when the source supports one; keep
  mechanism and scale in the same sentence.

## Resume writing rules

- Make every bullet a source-backed account of what the candidate built or
  owned. When accurate, lead with a specific builder or ownership verb such as
  designed, built, authored, shipped, modernized, or owned. Do not substitute a
  stronger verb for the supported scope.
- Avoid vague operator language, including worked on, contributed to,
  responsible for, maintained, supported, or led initiatives, when the
  evidence supports a more specific action. Never imply end-to-end ownership
  when the evidence instead supports collaboration or a bounded contribution.
- Name the actual product, team, feature, system, or user-facing surface. A
  reader must be able to picture what the candidate would build, not merely
  infer a broad category such as platform, AI, or backend.
- Include the technical mechanism and the observable outcome whenever source
  evidence supports them. Add relevant context, users, scale, baseline, delta,
  time, or business impact only when it is verified and makes the claim more
  concrete.
- For AI work, name the evidenced implementation, such as the model,
  inference stack, tool calling, retrieval pipeline, agent workflow, or
  evaluation method. Do not reduce specific work to generic AI/ML, LLM
  experience, or RAG language.
- Keep bullets concise. Remove repeated technologies, filler, and claims that
  do not clarify action, surface, mechanism, outcome, or context.
- Use words instead of slash-separated terms in bullet prose. CI/CD is the
  sole allowed exception.
- Render Technical Skills as an evidence-backed list of concrete languages,
  frameworks, systems, platforms, tools, or methods relevant to the target
  role. Exclude soft skills and broad, non-technical categories.

## Two-page density policy

- The resume may fill up to two pages. Treat two full pages as the density
  target, matching the guide resume's depth: role context lines, grouped
  bullets, per-role technology lines, and a substantial projects section.
- Never pad with unsupported claims; expand only with truthful, source-backed
  bullets, projects, and skills.
- Render role context (`\resumeRoleContext`) and a per-role technology line
  (`\resumeTechLine`) for every role where the profile supplies them. Use
  bullet group headings (`\resumeGroupHeading`) when a role's selected bullets
  carry group labels.
- Require a Technical Skills section whenever the profile has supported
  skills. Skills may occupy multiple rendered lines when the two-page budget
  permits.
- Prefer the fullest result produced by these rules. If the next supported
  bullet, project, or skill would overflow past two pages, omit it and keep
  the preceding result.

## Graduation policy

- Use December 2026 by default.
- Use May 2027 only when the combined job title and description explicitly
  contain a spring term and `co-op`, `coop`, or `internship`.
- Do not change graduation dates for any other hiring window or title.

## Output invariants

- Preserve the supplied LaTeX template's structure and style markers.
- Use `\resumeRoleContext` for role context lines, `\resumeGroupHeading` for
  bold competency groups, and `\resumeTechLine` for per-role technology lines;
  these commands are defined in the template preamble.
- Never use an em dash (`—`) anywhere in generated resume text. Rewrite with commas, parentheses, a colon, a semicolon, or separate sentences.
- Produce editable LaTeX and one to two pages of extractable PDF text.
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
