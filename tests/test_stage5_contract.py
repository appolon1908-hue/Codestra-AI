from app.main import EXTERNAL_MODEL_CALLS_ENABLED, app, capabilities


def test_canonical_ai_routes_match_sdk_and_kong_contract():
    paths = set(app.openapi()["paths"])
    assert "/v1/ai/generate" in paths
    assert "/v1/ai/requests/{request_id}" in paths
    assert "/v1/ai/capabilities" in paths
    assert "/v1/generate" not in paths


def test_capabilities_are_truthful_and_external_calls_remain_disabled():
    value = capabilities()
    assert value["audit"] is True
    assert value["usage_accounting"] is True
    assert value["external_model_calls"] is False
    assert EXTERNAL_MODEL_CALLS_ENABLED is False
