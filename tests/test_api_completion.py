from app.auth import ALLOW_DEV_AUTH_BYPASS, APP_ENV
from app.main import EXTERNAL_MODEL_CALLS_ENABLED, app


def test_api_completion_contract_has_governed_routes():
    paths = app.openapi()["paths"]
    expected = {
        "/health/live",
        "/health/ready",
        "/version",
        "/metrics",
        "/v1/ai/capabilities",
        "/v1/ai/models",
        "/v1/ai/policies",
        "/v1/ai/usage",
        "/v1/ai/generate",
        "/v1/ai/requests",
        "/v1/ai/requests/{request_id}",
        "/v1/ai/requests/{request_id}/events",
        "/v1/ai/requests/{request_id}/cancel",
    }
    assert expected <= set(paths)
    assert "/v1/crm/leads" not in paths
    assert "/v1/workflow/runs" not in paths


def test_effects_remain_disabled_and_dev_bypass_is_bounded():
    assert EXTERNAL_MODEL_CALLS_ENABLED is False
    assert APP_ENV in {"development", "test"} or ALLOW_DEV_AUTH_BYPASS is False


def test_runtime_openapi_declares_bearer_security_for_protected_routes():
    document = app.openapi()
    assert "serviceBearer" in document["components"]["securitySchemes"]
    assert document["paths"]["/v1/ai/generate"]["post"]["security"] == [
        {"serviceBearer": ["ai.request"]}
    ]


def test_request_list_filters_are_declared_in_runtime_and_source_contracts():
    parameters = app.openapi()["paths"]["/v1/ai/requests"]["get"]["parameters"]
    names = {parameter["name"] for parameter in parameters}
    assert {"cursor", "limit", "status", "task"} <= names
