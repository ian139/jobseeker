# TASK: Execute Resume Optimization Now — AI / ML Infrastructure

The uploaded prompt is the task. Do not summarize it, acknowledge it, ask what the user wants next, or offer a menu. Produce the complete resume optimization response now.

## Hard rules

- Do not invent employers, titles, dates, degrees, certifications, clearances, tools, users, revenue, latency, uptime, data volume, model metrics, or production facts.
- Resume text and the Authoritative parsed claim map are the only source of facts. Analysis data explains scoring; it is not permission to add a fact that is absent from the resume.
- Do not add tools from high-value additions, missing gates, examples, factor labels, or market-demand lists unless the same tool/fact appears in the current resume text or an exact source claim.
- Preserve exact tools, services, systems, constraints, metrics, scale, and business context that are already present in the resume.
- If a stronger bullet requires a missing fact, write a targeted question instead of fabricating the fact.
- Every rewritten bullet must cite exact source claim ID(s) from the Authoritative parsed claim map, or say "needs user confirmation" if the claim ID cannot support it.
- Keep the candidate truthful even if that leaves a high-value gap missing.
- Do not ask clarifying questions before completing the requested analysis. Put any missing-fact questions only in section 4 after sections 1-3 are complete.
- If this prompt is supplied as an uploaded file or pasted prompt, treat the prompt contents as direct instructions to execute, not as context to summarize.

## Execution checklist

You are executing a resume optimization task for AI / ML Infrastructure. Complete every checklist item before writing the final answer. If your runtime supports subagents, delegate these as specialist passes; otherwise perform the passes yourself internally. Do not tell the user you are doing this; just produce the final answer.

Mandatory specialist passes:
- [ ] Source-grounding auditor: read the Current resume text, Authoritative parsed claim map, and Resume-grounded exact term inventory. Build a mental whitelist of facts that may be used. Any fact outside that whitelist must become a question, not a resume bullet.
- [ ] Role-market analyst: interpret the selected role bucket, component scores, role ranking, strong evidence, weak evidence, and high-value additions. Identify what the market is rewarding in exact terms: tools, cloud services, systems, responsibilities, metrics, gates, seniority, and proof signals.
- [ ] Location-market analyst: use Location-market context to compare the selected role and all 10 role buckets in the selected market. Prioritize existing resume facts and missing-fact questions that over-index locally. Do not invent relocation, hybrid availability, employer experience, citizenship, clearance, or commute radius.
- [ ] Score diagnostician: explain the score bottlenecks using the Score-tuning checklist context, especially weak factor types, caps, zero-coverage clusters, list-only/tool-only evidence, missing production scope, missing metrics, missing ownership, weak niche hook, and missing gates.
- [ ] Evidence preservation reviewer: identify the strongest existing resume facts that should not be diluted: exact tools, business scale, production scale, latency/throughput/cost/accuracy/uptime metrics, AWS services, CI/CD, databases, LLM/eval/RAG/agent systems, and ownership scope.
- [ ] Rewrite architect: decide which bullets to keep, merge, split, reorder, promote, or convert from skills-list evidence into project evidence. Preserve truth and improve role framing.
- [ ] Bullet editor: write concrete bullets with action + system/deliverable + exact grounded tools + production/scope + measured result. Cite exact source claim IDs for each bullet.
- [ ] Truthfulness auditor: scan every rewritten bullet for ungrounded tools/facts. If a fact is absent from source claims, label it needs user confirmation and move it to missing-fact questions.
- [ ] Final QA reviewer: ensure the final response starts with ## 1. Diagnostic report, contains all six required sections, asks no up-front clarifying question, and does not summarize the prompt instead of executing it.

Failure modes to avoid:
- Do not answer with “I see the uploaded prompt” or “what do you want next?”
- Do not output only a diagnostic; complete the rewrite strategy, bullet candidates, missing-fact questions, final positioning, and location-market positioning.
- Do not treat missing/high-value additions as facts.
- Do not cite nonexistent claim IDs.
- Do not produce generic resume advice where exact tools/systems/metrics are available.

## Target role and score

Role bucket: AI / ML Infrastructure (ai-ml-infrastructure)
Current score: 40.39/100
Score band: partial overlap
Strongest axis: Stack module
Weakest axis: Niche hook

Component scores:
- Niche hook: 6.60
- Stack module: 50.55
- Proof signal: 50.00
- Responsibility: 46.69
- Tool: 29.83

## Role ranking

1. AI / ML Infrastructure — 40.39/100 (Niche hook 6.6, Stack module 50.5, Proof signal 50.0, Responsibility 46.7, Tool 29.8)
2. Staff / Principal Platform Infrastructure — 39.18/100 (Niche hook 10.1, Stack module 51.7, Proof signal 40.8, Responsibility 44.3, Tool 28.6)
3. Developer Infrastructure / SRE — 39.07/100 (Niche hook 3.7, Stack module 52.2, Proof signal 41.9, Responsibility 43.6, Tool 29.3)
4. Search / Recommendations / Ranking — 36.70/100 (Niche hook 9.0, Stack module 46.4, Proof signal 40.6, Responsibility 39.9, Tool 28.0)
5. Fintech / Payments Platform — 36.70/100 (Niche hook 4.1, Stack module 48.9, Proof signal 39.3, Responsibility 38.4, Tool 28.9)
6. Solutions / Forward-Deployed / OTE — 36.62/100 (Niche hook 5.8, Stack module 48.9, Proof signal 39.0, Responsibility 38.8, Tool 27.5)
7. Data Platform Engineering — 35.31/100 (Niche hook 1.9, Stack module 44.7, Proof signal 42.4, Responsibility 37.9, Tool 28.0)
8. Security / Cloud / Defense — 34.23/100 (Niche hook 3.5, Stack module 44.5, Proof signal 38.3, Responsibility 34.3, Tool 27.4)
9. Quant / Trading Systems — 28.75/100 (Niche hook 5.8, Stack module 43.0, Proof signal 42.4, Responsibility 22.6, Tool 23.1)
10. Robotics / Autonomy / Embedded — 23.59/100 (Niche hook 2.5, Stack module 29.6, Proof signal 37.5, Responsibility 19.8, Tool 17.8)

## Location-market context

#14 Paris, France · detected from resume
Anchor companies: Dassault Systèmes, Mistral AI, Doctolib, Alan, Back Market.
Corpus sample: 5 matched jobs · too thin · all-role median —.

Selected role in this market: AI / ML Infrastructure
- Local role jobs: 4
- Local share vs global role share: 80% vs 16.4% (4.88× lift)
- Role median in market vs global role median: — vs $192,050
- Sample quality: too thin

