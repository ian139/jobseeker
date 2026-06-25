# SCRAPER_UI.md

# Resume Intelligence Workbench

## Current Product Goal

The current goal is to make the scraper, job ranking, resume analysis, and resume generation workflow work extremely well before expanding into application tracking, outreach, CRM, or broader career automation.

This product should first become the best possible local workbench for answering:

> Given my current resume, which jobs should I care about, why do they match or fail, and what resume changes would most improve my chances?

The UI should prioritize the current workflow:

```text
Scrape Jobs
    ↓
Rank Jobs
    ↓
Inspect Match
    ↓
Understand Evidence
    ↓
Identify Missing Requirements
    ↓
Improve Resume
    ↓
Generate Tailored Resume
```

Everything else is future direction. Do not let future application tracking or outreach features make the current scraper/resume experience worse, more confusing, or overbuilt.

---

# Product Framing

This is not just a job scraper UI.

It is a Resume Intelligence Workbench.

The application should feel closer to an IDE, security investigation tool, or observability dashboard than a traditional job board or admin table.

The user should be able to explore jobs, inspect scoring evidence, understand resume gaps, and generate better resumes from one coherent interface.

The core experience should be fast, explainable, and analysis-first.

---

# Design Reference

Use `SCRAPER_DESIGN.md` as the general product and visual design reference.

Apply that design direction specifically to the job scraper frontend, including the local resume-prompt web UI in:

- `scraper/src/job_scraper/web.py`

Do not clone external reference screenshots 1:1.

Borrow the workflow, hierarchy, tab structure, and layout ideas when they improve usability, but preserve the existing product's visual identity.

Keep the existing application's:

- dark theme
- high-contrast cards
- compact technical feel
- badges
- score indicators
- spacing language
- typography direction
- overall brand mood

The redesign should feel like the current app matured into a stronger workstation, not like an unrelated app replaced it.

---

# Design Philosophy

## Explain before expanding

The UI should explain why a job matters before showing raw metadata.

The user should not have to dig through long sections to understand:

- why this job scored highly
- why this job scored poorly
- which resume evidence helped
- which missing requirements hurt
- what action to take next

---

## Analysis first, records second

Avoid making the app feel like a database viewer.

The job list is important, but the main experience should be understanding fit and improving the resume.

A large sortable table can exist as a secondary/debug view, but it should not dominate the primary workflow.

---

## Progressive disclosure

Show summaries first.

Then evidence.

Then detailed scoring factors.

Then raw parser/debug data.

Do not expose every field at once.

The user should be able to drill down without losing their place.

---

## Every score must be inspectable

Any score, badge, warning, or recommendation should be explainable.

A user should be able to answer:

- What contributed to this score?
- What hurt this score?
- What resume evidence was used?
- What requirement caused this gap?
- What could I change to improve it?

---

## Selected object drives the UI

The currently selected job, requirement, resume claim, evidence card, or improvement should drive the surrounding panels.

This should feel like an IDE:

- left side chooses the object
- center explains the object
- right side inspects details about the object

---

# Primary Layout

Use a three-panel desktop layout for the main analysis screen.

```text
+--------------------------------------------------------------------------+
| Left Navigation |              Analysis Workspace              | Inspector |
+--------------------------------------------------------------------------+
```

The layout should make it easy to move from broad job ranking to specific resume evidence.

## Left Panel: Navigator

Purpose:

- choose what to analyze
- move between ranked jobs
- filter job sets
- select saved searches or groups

The left panel should remain visible while navigating.

Possible sections:

- search/filter controls
- ranked jobs
- job categories
- saved searches
- resume/profile selector
- status filters

Each job item should show:

- job title
- company
- overall fit score
- quick badges
- status marker
- small confidence or warning indicator

Avoid dense rows with too many columns.

The left panel should behave like a navigator, not a spreadsheet.

---

## Center Panel: Analysis Workspace

Purpose:

- explain the selected job
- show score breakdown
- show evidence
- show missing requirements
- guide resume improvement
- support resume generation

The center panel is the main work area.

It should be organized around the selected job and the selected analysis tab.

Recommended top-level structure:

1. selected job header
2. summary score cards
3. bottleneck / key insight banner
4. analysis tabs
5. tab content

---

## Right Panel: Inspector

Purpose:

- show contextual details about the selected object
- avoid cluttering the center workspace
- provide metadata, traceability, and debug information when needed

