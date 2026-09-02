from __future__ import annotations

from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram, generate_latest
from prometheus_client.exposition import CONTENT_TYPE_LATEST

REGISTRY = CollectorRegistry(auto_describe=True)

REQUESTS = Counter(
    "codestra_ai_http_requests_total",
    "AI API requests by bounded operation and outcome.",
    ("operation", "method", "status_class"),
    registry=REGISTRY,
)
DURATION = Histogram(
    "codestra_ai_http_request_duration_seconds",
    "AI API request duration by bounded operation.",
    ("operation", "method"),
    registry=REGISTRY,
    buckets=(0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10),
)
AUTH_FAILURES = Counter(
    "codestra_ai_auth_failures_total",
    "Authentication and authorization failures.",
    ("reason",),
    registry=REGISTRY,
)
IDEMPOTENCY_CONFLICTS = Counter(
    "codestra_ai_idempotency_conflicts_total",
    "Semantic idempotency conflicts.",
    registry=REGISTRY,
)
POLICY_DENIALS = Counter(
    "codestra_ai_policy_denials_total",
    "Requests denied by capability or policy.",
    ("reason",),
    registry=REGISTRY,
)
MIDDLEWARE_RESULTS = Counter(
    "codestra_ai_middleware_operation_results_total",
    "Durable Middleware operation submissions by outcome.",
    ("outcome",),
    registry=REGISTRY,
)
RECONCILIATION = Counter(
    "codestra_ai_reconciliation_required_total",
    "Requests requiring Middleware/provider reconciliation.",
    registry=REGISTRY,
)
TOKENS = Counter(
    "codestra_ai_tokens_total",
    "Aggregate token usage; contains no tenant or request labels.",
    ("direction",),
    registry=REGISTRY,
)
COST = Counter(
    "codestra_ai_cost_micros_total",
    "Aggregate model cost in micros; contains no tenant labels.",
    registry=REGISTRY,
)
CAPABILITY = Gauge(
    "codestra_ai_capability_enabled",
    "AI external model capability state.",
    ("capability",),
    registry=REGISTRY,
)


def render_metrics() -> tuple[bytes, str]:
    return generate_latest(REGISTRY), CONTENT_TYPE_LATEST