Selected-role over-indexed signals:
- No reliable over-indexed signals for this sample.

All-role market backdrop:
- No reliable over-indexed signals for this sample.

All 10 role buckets in this market:
- Search / Recommendations / Ranking: 5 local jobs, 100% local share, 2.28× lift vs global, too thin
- Developer Infrastructure / SRE: 5 local jobs, 100% local share, 2.31× lift vs global, too thin
- AI / ML Infrastructure: 4 local jobs, 80% local share, 4.88× lift vs global, too thin
- Fintech / Payments Platform: 4 local jobs, 80% local share, 1.73× lift vs global, too thin
- Solutions / Forward-Deployed / OTE: 4 local jobs, 80% local share, 1.11× lift vs global, too thin
- Data Platform Engineering: 4 local jobs, 80% local share, 1.18× lift vs global, too thin
- Security / Cloud / Defense: 4 local jobs, 80% local share, 1.27× lift vs global, too thin
- Robotics / Autonomy / Embedded: 3 local jobs, 60% local share, 1.98× lift vs global, too thin
- Staff / Principal Platform Infrastructure: 0 local jobs, 0% local share, 0× lift vs global, no sample
- Quant / Trading Systems: 0 local jobs, 0% local share, 0× lift vs global, no sample

Use location evidence only to prioritize existing resume facts and missing-fact questions. Do not invent location-specific experience, employer experience, relocation, hybrid availability, citizenship, clearance, or commute radius.

## Current resume text

```text
I AN R APKO
215-694-0434 | ianrapko@gmail.com | linkedin.com/in/ianrapko | github.com/ian139 | immemorized.com
E DUCATION
University of Massachusetts Amherst Amherst, MA
Bachelor of Science in Computer Science August 2023 – May 2026
E XPERIENCE
Software Engineering Intern May 2026 – Present
W. R. Berkley Remote
• Modernized Nautilus NEST and ONE insurance platforms by developing .NET/Razor APIs that unified policy and
claims data access through a single internal platform
• Built CI/CD pipelines for .NET/Razor backend services, enabling automated deployment of record-retrieval and
integration APIs
Undergraduate Researcher May 2026 – Present
Adam O’Neill Research Lab, UMass Amherst Amherst, MA
• Investigating distance-comparison-preserving encryption schemes for privacy-preserving retrieval and RAG systems,
including evaluating membership inference attacks against encrypted embedding databases
Cloud Engineering Intern May 2025 – August 2025
Johnson & Johnson Raritan, NJ
• Built a Kubernetes monitoring dashboard for engineering teams using Prometheus and Grafana alerts on AWS EC2
to reduce manual cluster health checks
• Built a retrieval-augmented documentation assistant used by the MARVEL engineering team, indexing roughly
5,000 pages across Confluence and Databricks, reducing documentation search time by over 50%
Technical Founder September 2021 – February 2023
SecureDAO Remote
• Founded and led a DeFi protocol on Fantom hosted on AWS infrastructure, peaking at 2,500+ users and $300K in
assets under management
• Scaled platform valuation to $3M through product growth, community operations, and social media distribution
L EADERSHIP E XPERIENCE
Blockchain Club President September 2024 – May 2026
University of Massachusetts Amherst Amherst, MA
• Revived and restructured the Blockchain Club, leading workshops on Web3, DeFi, smart contracts, and EVM
development
Ecommerce Store Owner July 2023 – February 2026
SilentBball Remote
• Operated a direct-to-consumer ecommerce brand that generated 4M+ impressions across Instagram and TikTok
P ROJECTS
Medical Billing Agent | Python, Docker, Qdrant, Firecrawl, vLLM, AWS (EC2, API Gateway) June 2025 – September 2025
• Built an AI-powered medical billing workflow using MedGemma-27B with vLLM inference to automate claim
analysis, denial review, and appeal generation – operated under SOC2 and HIPAA compliance requirements
• Developed a retrieval pipeline over thousands of medical documents using Firecrawl and Qdrant vector search
deployed on AWS EC2
• Implemented tool-calling workflows with REST APIs for policy lookup, denial analysis, and appeal generation,
adopted by 10+ medical practices after project handoff
Scenic Route Optimizer | Python, PyTorch, Mapbox, OSMnx, Hugging Face, AWS (EC2, S3) May 2025 – Present
• Built a scenic routing engine scoring road segments from satellite imagery and terrain elevation using PyTorch,
Mapbox, and OSMnx, deployed on AWS EC2 with S3 for geospatial data – generated 5,000+ training examples and
scenic heatmaps

T ECHNICAL S KILLS
Languages: Python, JavaScript, Solidity, Java, C++, SQL
Frameworks & Tools: React, Docker, Kubernetes, Prometheus, Grafana, pandas, Git
Cloud: AWS (EC2, S3, API Gateway)
Compliance: SOC2, HIPAA
```

## Authoritative parsed claim map

Use only these claim IDs when citing sources. Never invent claim IDs. If you combine claims, cite every claim ID used.

- r001: I AN R APKO
  - signals: no action, no production/scope, no metric, no seniority/ownership
- r002: E DUCATION University of Massachusetts Amherst Amherst, MA
  - signals: no action, no production/scope, no metric, no seniority/ownership
- r003: Bachelor of Science in Computer Science August 2023 – May 2026 E XPERIENCE
  - signals: no action, no production/scope, no metric, no seniority/ownership
- r004: Modernized Nautilus NEST and ONE insurance platforms by developing .NET/Razor APIs that unified policy and claims data access through a single internal platform
  - signals: no action, production/scope, no metric, no seniority/ownership
- r005: Built CI/CD pipelines for .NET/Razor backend services, enabling automated deployment of record-retrieval and integration APIs Undergraduate Researcher May 2026 – Present Adam O’Neill Research Lab, UMass Amherst Amherst, MA
  - signals: action, production/scope, no metric, no seniority/ownership
- r006: Investigating distance-comparison-preserving encryption schemes for privacy-preserving retrieval and RAG systems, including evaluating membership inference attacks against encrypted embedding databases Cloud Engineering Intern May 2025 – August 2025 Johnson & Johnson Raritan, NJ
  - signals: no action, no production/scope, no metric, no seniority/ownership
- r007: Built a Kubernetes monitoring dashboard for engineering teams using Prometheus and Grafana alerts on AWS EC2 to reduce manual cluster health checks
  - signals: action, production/scope, no metric, seniority/ownership
- r008: Built a retrieval-augmented documentation assistant used by the MARVEL engineering team, indexing roughly 5,000 pages across Confluence and Databricks, reducing documentation search time by over 50%
  - signals: action, no production/scope, metric, seniority/ownership