The inspector should update based on what the user selects.

Possible inspected objects:

- selected job
- requirement
- evidence card
- resume claim
- missing requirement
- scoring factor
- generated resume section
- parser/debug output

The inspector should provide context, not duplicate the center panel.

---

# Job Header

At the top of the center workspace, show the selected job clearly.

Include:

- job title
- company
- location/remote status
- source
- scraped date
- current application/resume status if available
- primary score
- important badges

The header should make the selected job unmistakable.

Keep it compact.

---

# Summary Cards

Immediately below the job header, show a compact score summary.

Suggested cards:

- Overall Fit
- Resume Evidence
- Requirement Coverage
- Technical Stack
- Seniority / Leadership
- Confidence

Only include cards that are backed by real data.

Each card should be clickable or inspectable.

When selected, the right inspector should explain how the score was produced.

---

# Bottleneck Banner

Below the summary cards, show the most important reason the score is not higher.

Examples:

- Missing distributed systems leadership evidence
- Weak cloud infrastructure match
- Strong Python match but limited production ML evidence
- Good seniority match but missing domain-specific keywords
- Resume has relevant experience but lacks quantified impact

The bottleneck banner should answer:

> What is the single highest-impact thing to fix first?

This is one of the most important UI elements.

---

# Analysis Tabs

Use tabs to switch analytical perspectives without losing the selected job.

Recommended tabs for v1:

1. Overview
2. Evidence
3. Missing Requirements
4. Resume Claims
5. Ranking Factors
6. Resume Improvements
7. Resume Generator
8. Raw / Debug

Tabs should not feel like unrelated pages.

Switching tabs should preserve the selected job and surrounding context.

---

# Tab: Overview

Purpose:

Give the fastest possible understanding of the selected job.

Show:

- concise fit summary
- best matching resume evidence
- biggest gaps
- top recommended resume improvement
- generate resume / prepare resume action if available

This tab should answer:

> Should I care about this job, and what should I do next?

The overview should not be long.

It should link into deeper tabs for evidence, missing requirements, and improvements.

---

# Tab: Evidence

Purpose:

Explain why the score exists.

Display evidence cards ordered by contribution.

Each evidence card should include:

- matched requirement
- resume excerpt or parsed claim
- contribution score
- confidence
- matched keywords
- short explanation
- link to inspect details

Cards should make the scoring engine feel transparent.

Good evidence cards should be easy to scan.

Avoid dumping raw JSON or long parser output here.

---

# Tab: Missing Requirements

Purpose:

Show what is preventing a better match.

Group missing or weakly satisfied requirements by category.

Example categories:

- Core Technical Skills
- Frameworks / Tools
- Cloud / Infrastructure
- Architecture
- Leadership
- Domain Experience
- Communication
- Testing
- Data / ML
- Security

Each missing item should show:

- requirement text
- why it matters
- where it appeared in the job
- estimated score impact
- whether the resume may already contain related evidence
- suggested resume improvement

Avoid generic "missing skills" lists.

Every item should be actionable or explainable.

---

# Tab: Resume Claims

Purpose:

Let the user inspect how the resume was parsed and understood.

Every important resume claim should be inspectable.

A claim may represent:

- skill
- project
- job responsibility
- accomplishment
- metric
- leadership experience
- domain experience
- tool/framework usage

Each claim should show:

- extracted text
- normalized category
- confidence
- source section
- matched requirements
- matched jobs
- related evidence cards
- improvement opportunities

Think of resume claims as reusable knowledge nodes.

This tab is important because resume generation later depends on trustworthy parsed claims.

---

# Tab: Ranking Factors

Purpose:

Expose the scoring engine.

Show scoring factors grouped into:

- positive factors
- negative factors
- neutral observations
- confidence warnings

Each factor should show:

- category
- weight
- contribution
- explanation
- linked evidence or requirement

This tab is for trust and debugging.

It should be readable by a normal user but detailed enough to debug bad rankings.

---

# Tab: Resume Improvements

Purpose:

Tell the user what to improve next.

Group recommendations by actionability and expected impact.

Recommended groups:

- Highest Impact
- Quick Wins
- Missing Evidence
- Missing Keywords
- Needs Quantification
- Project Suggestions
- Rewrite Suggestions

Every recommendation should include:

- specific change
- reason
- linked job requirement
- linked resume evidence or gap
- estimated impact
- suggested wording when possible

