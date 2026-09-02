from __future__ import annotations

import pytest
import httpx
from fastapi import HTTPException

from app.main import GenerationRequest, TaskType, app, generate
from app.middleware_client import MiddlewareAIClient, MiddlewareSubmissionError


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "url",
    [
        "http://localhost.attacker.example",
        "http://localhost@attacker.example",
        "http://127.0.0.1.attacker.example",
    ],
)
async def test_middleware_url_cannot_smuggle_loopback_prefix(monkeypatch: pytest.MonkeyPatch, url: str):
    monkeypatch.setenv("MIDDLEWARE_BASE_URL", url)
    client = MiddlewareAIClient()
    with pytest.raises(MiddlewareSubmissionError, match="middleware_base_url_invalid"):
        await client.submit({}, tenant_id="tenant-1", correlation_id="correlation-1", idempotency_key="request-key")


@pytest.mark.asyncio
async def test_generation_rejects_region_outside_published_policy():
    body = GenerationRequest(task=TaskType.SUMMARIZE, input="safe synthetic input", region="eu")
    with pytest.raises(HTTPException, match="region_not_allowed") as denied:
        await generate(body, "tenant-1", "request-key", object())  # type: ignore[arg-type]
    assert denied.value.status_code == 403


@pytest.mark.asyncio
async def test_http_exceptions_use_the_safe_error_envelope():
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            "/v1/ai/generate",
            headers={
                "X-Tenant-ID": "tenant-1",
                "X-Correlation-ID": "correlation-test-1",
                "Idempotency-Key": "request-key",
            },
            json={"task": "summarize", "input": "synthetic", "region": "eu"},
        )
    assert response.status_code == 403
    assert response.json() == {
        "error": {
            "code": "region_not_allowed",
            "message": "request could not be completed",
            "correlation_id": "correlation-test-1",
            "retryable": False,
            "details": {},
        }
    }


@pytest.mark.asyncio
async def test_validation_errors_use_the_safe_error_envelope():
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            "/v1/ai/generate",
            headers={
                "X-Tenant-ID": "tenant-1",
                "X-Correlation-ID": "correlation-test-2",
                "Idempotency-Key": "request-key",
            },
            json={"task": "summarize", "input": ""},
        )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "request_validation_failed"