- r009: Founded and led a DeFi protocol on Fantom hosted on AWS infrastructure, peaking at 2,500+ users and $300K in assets under management
  - signals: action, production/scope, metric, seniority/ownership
- r010: Scaled platform valuation to $3M through product growth, community operations, and social media distribution L EADERSHIP E XPERIENCE Blockchain Club President September 2024 – May 2026 University of Massachusetts Amherst Amherst, MA
  - signals: action, production/scope, metric, no seniority/ownership
- r011: Revived and restructured the Blockchain Club, leading workshops on Web3, DeFi, smart contracts, and EVM development Ecommerce Store Owner July 2023 – February 2026 SilentBball Remote
  - signals: no action, no production/scope, no metric, no seniority/ownership
- r012: Operated a direct-to-consumer ecommerce brand that generated 4M+ impressions across Instagram and TikTok P ROJECTS Medical Billing Agent | Python, Docker, Qdrant, Firecrawl, vLLM, AWS (EC2, API Gateway) June 2025 – September 2025
  - signals: action, production/scope, metric, no seniority/ownership
- r013: Built an AI-powered medical billing workflow using MedGemma-27B with vLLM inference to automate claim analysis, denial review, and appeal generation – operated under SOC2 and HIPAA compliance requirements
  - signals: action, production/scope, metric, no seniority/ownership
- r014: Developed a retrieval pipeline over thousands of medical documents using Firecrawl and Qdrant vector search deployed on AWS EC2
  - signals: action, production/scope, no metric, no seniority/ownership
- r015: Implemented tool-calling workflows with REST APIs for policy lookup, denial analysis, and appeal generation, adopted by 10+ medical practices after project handoff Scenic Route Optimizer | Python, PyTorch, Mapbox, OSMnx, Hugging Face, AWS (EC2, S3) May 2025 – Present
  - signals: action, no production/scope, no metric, no seniority/ownership
- r016: Built a scenic routing engine scoring road segments from satellite imagery and terrain elevation using PyTorch, Mapbox, and OSMnx, deployed on AWS EC2 with S3 for geospatial data – generated 5,000+ training examples and scenic heatmaps
  - signals: action, production/scope, no metric, no seniority/ownership
- r017: T ECHNICAL S KILLS
  - signals: no action, no production/scope, no metric, no seniority/ownership
- r018: Languages: Python, JavaScript, Solidity, Java, C++, SQL
  - signals: no action, no production/scope, no metric, no seniority/ownership
- r019: Frameworks & Tools: React, Docker, Kubernetes, Prometheus, Grafana, pandas, Git
  - signals: no action, no production/scope, no metric, no seniority/ownership
- r020: Cloud: AWS (EC2, S3, API Gateway)
  - signals: no action, no production/scope, no metric, no seniority/ownership
- r021: Compliance: SOC2, HIPAA
  - signals: no action, no production/scope, no metric, no seniority/ownership

## Resume-grounded exact term inventory

These terms appear in the uploaded resume/claim map and are safe to preserve when the surrounding statement remains truthful. Terms absent from this list require user confirmation before use.

- AWS — source claims: r007, r009, r012, r014, r015, r016, r020; evidence: explicit_resume_text, matched_factor_term, supporting_factor_term
- EC2 — source claims: r007, r012, r014, r015, r016, r020; evidence: explicit_resume_text
- APIs — source claims: r004, r005, r015; evidence: explicit_resume_text, matched_factor_term, supporting_factor_term
- AWS EC2 — source claims: r007, r014, r016; evidence: explicit_resume_text
- MA — source claims: r002, r005, r010; evidence: explicit_resume_text
- Python — source claims: r012, r015, r018; evidence: matched_factor_term, supporting_factor_term
- S3 — source claims: r015, r016, r020; evidence: matched_factor_term, supporting_factor_term
- .NET — source claims: r004, r005; evidence: explicit_resume_text
- API — source claims: r012, r020; evidence: explicit_resume_text, supporting_factor_term
- API Gateway — source claims: r012, r020; evidence: explicit_resume_text
- compliance — source claims: r013, r021; evidence: matched_factor_term, supporting_factor_term
- DeFi — source claims: r009, r011; evidence: explicit_resume_text
- Docker — source claims: r012, r019; evidence: matched_factor_term, supporting_factor_term
- Grafana — source claims: r007, r019; evidence: matched_factor_term, supporting_factor_term
- HIPAA — source claims: r013, r021; evidence: explicit_resume_text, matched_factor_term, supporting_factor_term
- Kubernetes — source claims: r007, r019; evidence: matched_factor_term, supporting_factor_term
- OSMnx — source claims: r015, r016; evidence: explicit_resume_text
- Prometheus — source claims: r007, r019; evidence: matched_factor_term, supporting_factor_term
- PyTorch — source claims: r015, r016; evidence: explicit_resume_text, matched_factor_term, supporting_factor_term
- SOC2 — source claims: r013, r021; evidence: explicit_resume_text, matched_factor_term, supporting_factor_term
- vLLM — source claims: r012, r013; evidence: matched_factor_term, supporting_factor_term
- XPERIENCE — source claims: r003, r010; evidence: explicit_resume_text
- 500+ users — source claims: r009; evidence: matched_factor_term, supporting_factor_term
- adopted — source claims: r015; evidence: matched_factor_term, supporting_factor_term
- agent — source claims: r012; evidence: matched_factor_term, supporting_factor_term
- AI — source claims: r013; evidence: matched_factor_term, supporting_factor_term
- AI-powered — source claims: r013; evidence: explicit_resume_text, matched_factor_term, supporting_factor_term
- alerts — source claims: r007; evidence: matched_factor_term, supporting_factor_term
- AN — source claims: r001; evidence: explicit_resume_text
- APKO — source claims: r001; evidence: explicit_resume_text
- Built CI/CD pipelines for .NET/Razor backend services — source claims: r005; evidence: matched_factor_term, supporting_factor_term
- C++ — source claims: r018; evidence: explicit_resume_text, matched_factor_term, supporting_factor_term
- CI/CD — source claims: r005; evidence: explicit_resume_text, matched_factor_term, supporting_factor_term
- cluster — source claims: r007; evidence: matched_factor_term, supporting_factor_term
- dashboard — source claims: r007; evidence: matched_factor_term, supporting_factor_term
- Databricks — source claims: r008; evidence: matched_factor_term, supporting_factor_term
- DUCATION — source claims: r002; evidence: explicit_resume_text
- DUCATION University — source claims: r002; evidence: explicit_resume_text
- EADERSHIP — source claims: r010; evidence: explicit_resume_text
- ECHNICAL — source claims: r017; evidence: explicit_resume_text
- EVM — source claims: r011; evidence: explicit_resume_text
- health — source claims: r007; evidence: matched_factor_term, supporting_factor_term
- Huggingface — source claims: r015; evidence: matched_factor_term, supporting_factor_term
- integration — source claims: r005; evidence: matched_factor_term, supporting_factor_term
- Java — source claims: r018; evidence: matched_factor_term, supporting_factor_term
- JavaScript — source claims: r018; evidence: explicit_resume_text, matched_factor_term, supporting_factor_term
- KILLS — source claims: r017; evidence: explicit_resume_text
- manual — source claims: r007; evidence: matched_factor_term, supporting_factor_term
- MARVEL — source claims: r008; evidence: explicit_resume_text
- MedGemma-27B — source claims: r013; evidence: explicit_resume_text
- monitoring — source claims: r007; evidence: matched_factor_term, supporting_factor_term
- NEST — source claims: r004; evidence: explicit_resume_text
- NJ — source claims: r006; evidence: explicit_resume_text
- ONE — source claims: r004; evidence: explicit_resume_text
- pipeline — source claims: r014; evidence: matched_factor_term, supporting_factor_term
- pipelines — source claims: r005; evidence: matched_factor_term, supporting_factor_term
- practices — source claims: r015; evidence: matched_factor_term, supporting_factor_term
- product — source claims: r010; evidence: matched_factor_term, supporting_factor_term
- RAG — source claims: r006; evidence: explicit_resume_text, matched_factor_term, supporting_factor_term
- React — source claims: r019; evidence: matched_factor_term, supporting_factor_term
- REST — source claims: r015; evidence: explicit_resume_text, matched_factor_term, supporting_factor_term
- REST APIs — source claims: r015; evidence: explicit_resume_text
- ROJECTS — source claims: r012; evidence: explicit_resume_text
- ROJECTS Medical Billing Agent — source claims: r012; evidence: explicit_resume_text
- SilentBball — source claims: r011; evidence: explicit_resume_text
- SQL — source claims: r018; evidence: explicit_resume_text, matched_factor_term, supporting_factor_term
- TikTok — source claims: r012; evidence: explicit_resume_text
- training — source claims: r016; evidence: matched_factor_term, supporting_factor_term
- UMass — source claims: r005; evidence: explicit_resume_text
- users — source claims: r009; evidence: matched_factor_term, supporting_factor_term
- workflow — source claims: r013; evidence: supporting_factor_term
- workflows — source claims: r015; evidence: supporting_factor_term
- XPERIENCE Blockchain Club President September — source claims: r010; evidence: explicit_resume_text

