# Codestra AI Architecture

## Role
Codestra AI is the shared AI control plane for Codestra business services. It centralizes provider/model routing, structured generation, classification, scoring, summarization, evaluation, policy, usage accounting, and auditability.

## Owns
- model/provider abstraction
- prompt/template registry
- structured-output schemas
- generation jobs and result lineage
- classification/scoring jobs
- moderation/safety policy hooks
- evaluation suites
- model fallback/routing rules
- cost and quota controls
- usage/audit records

## Does not own
- campaigns or spend: Codestra Marketing
- customer communications delivery: Codestra Communication CC
- social publishing: Codestra Social
- CRM records: Odoo
- orchestration: n8n
- transport/webhook durability: Middleware

## Initial APIs
- POST /v1/ai/generate
- POST /v1/ai/classify
- POST /v1/ai/score
- POST /v1/ai/summarize
- POST /v1/ai/evaluate
- GET /v1/ai/models
- GET /v1/ai/jobs/{id}

## Marketing use cases
- campaign concepts
- ad-copy variants
- landing-page copy
- audience hypothesis generation
- lead qualification assistance
- performance summaries
- optimization recommendations

## Safety
AI outputs are recommendations or drafts unless the calling service has an independently authorized mutation path. AI cannot directly activate campaigns, change budgets, send customer messages, or publish social content without service-level authorization and approval policy.
