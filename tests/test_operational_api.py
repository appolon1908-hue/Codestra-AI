import httpx
import pytest

from app import main
from app.main import app, capabilities, health, version


def test_operational_endpoints_are_attributable_and_fail_closed():
    assert {"/health", "/ready", "/version", "/capabilities"}.issubset(
        app.openapi()["paths"]
    )
    assert health()["service"] == "codestra-ai"
    assert version()["service"] == "codestra-ai"
    value = capabilities()
    assert value["business_writes_enabled"] is False
    assert value["external_model_calls_enabled"] is False
    assert value["read_only_mode"] is True


def test_version_does_not_invent_runtime_attribution(monkeypatch):
    monkeypatch.delenv("CODESTRA_GIT_SHA", raising=False)
    monkeypatch.delenv("CODESTRA_IMAGE_DIGEST", raising=False)
    value = version()
    assert value["git_sha"] == "unknown"
    assert value["image_digest"] == "unknown"


def test_unimplemented_provider_cannot_be_advertised(monkeypatch):
    monkeypatch.setattr(main, "EXTERNAL_MODEL_CALLS_ENABLED", True)
    monkeypatch.setattr(main, "EXTERNAL_MODEL_EXECUTION_AVAILABLE", False)
    value = capabilities()
    assert value["external_delivery_enabled"] is False
    assert value["external_model_calls_enabled"] is False
    assert value["read_only_mode"] is True


@pytest.mark.asyncio
async def test_operational_headers_and_content_type():
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.get(
            "/health", headers={"X-Correlation-ID": "contract-id"}
        )
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["x-correlation-id"] == "contract-id"
    assert response.headers["content-type"].startswith("application/json")