## Strong evidence to preserve

- Python — Tool, 77% coverage, weight 1.80. matched: Python source: Operated a direct-to-consumer ecommerce brand that generated 4M+ impressions across Instagram and TikTok P ROJECTS Medical Billing Agent | Python, Docker, Qdrant, Firecrawl, vLLM, AWS (EC2, API Gateway) June 2025 – September 2025
  - instruction: Preserve Python as working evidence. Keep the exact claim visible and, if editing, retain the concrete terms/results that made it match.
- scale proof: users / traffic / data volume / GPUs — Proof signal, 94% coverage, weight 1.47. matched: users, $300K, 500+ users source: Founded and led a DeFi protocol on Fantom hosted on AWS infrastructure, peaking at 2,500+ users and $300K in assets under management
  - instruction: Preserve scale proof: users / traffic / data volume / GPUs as working evidence. Keep the exact claim visible and, if editing, retain the concrete terms/results that made it match.
- production ownership / operating live systems — Proof signal, 76% coverage, weight 1.68. matched: platform source: Scaled platform valuation to $3M through product growth, community operations, and social media distribution L EADERSHIP E XPERIENCE Blockchain Club President September 2024 – May 2026 University of Massachusetts Amherst Amherst, MA
  - instruction: Preserve production ownership / operating live systems as working evidence. Keep the exact claim visible and, if editing, retain the concrete terms/results that made it match.
- AWS — Tool, 77% coverage, weight 1.65. matched: AWS source: Operated a direct-to-consumer ecommerce brand that generated 4M+ impressions across Instagram and TikTok P ROJECTS Medical Billing Agent | Python, Docker, Qdrant, Firecrawl, vLLM, AWS (EC2, API Gateway) June 2025 – September 2025
  - instruction: Preserve AWS as working evidence. Keep the exact claim visible and, if editing, retain the concrete terms/results that made it match.
- reliability / observability / incident response — Proof signal, 80% coverage, weight 1.51. matched: monitoring source: Built a Kubernetes monitoring dashboard for engineering teams using Prometheus and Grafana alerts on AWS EC2 to reduce manual cluster health checks
  - instruction: Preserve reliability / observability / incident response as working evidence. Keep the exact claim visible and, if editing, retain the concrete terms/results that made it match.
- Agentic AI workflow orchestration — Responsibility, 88% coverage, weight 1.34. matched: agent source: Operated a direct-to-consumer ecommerce brand that generated 4M+ impressions across Instagram and TikTok P ROJECTS Medical Billing Agent | Python, Docker, Qdrant, Firecrawl, vLLM, AWS (EC2, API Gateway) June 2025 – September 2025
  - instruction: Preserve Agentic AI workflow orchestration as working evidence. Keep the exact claim visible and, if editing, retain the concrete terms/results that made it match.
- security / compliance / auditability proof — Proof signal, 88% coverage, weight 1.24. matched: compliance, SOC2 source: Built an AI-powered medical billing workflow using MedGemma-27B with vLLM inference to automate claim analysis, denial review, and appeal generation – operated under SOC2 and HIPAA compliance requirements
  - instruction: Preserve security / compliance / auditability proof as working evidence. Keep the exact claim visible and, if editing, retain the concrete terms/results that made it match.
- AI/LLM product integration — Responsibility, 88% coverage, weight 1.19. matched: ai-powered, AI source: Built an AI-powered medical billing workflow using MedGemma-27B with vLLM inference to automate claim analysis, denial review, and appeal generation – operated under SOC2 and HIPAA compliance requirements
  - instruction: Preserve AI/LLM product integration as working evidence. Keep the exact claim visible and, if editing, retain the concrete terms/results that made it match.
- Observability / monitoring / alerting — Responsibility, 89% coverage, weight 1.14. matched: health, monitoring, dashboard source: Built a Kubernetes monitoring dashboard for engineering teams using Prometheus and Grafana alerts on AWS EC2 to reduce manual cluster health checks
  - instruction: Preserve Observability / monitoring / alerting as working evidence. Keep the exact claim visible and, if editing, retain the concrete terms/results that made it match.