This tab should answer:

> What should I edit in my resume before applying?

---

# Tab: Resume Generator

Purpose:

Support the current resume-generation workflow.

This tab should eventually connect clearly to the CLI workflow:

```bash
cp config/resume-profile.example.yaml config/resume-profile.yaml
job-scraper generate-resume --job-id <ID> --profile config/resume-profile.yaml --no-llm
```

The UI should eventually support:

- selected resume profile
- profile completeness status
- generated resume preview
- generated resume sections
- job-specific tailoring summary
- export/download action
- no-LLM deterministic generation mode indicator
- warnings for missing profile fields

This tab does not need to become a full resume editor immediately.

For v1, prioritize making the workflow understandable and reliable.

---

# Tab: Raw / Debug

Purpose:

Expose raw details without polluting the main UX.

This tab may include:

- scraped job text
- parsed requirements
- raw score data
- parser diagnostics
- resume profile data
- CLI command equivalents
- logs or errors

This tab is for debugging and power users.

It should not be the primary user experience.

---

# Job Navigation

The job list should make scanning and prioritization easy.

Prefer ranked cards or compact list rows over a giant table.

Each job row/card should include:

- title
- company
- score
- status
- top reason
- key badges
- warning indicator if applicable

Possible badges:

- Remote
- Hybrid
- Senior
- Strong Python Match
- Weak Cloud Match
- Resume Ready
- Needs Tailoring
- High Confidence
- Low Confidence

Selecting a job should update the center workspace and inspector.

Avoid forcing the user into separate detail pages unless there is a clear reason.

---

# Resume Workflow

The scraper and resume workflow should feel connected.

The user should be able to move naturally from:

```text
Job
    ↓
Fit Analysis
    ↓
Resume Gaps
    ↓
Resume Improvements
    ↓
Generated Resume
```

The resume generation experience should explain:

- which job is being targeted
- which resume profile is being used
- which requirements influenced the output
- which resume claims were selected
- what changed compared to the base profile
- whether the output was generated deterministically or with LLM assistance

The UI should make it hard to accidentally generate a resume for the wrong job or wrong profile.

---

# Current CLI Workflows To Support

The UI should respect the current command-line workflows and eventually surface them clearly.

## Resume Profile Setup

```bash
cp config/resume-profile.example.yaml config/resume-profile.yaml
```

The UI should eventually help users understand whether `config/resume-profile.yaml` is complete.

Important profile areas:

- contact info
- skills
- experience
- projects
- bullet metadata
- education
- links

## Generate Resume

```bash
job-scraper generate-resume --job-id <ID> --profile config/resume-profile.yaml --no-llm
```

The UI should eventually support generating a targeted resume from a selected job.

## Prepare Application Pack

```bash
job-scraper prepare-application --job-id <ID> --profile config/resume-profile.yaml --no-llm
```

This is future-facing but should not dominate v1.

For now, make sure the architecture does not prevent this from becoming a first-class workflow later.

## Track Application State

```bash
job-scraper list-applications
job-scraper update-application
```

Application tracking is future direction.

Do not build the primary scraper/resume UI around application tracking yet.

---

# Component Architecture

Prefer composable components over one large view.

Suggested structure:

```text
App

Layout
    WorkbenchShell
    Sidebar
    Workspace
    Inspector

Navigation
    JobNavigator
    JobFilters
    SavedSearchList
    ResumeProfileSelector

Job Analysis
    JobHeader
    SummaryCards
    BottleneckBanner
    AnalysisTabs
    OverviewTab
    EvidenceTab
    MissingRequirementsTab
    ResumeClaimsTab
    RankingFactorsTab
    ResumeImprovementsTab
    ResumeGeneratorTab
    RawDebugTab

Cards
    JobListItem
    EvidenceCard
    MissingRequirementCard
    ResumeClaimCard
    RankingFactorCard
    ImprovementCard

Inspector
    JobInspector
    RequirementInspector
    EvidenceInspector
    ResumeClaimInspector
    ScoreInspector
    DebugInspector

Shared
    ScoreBadge
    ConfidenceBadge
    StatusBadge
    KeywordChip
    ScoreBar
    EmptyState
    LoadingState
    ErrorState
```

The exact names can vary based on implementation language, but the separation should remain.

---

# State Model

The UI should have clear centralized state.

Important state:

