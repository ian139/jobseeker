# Job scraper UI and job detail experience design

Design a dark, premium "Market Signal Console" interface inspired by Bloomberg Terminal, military command dashboards, and the Gruvbox color palette.

Visual style:

* High-contrast dark theme
* Minimalistic, data-dense layout
* Monospace typography for metrics and labels
* Clean geometric spacing
* Subtle borders and panel separation
* Terminal-like aesthetic with modern polish
* Focus on readability and information hierarchy

Color Palette:

Backgrounds:

* Main Background: #0B0F14
* Panel Background: #11161D
* Elevated Panel: #161C23
* Secondary Surface: #1A212B

Borders:

* Primary Border: #2B3440
* Muted Border: #202832

Typography:

* Primary Text: #EBDBB2
* Secondary Text: #BDAE93
* Muted Text: #928374
* Disabled Text: #665C54

Gruvbox Accent Colors:

* Primary Gold: #D8A657
* Bright Gold: #FABD2F
* Success Green: #A9B665
* Bright Green: #B8BB26
* Aqua: #89B482
* Blue: #7DAEA3
* Orange: #E78A4E
* Red: #EA6962

Component Usage:

Progress Bars:

* Filled: #D8A657
* Hover: #FABD2F
* Empty Track: #1A212B

Positive Metrics:

* Text: #A9B665

Negative Metrics:

* Text: #EA6962

Links:

* Default: #7DAEA3
* Hover: #89B482

Highlighted Cards:

* Background: #EBDBB2
* Text: #11161D

Design Principles:

* Use color sparingly; most UI elements should remain neutral.
* Reserve gold for rankings, importance, and progress.
* Reserve green for positive outcomes and salary metrics.
* Use subtle borders instead of shadows.
* Emphasize whitespace and alignment.
* Create a feeling of a professional intelligence terminal rather than a consumer dashboard.
* The interface should feel like a blend of Bloomberg Terminal, Stripe Dashboard, Linear, and Gruvbox.
* Prefer near-black backgrounds with warm Gruvbox accents.
* Maintain strong accessibility and contrast ratios throughout the design.

Product direction:

* This document captures UI and data-flow requirements only. Do not implement scraper, parser, storage, or report-generation code from this design document unless explicitly requested.
* The first-run experience should guide users to import a resume before tuning search filters. Resume import is the primary action; filters are optional, secondary controls.
* The page should make the full job-scraping workflow understandable at a glance: resume state, scrape status, job list, selected job details, generated prompt/report readiness, and available actions.
* Prefer progressive disclosure over hiding critical actions below the fold. Users should never need to scroll to the bottom of a long page to discover the selected job details or actions.

Layout requirements:

* Use a clear multi-panel layout:
  * Left/control rail: resume import, resume analysis status, optional filters, scrape controls.
  * Center/results panel: scraped job list with searchable/sortable cards and parse status indicators.
  * Right/detail panel: selected job details, generated prompt/report preview, and actions.
* Keep the selected job detail area visible while users browse the job list. The detail panel should be sticky, fixed within the viewport, or otherwise independently accessible without desynchronized scrolling.
* Keep job actions visible near the relevant detail content. Actions such as "Download markdown", "Copy prompt", "Open source", and "Save job" should not require scrolling past all details or reaching the page footer.
* Avoid nested scrolling regions that make the job list, detail content, and action controls feel disconnected. If overflow is unavoidable, show clear headers, persistent action bars, and scroll shadows.
* Use status summaries and compact panels so users can clearly see what is happening: resume parsed, jobs scraped, details stored, prompts generated, report generated, errors needing attention.

Job detail card requirements:

* Job detail cards must display accurate parsed information from the scraped source instead of unknown, placeholder, or default fields.
* Required parsed fields:
  * Job title
  * Company
  * Location or remote/hybrid/on-site classification
  * Compensation, including range, currency, period, and confidence when available
  * Employment type
  * Seniority level
  * Core responsibilities
  * Required qualifications
  * Preferred qualifications
  * Technologies, tools, domains, and keywords
  * Source URL
  * Scraped timestamp
  * Parser confidence and missing-field warnings
* Unknown values should be explicit, rare, and actionable. The UI should distinguish "not present in source", "parser failed", and "not yet parsed" instead of collapsing all cases to "unknown".
* Cards should show a parse-quality indicator so users can trust high-confidence jobs and quickly identify jobs needing review.
* Default values must never be presented as facts. If a value is inferred, mark it as inferred and expose enough context for review.

Resume import and understanding requirements:

* Resume import is the key first-time action and should be visually prioritized above filters and scraping controls.
* The system should parse and understand resume details thoroughly before generating prompts or improvement markdown reports.
* Resume understanding should include:
  * Candidate identity/contact fields when present
  * Current and prior roles
  * Skills, technologies, domains, and seniority signals
  * Project and accomplishment details
  * Quantified impact metrics
  * Education, certifications, awards, and publications
  * Employment preferences or constraints when present
  * Gaps, weak evidence, or ambiguous claims needing clarification
* Generated prompts and improvement markdown reports must be grounded in the parsed resume and selected job details. They should not rely on shallow keyword overlap alone.
* Reports should explain why recommendations were made, which resume evidence supports them, what job requirements they target, and what information is missing.

Scraped job data-flow requirements:

* Store scraped job details as structured records immediately after scraping/parsing so they are quickly accessible for filtering, sorting, detail display, prompt generation, reporting, and future tools.
* Stored records should include raw source content, normalized parsed fields, parser confidence, missing-field diagnostics, timestamps, source metadata, and the resume-match analysis state.
* Filtering should operate on stored structured job details, not by re-scraping or reparsing source pages on every interaction.
* The UI should make storage state visible: newly scraped, parsed, saved, stale, failed, or needs review.
* Detail retrieval should feel immediate after a job appears in the results list. Selecting a job should not trigger a long blocking parse unless the record clearly shows it is pending.

Success criteria:

* A first-time user can identify resume import as the recommended first action within the initial viewport.
* Users can browse jobs and inspect the selected job's details/actions without scrolling to the page bottom or managing disconnected scroll positions.
* No job card presents placeholder/default data as real parsed information.
* Missing job fields are categorized as absent in source, parser failure, or pending parse.
* Scraped job details are available from structured storage for filters, detail cards, reports, and external tools.
* Generated prompts and markdown improvement reports cite or clearly reflect both parsed resume evidence and selected job requirements.
* The page layout communicates the current workflow state without forcing users to infer what is happening from hidden panels or scattered controls.