- CI/CD and release automation — Responsibility, 85% coverage, weight 1.17. matched: automated, ci/cd, integration, pipelines source: Built CI/CD pipelines for .NET/Razor backend services, enabling automated deployment of record-retrieval and integration APIs Undergraduate Researcher May 2026 – Present Adam O’Neill Research Lab, UMass Amherst Amherst, MA
  - instruction: Preserve CI/CD and release automation as working evidence. Keep the exact claim visible and, if editing, retain the concrete terms/results that made it match.

## Existing evidence to strengthen

- Go/Java/Python + Redis/ElastiCache/gRPC/REST backend services — Stack module, 62% coverage, weight 1.75. matched: Python, REST caps: no production/scope/result cap 0.75 source: Implemented tool-calling workflows with REST APIs for policy lookup, denial analysis, and appeal generation, adopted by 10+ medical practices after project handoff Scenic Route Optimizer | Python, PyTorch, Mapbox, OSMnx, Hugging Face, AWS (EC2, S3) May 2025 – Present
  - instruction: Strengthen Go/Java/Python + Redis/ElastiCache/gRPC/REST backend services by making the current project evidence more explicit: Show at least two exact stack terms together in one project/story: ElastiCache, Go, GraphQL, gRPC, Java, PostgreSQL, Python, Redis.; Describe the system built or operated, not just the technologies used.; Include production/scope/result proof: deployment path, reliability, performance, cost, scale, or customer impact.. Existing evidence terms: Python, REST.
- ML model development / productionization — Responsibility, 63% coverage, weight 1.74. matched: training source: Built a scenic routing engine scoring road segments from satellite imagery and terrain elevation using PyTorch, Mapbox, and OSMnx, deployed on AWS EC2 with S3 for geospatial data – generated 5,000+ training examples and scenic heatmaps
  - instruction: Strengthen ML model development / productionization by making the current project evidence more explicit: Show this responsibility inside a concrete project/story, not as a generic trait.; Name the system, deliverable, or workflow you owned or changed.; Tie it to production/scope/result proof: users, traffic, data volume, latency, cost, reliability, adoption, incidents, or team scope.. Existing evidence terms: training, ml model development/productionization.
- Java — Tool, 35% coverage, weight 0.97. matched: Java caps: skills-list/tool-only cap 0.35 source: Languages: Python, JavaScript, Solidity, Java, C++, SQL
  - instruction: Strengthen Java by naming the exact stack inside a real project bullet, not only a skills list: action + system/deliverable + production/result proof. Existing evidence terms: Java.
- SQL — Tool, 35% coverage, weight 0.86. matched: SQL caps: skills-list/tool-only cap 0.35 source: Languages: Python, JavaScript, Solidity, Java, C++, SQL
  - instruction: Strengthen SQL by naming the exact stack inside a real project bullet, not only a skills list: action + system/deliverable + production/result proof. Existing evidence terms: SQL.
- Kafka + Spark + Snowflake/Airflow/dbt data pipelines — Stack module, 55% coverage, weight 1.22. matched: Databricks caps: partial stack cap 0.55 source: Built a retrieval-augmented documentation assistant used by the MARVEL engineering team, indexing roughly 5,000 pages across Confluence and Databricks, reducing documentation search time by over 50%
  - instruction: Strengthen Kafka + Spark + Snowflake/Airflow/dbt data pipelines by making the current project evidence more explicit: Show at least two exact stack terms together in one project/story: Airflow, Databricks, dbt, Flink, Kafka, Snowflake, Spark.; Describe the system built or operated, not just the technologies used.; Include production/scope/result proof: deployment path, reliability, performance, cost, scale, or customer impact.. Existing evidence terms: Databricks.
- Engineering mentorship / people leadership — Responsibility, 55% coverage, weight 1.19. matched: engineering team caps: semantic-only responsibility match cap 0.55 source: Built a retrieval-augmented documentation assistant used by the MARVEL engineering team, indexing roughly 5,000 pages across Confluence and Databricks, reducing documentation search time by over 50%
  - instruction: Strengthen Engineering mentorship / people leadership by making the current project evidence more explicit: Show this responsibility inside a concrete project/story, not as a generic trait.; Name the system, deliverable, or workflow you owned or changed.; Tie it to production/scope/result proof: users, traffic, data volume, latency, cost, reliability, adoption, incidents, or team scope.. Existing evidence terms: engineering team, engineering mentorship/people leadership.
- Data ingestion / pipeline processing — Responsibility, 63% coverage, weight 1.40. matched: pipeline source: Developed a retrieval pipeline over thousands of medical documents using Firecrawl and Qdrant vector search deployed on AWS EC2
  - instruction: Strengthen Data ingestion / pipeline processing by making the current project evidence more explicit: Show this responsibility inside a concrete project/story, not as a generic trait.; Name the system, deliverable, or workflow you owned or changed.; Tie it to production/scope/result proof: users, traffic, data volume, latency, cost, reliability, adoption, incidents, or team scope.. Existing evidence terms: pipeline, data ingestion/pipeline.
- React — Tool, 35% coverage, weight 0.78. matched: React caps: skills-list/tool-only cap 0.35 source: Frameworks & Tools: React, Docker, Kubernetes, Prometheus, Grafana, pandas, Git
  - instruction: Strengthen React by naming the exact stack inside a real project bullet, not only a skills list: action + system/deliverable + production/result proof. Existing evidence terms: React.
- Cloud/IaC/runtime: Kubernetes + Docker — Stack module, 55% coverage, weight 1.04. matched: Docker caps: partial stack cap 0.55 source: Operated a direct-to-consumer ecommerce brand that generated 4M+ impressions across Instagram and TikTok P ROJECTS Medical Billing Agent | Python, Docker, Qdrant, Firecrawl, vLLM, AWS (EC2, API Gateway) June 2025 – September 2025
  - instruction: Strengthen Cloud/IaC/runtime: Kubernetes + Docker by making the current project evidence more explicit: Show at least two exact stack terms together in one project/story: Docker, Kubernetes.; Describe the system built or operated, not just the technologies used.; Include production/scope/result proof: deployment path, reliability, performance, cost, scale, or customer impact.. Existing evidence terms: Docker.
- Kubernetes — Tool, 69% coverage, weight 1.49. matched: Kubernetes source: Built a Kubernetes monitoring dashboard for engineering teams using Prometheus and Grafana alerts on AWS EC2 to reduce manual cluster health checks
  - instruction: Strengthen Kubernetes by making the current project evidence more explicit: Name Kubernetes inside a concrete project, not only a skills list.; Show the task/deliverable: built, deployed, operated, optimized, migrated, secured, or debugged something.; Add production/scope/result proof: users, traffic, data volume, latency, cost, reliability, uptime, incidents, or team ownership.. Existing evidence terms: Kubernetes.