- selected job
- selected tab
- selected evidence item
- selected requirement
- selected resume claim
- selected resume profile
- active filters
- sort mode
- search query
- debug visibility

Changing the selected job should update all dependent panels.

Changing the selected tab should not reset the selected job.

Changing the selected evidence/claim should update the inspector.

---

# Empty States

Empty states should be useful.

Examples:

## No jobs scraped

Explain how to scrape or import jobs.

## No resume profile

Explain how to copy and fill `config/resume-profile.yaml`.

## No evidence found

Explain whether this means poor fit, missing parser data, or unavailable resume profile.

## No missing requirements

Explain whether the match is strong or the parser lacks requirement data.

## Resume generation unavailable

Explain which profile fields or dependencies are missing.

Do not show blank panels.

---

# Error States

Errors should be actionable.

For CLI-backed workflows, show:

- command attempted
- short error summary
- likely cause
- suggested fix
- link to raw output in debug tab

Do not bury important errors in logs.

---

# Information Hierarchy

Default priority:

1. Which jobs are worth attention?
2. Why does this job fit or not fit?
3. Which resume evidence caused the score?
4. What is missing?
5. What resume change should happen next?
6. Can I generate a tailored resume?
7. What raw/debug data supports this?

Raw metadata should come last.

---

# UX Rules

- Always explain scores.
- Prefer cards over dense tables.
- Prefer tabs over long vertical pages.
- Prefer progressive disclosure over showing everything.
- Keep the selected job visible.
- Keep the most important action visible.
- Keep evidence linked to scores.
- Keep recommendations linked to evidence.
- Avoid modal-heavy workflows.
- Avoid giant raw-data sections in primary tabs.
- Avoid making users scroll through metadata before seeing value.
- Avoid hiding errors.
- Avoid silently failing generation workflows.

---

# Performance Expectations

The UI should be designed for many jobs.

Assume the user may eventually have thousands of scraped jobs.

The interface should support:

- fast filtering
- fast selection
- lazy detail loading
- stable layout
- clear loading states
- minimal full-page refreshes

Do not design around only a tiny demo dataset.

---

# Accessibility and Keyboard Use

The workbench should be usable with keyboard navigation over time.

Important interactions should eventually support:

- moving through job list
- switching tabs
- opening inspector details
- searching
- triggering generation commands
- copying command equivalents or output paths

Accessibility does not need to be perfect in the first implementation, but the design should not make it impossible.

---

# Out of Scope For Current v1

The following are future direction and should not distract from making scraper/resume analysis excellent first:

- full outreach CRM
- contact import UI
- outreach queue UI
- recruiter relationship tracking
- full application pipeline UI
- analytics dashboards across applications
- interview preparation workspaces
- automatic external submissions
- email or LinkedIn automation

These should be considered future workspaces, not current primary navigation.

---

# Future Direction

The architecture should eventually support the broader job-search lifecycle.

Future workflows may include:

## Application Packs

CLI workflow:

```bash
job-scraper prepare-application --job-id <ID> --profile config/resume-profile.yaml --no-llm
```

Potential UI:

- package preview
- generated files
- checklist
- application notes
- export options

## Application Tracking

CLI workflow:

```bash
job-scraper list-applications
job-scraper update-application
```

Potential UI:

- application status board
- saved/applied/interviewing/rejected states
- notes
- history

## Outreach

CLI workflow:

```bash
cp config/outreach.example.yaml config/outreach.yaml
job-scraper outreach init
job-scraper outreach import-contacts --csv config/contacts.csv
job-scraper outreach queue --config config/outreach.yaml
job-scraper outreach next --limit 5 --open
job-scraper outreach mark
job-scraper outreach mark-contact
```

Potential UI:

- contact list
- due actions
- outreach queue
- outcome tracking
- follow-up reminders

These future workflows should not drive the current UI, but the current architecture should avoid blocking them.

---

# Implementation Guidance For Agents

When implementing UI changes:

1. Start from this document and `SCRAPER_DESIGN.md`.
2. Preserve the existing product's visual identity.
3. Do not clone reference screenshots exactly.
4. Improve information architecture first.
5. Keep changes scoped.
6. Prefer component extraction when a file becomes too large.
7. Add focused tests where practical.
8. Do not break existing scraper/resume commands.
9. Keep raw/debug data accessible but secondary.
10. Verify the user can still complete the core flow.

The most important current outcome is a polished, understandable scraper/resume analysis workflow.

Build that first.
