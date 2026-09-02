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


@pytest.mark.asyncio
async def test_authentication_error_preserves_bearer_challenge():
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get(
            "/v1/ai/requests",
            headers={"X-Tenant-ID": "tenant-1", "Authorization": "Basic invalid"},
        )
    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"


@pytest.mark.asyncio
async def test_remote_protocol_failure_is_an_unknown_outcome(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("MIDDLEWARE_BASE_URL", "https://middleware.example")
    client = MiddlewareAIClient()
    monkeypatch.setattr(client, "_token", lambda: "synthetic-token")

    class BrokenClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        async def post(self, *_args, **_kwargs):
            raise httpx.RemoteProtocolError("synthetic protocol failure")

    monkeypatch.setattr(httpx, "AsyncClient", lambda **_kwargs: BrokenClient())
    with pytest.raises(MiddlewareSubmissionError) as failure:
        await client.submit({}, tenant_id="tenant-1", correlation_id="correlation-1", idempotency_key="request-key")
    assert failure.value.outcome_unknown is True