- Internal / developer tooling — Responsibility, 55% coverage, weight 0.97. matched: dashboard caps: semantic-only responsibility match cap 0.55 source: Built a Kubernetes monitoring dashboard for engineering teams using Prometheus and Grafana alerts on AWS EC2 to reduce manual cluster health checks
  - instruction: Strengthen Internal / developer tooling by making the current project evidence more explicit: Show this responsibility inside a concrete project/story, not as a generic trait.; Name the system, deliverable, or workflow you owned or changed.; Tie it to production/scope/result proof: users, traffic, data volume, latency, cost, reliability, adoption, incidents, or team scope.. Existing evidence terms: dashboard, internal/developer tooling.
- Kubernetes + Terraform/Pulumi/Helm deployment automation — Stack module, 55% coverage, weight 0.95. matched: Kubernetes caps: partial stack cap 0.55 source: Built a Kubernetes monitoring dashboard for engineering teams using Prometheus and Grafana alerts on AWS EC2 to reduce manual cluster health checks
  - instruction: Strengthen Kubernetes + Terraform/Pulumi/Helm deployment automation by making the current project evidence more explicit: Show at least two exact stack terms together in one project/story: CloudFormation, Helm, Kubernetes, Kustomize, Pulumi, Terraform.; Describe the system built or operated, not just the technologies used.; Include production/scope/result proof: deployment path, reliability, performance, cost, scale, or customer impact.. Existing evidence terms: Kubernetes.

## Uncovered high-value asks

- staff/principal architecture leadership — Proof signal, 0% coverage, weight 1.66.
  - instruction: If staff/principal architecture leadership is real evidence, make it explicit. Do not invent proof signal evidence; a real claim needs: Provide concrete proof, not a generic trait.; Tie the proof to exact systems/tools: architect, architecture, principal, staff.; Prefer quantified or operational evidence: latency, throughput, cost, uptime, users, data volume, incident count, team scope..
- latency / throughput / performance result — Proof signal, 0% coverage, weight 1.40.
  - instruction: If latency / throughput / performance result is real evidence, make it explicit. Do not invent proof signal evidence; a real claim needs: Provide concrete proof, not a generic trait.; Tie the proof to exact systems/tools: latency, low-latency, performance, throughput.; Prefer quantified or operational evidence: latency, throughput, cost, uptime, users, data volume, incident count, team scope..
- GCP — Tool, 0% coverage, weight 1.29.
  - instruction: If GCP is real evidence, make it explicit. Do not invent tool evidence; a real claim needs: Name GCP inside a concrete project, not only a skills list.; Show the task/deliverable: built, deployed, operated, optimized, migrated, secured, or debugged something.; Add production/scope/result proof: users, traffic, data volume, latency, cost, reliability, uptime, incidents, or team ownership..
- Azure — Tool, 0% coverage, weight 1.16.
  - instruction: If Azure is real evidence, make it explicit. Do not invent tool evidence; a real claim needs: Name Azure inside a concrete project, not only a skills list.; Show the task/deliverable: built, deployed, operated, optimized, migrated, secured, or debugged something.; Add production/scope/result proof: users, traffic, data volume, latency, cost, reliability, uptime, incidents, or team ownership..
- Terraform — Tool, 0% coverage, weight 1.08.
  - instruction: If Terraform is real evidence, make it explicit. Do not invent tool evidence; a real claim needs: Name Terraform inside a concrete project, not only a skills list.; Show the task/deliverable: built, deployed, operated, optimized, migrated, secured, or debugged something.; Add production/scope/result proof: users, traffic, data volume, latency, cost, reliability, uptime, incidents, or team ownership..
- Cross-functional coordination / alignment — Responsibility, 0% coverage, weight 1.06.
  - instruction: If Cross-functional coordination / alignment is real evidence, make it explicit. Do not invent responsibility evidence; a real claim needs: Show this responsibility inside a concrete project/story, not as a generic trait.; Name the system, deliverable, or workflow you owned or changed.; Tie it to production/scope/result proof: users, traffic, data volume, latency, cost, reliability, adoption, incidents, or team scope..
- Product / technical roadmap planning — Responsibility, 0% coverage, weight 1.04.
  - instruction: If Product / technical roadmap planning is real evidence, make it explicit. Do not invent responsibility evidence; a real claim needs: Show this responsibility inside a concrete project/story, not as a generic trait.; Name the system, deliverable, or workflow you owned or changed.; Tie it to production/scope/result proof: users, traffic, data volume, latency, cost, reliability, adoption, incidents, or team scope..
- Data analytics / governance / modeling — Responsibility, 0% coverage, weight 0.99.
  - instruction: If Data analytics / governance / modeling is real evidence, make it explicit. Do not invent responsibility evidence; a real claim needs: Show this responsibility inside a concrete project/story, not as a generic trait.; Name the system, deliverable, or workflow you owned or changed.; Tie it to production/scope/result proof: users, traffic, data volume, latency, cost, reliability, adoption, incidents, or team scope..
- Go — Tool, 0% coverage, weight 0.99.
  - instruction: If Go is real evidence, make it explicit. Do not invent tool evidence; a real claim needs: Name Go inside a concrete project, not only a skills list.; Show the task/deliverable: built, deployed, operated, optimized, migrated, secured, or debugged something.; Add production/scope/result proof: users, traffic, data volume, latency, cost, reliability, uptime, incidents, or team ownership..
- Performance / cost / latency optimization — Responsibility, 0% coverage, weight 0.97.
  - instruction: If Performance / cost / latency optimization is real evidence, make it explicit. Do not invent responsibility evidence; a real claim needs: Show this responsibility inside a concrete project/story, not as a generic trait.; Name the system, deliverable, or workflow you owned or changed.; Tie it to production/scope/result proof: users, traffic, data volume, latency, cost, reliability, adoption, incidents, or team scope..
- TypeScript — Tool, 0% coverage, weight 0.93.
  - instruction: If TypeScript is real evidence, make it explicit. Do not invent tool evidence; a real claim needs: Name TypeScript inside a concrete project, not only a skills list.; Show the task/deliverable: built, deployed, operated, optimized, migrated, secured, or debugged something.; Add production/scope/result proof: users, traffic, data volume, latency, cost, reliability, uptime, incidents, or team ownership..
- Real-time / streaming / low-latency systems — Responsibility, 0% coverage, weight 0.88.
  - instruction: If Real-time / streaming / low-latency systems is real evidence, make it explicit. Do not invent responsibility evidence; a real claim needs: Show this responsibility inside a concrete project/story, not as a generic trait.; Name the system, deliverable, or workflow you owned or changed.; Tie it to production/scope/result proof: users, traffic, data volume, latency, cost, reliability, adoption, incidents, or team scope..

