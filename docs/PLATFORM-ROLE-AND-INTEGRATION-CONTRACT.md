# Codestra AI — Role and Integration Contract

## Mission
Codestra AI is the shared enterprise AI gateway and policy layer. It provides model routing, structured generation, evaluation, cost controls, auditability and safe AI capabilities to Codestra services.

## Owns
- Model/provider abstraction and routing
- Prompt/template registry and versioning
- Structured output contracts
- AI request policy, quotas and cost budgets
- Safety filters, audit records and evaluation results
- Model health, latency, token/cost metrics and fallback policy
- Reusable capabilities for generation, classification, extraction, scoring, summarization and recommendation

## Does Not Own
- Marketing campaign state or spend authority
- CRM records
- Message delivery
- Social publishing
- Workflow authority
- Identity or gateway policy

## Mandatory Request Path
Business service -> Codestra SDK/internal service client -> Kong/service mesh policy -> Codestra AI -> approved model provider.
Provider credentials must remain server-side and never be distributed to browsers or unrelated services.

## Human/Policy Boundary
AI may prepare marketing assets, recommendations, qualification summaries and optimization proposals. AI must not independently authorize financial spend, change identity policy, expose secrets, or bypass approval requirements owned by business services.

## Core Domains
ModelProvider, ModelRoute, PromptTemplate, PromptVersion, AIRequest, StructuredResult, Evaluation, PolicyDecision, UsageRecord, CostBudget, ProviderHealth.

## Required APIs
- /v1/ai/generate
- /v1/ai/classify
- /v1/ai/extract
- /v1/ai/score
- /v1/ai/summarize
- /v1/ai/recommend
- /v1/ai/models
- /v1/ai/usage
- /v1/ai/evaluations

## Implementation Order
1. Provider-neutral contracts
2. Policy and tenancy context
3. Structured output validation
4. Audit and usage metering
5. Provider adapters
6. Evaluation harness
7. Cost controls and quotas
8. Business-service integrations
9. Observability and fallback testing