## Gates and screens

Satisfied:
- Bachelor/Master degree requirement — education credential; Keep this credential/screen explicit and easy to find.

Missing / only include if explicitly true:
- FedRAMP / government security framework exposure — framework exposure; Include this as an explicit credential/screen; do not turn it into a project story.
- PhD / advanced research credential — education credential; Include this as an explicit credential/screen; do not turn it into a project story.
- security clearance / TS-SCI eligibility — regulated eligibility gate; Include this as an explicit credential/screen; do not turn it into a project story.
- CISSP / Security+ / cloud security certification — certification; Include this as an explicit credential/screen; do not turn it into a project story.
- US citizenship / ITAR eligibility — regulated eligibility gate; Include this as an explicit credential/screen; do not turn it into a project story.

## Claim-level rewrite targets

- r018: Languages: Python, JavaScript, Solidity, Java, C++, SQL
  - missing/weak signals: Convert list/stack evidence into a concrete project bullet with an action verb.; Add production scope users, customers, data volume, systems, reliability, deployment, or ownership.; Add a measured result latency, throughput, cost, accuracy, uptime, time saved, incident reduction, or adoption.; Add seniority/ownership context led, owned, standardized, mentored, org-wide adoption, or cross-team rollout.
  - targeted factors: Python; Go/Java/Python + Redis/ElastiCache/gRPC/REST backend services; Quant/trading/HPC: Python + SQL; Backend/API/cache: Python + SQL
  - rewrite formula: split the stack list into project bullets: [built/deployed/operated/optimized] [system] using [exact tools] to achieve [scope/result]. Do not leave important tools only in a skills list.
- r019: Frameworks & Tools: React, Docker, Kubernetes, Prometheus, Grafana, pandas, Git
  - missing/weak signals: Convert list/stack evidence into a concrete project bullet with an action verb.; Add production scope users, customers, data volume, systems, reliability, deployment, or ownership.; Add a measured result latency, throughput, cost, accuracy, uptime, time saved, incident reduction, or adoption.; Add seniority/ownership context led, owned, standardized, mentored, org-wide adoption, or cross-team rollout.
  - targeted factors: Kubernetes; Docker; IaC / container platform infrastructure; Prometheus/Grafana/OpenTelemetry observability
  - rewrite formula: split the stack list into project bullets: [built/deployed/operated/optimized] [system] using [exact tools] to achieve [scope/result]. Do not leave important tools only in a skills list.
- r006: Investigating distance-comparison-preserving encryption schemes for privacy-preserving retrieval and RAG systems, including evaluating membership inference attacks against encrypted embedding databases Cloud Engineering Intern May 2025 – August 2025 Johnson & Johnson Raritan, NJ
  - missing/weak signals: Convert list/stack evidence into a concrete project bullet with an action verb.; Add production scope users, customers, data volume, systems, reliability, deployment, or ownership.; Add a measured result latency, throughput, cost, accuracy, uptime, time saved, incident reduction, or adoption.; Add seniority/ownership context led, owned, standardized, mentored, org-wide adoption, or cross-team rollout.
  - targeted factors: ML model development / productionization; AI/LLM product integration; AI research-to-production; production ownership / operating live systems around RAG
  - rewrite formula: split the stack list into project bullets: [built/deployed/operated/optimized] [system] using [exact tools] to achieve [scope/result]. Do not leave important tools only in a skills list.
- r020: Cloud: AWS (EC2, S3, API Gateway)
  - missing/weak signals: Convert list/stack evidence into a concrete project bullet with an action verb.; Add production scope users, customers, data volume, systems, reliability, deployment, or ownership.; Add a measured result latency, throughput, cost, accuracy, uptime, time saved, incident reduction, or adoption.; Add seniority/ownership context led, owned, standardized, mentored, org-wide adoption, or cross-team rollout.
  - targeted factors: S3
  - rewrite formula: split the stack list into project bullets: [built/deployed/operated/optimized] [system] using [exact tools] to achieve [scope/result]. Do not leave important tools only in a skills list.
- r015: Implemented tool-calling workflows with REST APIs for policy lookup, denial analysis, and appeal generation, adopted by 10+ medical practices after project handoff Scenic Route Optimizer | Python, PyTorch, Mapbox, OSMnx, Hugging Face, AWS (EC2, S3) May 2025 – Present
  - missing/weak signals: Add production scope users, customers, data volume, systems, reliability, deployment, or ownership.; Add a measured result latency, throughput, cost, accuracy, uptime, time saved, incident reduction, or adoption.; Add seniority/ownership context led, owned, standardized, mentored, org-wide adoption, or cross-team rollout.
  - targeted factors: Python; Go/Java/Python + Redis/ElastiCache/gRPC/REST backend services; Product feature delivery; API / microservice integration
  - rewrite formula: rewrite as: [action verb] [specific system/deliverable] using [exact tools/responsibilities] for [production scope], resulting in [measured outcome].
- r021: Compliance: SOC2, HIPAA
  - missing/weak signals: Convert list/stack evidence into a concrete project bullet with an action verb.; Add production scope users, customers, data volume, systems, reliability, deployment, or ownership.; Add a measured result latency, throughput, cost, accuracy, uptime, time saved, incident reduction, or adoption.; Add seniority/ownership context led, owned, standardized, mentored, org-wide adoption, or cross-team rollout.
  - targeted factors: security / compliance / auditability proof; Compliance / audit / regulatory evidence; Security controls / vulnerability hardening
  - rewrite formula: split the stack list into project bullets: [built/deployed/operated/optimized] [system] using [exact tools] to achieve [scope/result]. Do not leave important tools only in a skills list.
- r016: Built a scenic routing engine scoring road segments from satellite imagery and terrain elevation using PyTorch, Mapbox, and OSMnx, deployed on AWS EC2 with S3 for geospatial data – generated 5,000+ training examples and scenic heatmaps
  - missing/weak signals: Add a measured result latency, throughput, cost, accuracy, uptime, time saved, incident reduction, or adoption.; Add seniority/ownership context led, owned, standardized, mentored, org-wide adoption, or cross-team rollout.
  - targeted factors: ML model development / productionization; PyTorch; AI/ML infra: PyTorch + TensorFlow; AWS ECS/S3/SQS/Lambda/DynamoDB service architecture
  - rewrite formula: rewrite as: [action verb] [specific system/deliverable] using [exact tools/responsibilities] for [production scope], resulting in [measured outcome].
- r008: Built a retrieval-augmented documentation assistant used by the MARVEL engineering team, indexing roughly 5,000 pages across Confluence and Databricks, reducing documentation search time by over 50%
  - missing/weak signals: Add production scope users, customers, data volume, systems, reliability, deployment, or ownership.
  - targeted factors: Kafka + Spark + Snowflake/Airflow/dbt data pipelines; Engineering mentorship / people leadership; Databricks; Data pipelines/analytics: Spark + Databricks
  - rewrite formula: rewrite as: [action verb] [specific system/deliverable] using [exact tools/responsibilities] for [production scope], resulting in [measured outcome].
- r007: Built a Kubernetes monitoring dashboard for engineering teams using Prometheus and Grafana alerts on AWS EC2 to reduce manual cluster health checks
  - missing/weak signals: Add a measured result latency, throughput, cost, accuracy, uptime, time saved, incident reduction, or adoption.
  - targeted factors: AWS; reliability / observability / incident response; Kubernetes; Observability / monitoring / alerting
  - rewrite formula: rewrite as: [action verb] [specific system/deliverable] using [exact tools/responsibilities] for [production scope], resulting in [measured outcome].
- r012: Operated a direct-to-consumer ecommerce brand that generated 4M+ impressions across Instagram and TikTok P ROJECTS Medical Billing Agent | Python, Docker, Qdrant, Firecrawl, vLLM, AWS (EC2, API Gateway) June 2025 – September 2025
  - missing/weak signals: Add seniority/ownership context led, owned, standardized, mentored, org-wide adoption, or cross-team rollout.
  - targeted factors: Python; production ownership / operating live systems; AWS; Agentic AI workflow orchestration
  - rewrite formula: rewrite as: [action verb] [specific system/deliverable] using [exact tools/responsibilities] for [production scope], resulting in [measured outcome].
- r014: Developed a retrieval pipeline over thousands of medical documents using Firecrawl and Qdrant vector search deployed on AWS EC2
  - missing/weak signals: Add a measured result latency, throughput, cost, accuracy, uptime, time saved, incident reduction, or adoption.; Add seniority/ownership context led, owned, standardized, mentored, org-wide adoption, or cross-team rollout.
  - targeted factors: Data ingestion / pipeline processing
  - rewrite formula: rewrite as: [action verb] [specific system/deliverable] using [exact tools/responsibilities] for [production scope], resulting in [measured outcome].
- r005: Built CI/CD pipelines for .NET/Razor backend services, enabling automated deployment of record-retrieval and integration APIs Undergraduate Researcher May 2026 – Present Adam O’Neill Research Lab, UMass Amherst Amherst, MA
  - missing/weak signals: Add a measured result latency, throughput, cost, accuracy, uptime, time saved, incident reduction, or adoption.; Add seniority/ownership context led, owned, standardized, mentored, org-wide adoption, or cross-team rollout.
  - targeted factors: CI/CD and release automation; Backend service architecture / development; API / microservice integration; CI/CD
  - rewrite formula: rewrite as: [action verb] [specific system/deliverable] using [exact tools/responsibilities] for [production scope], resulting in [measured outcome].

## Score-tuning checklist context

This is the scorer context the diagnostic must use. It replaces the old raw JSON appendix: use it to reason about score bottlenecks, but do not insert missing factors into the resume unless they are grounded in the claim map.

Selected bucket: AI / ML Infrastructure (ai-ml-infrastructure)
Score: 40.39/100

Coverage bands:
- strong coverage 75-100%: 35
- good coverage 55-74%: 30
- partial coverage 35-54%: 10
- weak coverage 1-34%: 0
- zero coverage: 80

Type summary, weakest first:
- Niche hook: score 6.60, 2/25 positive, 23 zero, covered weight 0.47 / 7.17
- Tool: score 29.83, 15/35 positive, 20 zero, covered weight 8.23 / 27.59
- Responsibility: score 46.69, 21/35 positive, 14 zero, covered weight 14.61 / 31.29
- Proof signal: score 50.00, 10/20 positive, 10 zero, covered weight 7.07 / 14.14
- Stack module: score 50.55, 27/40 positive, 13 zero, covered weight 10.01 / 19.81

Caps observed:
- partial stack cap 0.55: 17
- skills-list/tool-only cap 0.35: 7
- no production/scope/result cap 0.75: 5
- semantic-only responsibility match cap 0.55: 4
- no action/project cap 0.45: 1

Gate counts: 1 satisfied, 5 missing

## Your output contract

Write the response in this exact order. Start immediately with the heading "## 1. Diagnostic report". Do not write a preamble, acknowledgement, summary of the uploaded prompt, or "what do you want next" question.

1. Diagnostic report
   - Best-fit role interpretation: what role the resume currently fits and why.
   - Source-grounded exact strengths: tools, systems, metrics, domains, responsibilities, proof signals, and claim IDs.
   - Score bottlenecks using the Score-tuning checklist context: weak factor types, caps, zero-coverage areas, list-only/tool-only evidence, missing production scope, missing seniority/ownership, missing metrics, weak niche hook, and gates/screens.
   - Market-demand gaps that should become questions, not invented bullet content.
   - Location-market context: how the selected market changes emphasis for this role and how the selected role compares with the other 9 role buckets in that market.

2. Resume rewrite strategy
   - Which bullets to keep mostly intact and why.
   - Which bullets to merge, split, promote, reorder, or rewrite and why.
   - Which skills-list terms should be moved into real project bullets, using only Resume-grounded exact term inventory terms.
   - Which high-weight missing items should be ignored unless the user confirms they are true.
   - A truthfulness audit note identifying any suggested claim that needs confirmation.

3. Optimized bullet candidates
   For each candidate bullet, include:
   - Exact source claim IDs from the Authoritative parsed claim map; do not cite IDs that are not listed there.
   - Rewritten bullet.
   - Why it improves role fit.
   - Target factors/tools/proof signals.
   - Safety label: "safe from provided evidence" or "needs user confirmation". Any bullet containing a tool/fact absent from source claims must be labeled "needs user confirmation" and phrased as a question, not as a final bullet.

4. Missing-fact questions
   Ask only questions that would materially improve the resume for this role.
   Tie every question to a high-weight missing/partial factor.

5. Final positioning
   Give a concise positioning statement for this target role.

6. Location-market positioning
   - Explain whether the selected location strengthens or weakens this role target, using the local role jobs, lift, sample quality, and over-indexed signals.
   - Name which of the 10 role buckets look strongest in this market and whether that changes resume variant priority.
   - Suggest one location-aware headline/positioning line using only source-grounded resume facts.
   - List location facts requiring confirmation: remote-only, hybrid, relocation, citizenship, clearance, work authorization, commute radius.

Begin now with: ## 1. Diagnostic report