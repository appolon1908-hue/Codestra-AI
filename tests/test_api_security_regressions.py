from __future__ import annotations

import base64
import json

import pytest
import httpx
from fastapi import HTTPException
from jwt.exceptions import PyJWKClientError

from app import auth
from app.main import GenerationRequest, TaskType, _decode_cursor, app, generate
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
async def test_whitespace_tenant_alias_is_rejected_before_handler():
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get(
            "/v1/ai/requests",
            headers={"X-Tenant-ID": " tenant-1 "},
        )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_tenant_header"


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


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "document",
    [
        {"operation_id": None, "state": None},
        {"operation_id": "", "state": "accepted"},
        {"operation_id": "operation-1", "state": ""},
        {"operation_id": "x" * 129, "state": "accepted"},
    ],
)
async def test_middleware_rejects_null_or_empty_operation_identity(
    monkeypatch: pytest.MonkeyPatch, document: dict[str, object]
):
    monkeypatch.setenv("MIDDLEWARE_BASE_URL", "https://middleware.example")
    client = MiddlewareAIClient()
    monkeypatch.setattr(client, "_token", lambda: "synthetic-token")

    class Response:
        status_code = 202

        def json(self):
            return document

    class StubClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        async def post(self, *_args, **_kwargs):
            return Response()

    monkeypatch.setattr(httpx, "AsyncClient", lambda **_kwargs: StubClient())
    with pytest.raises(MiddlewareSubmissionError) as failure:
        await client.submit({}, tenant_id="tenant-1", correlation_id="correlation-1", idempotency_key="request-key")
    assert failure.value.outcome_unknown is True


@pytest.mark.asyncio
async def test_jwks_lookup_failure_is_a_safe_authentication_error(monkeypatch: pytest.MonkeyPatch):
    class BrokenJWKClient:
        def get_signing_key_from_jwt(self, _token: str):
            raise PyJWKClientError("synthetic unknown key")

    monkeypatch.setattr(auth, "_jwk_client", lambda: BrokenJWKClient())
    with pytest.raises(HTTPException) as denied:
        await auth._decode("synthetic-token")
    assert denied.value.status_code == 401
    assert denied.value.detail == "invalid_access_token"
    assert denied.value.headers == {"WWW-Authenticate": "Bearer"}


@pytest.mark.parametrize("cursor", ["a", "_w", "not-base64!!"])
def test_every_malformed_cursor_is_a_safe_client_error(cursor: str):
    with pytest.raises(HTTPException) as invalid:
        _decode_cursor(cursor)
    assert invalid.value.status_code == 400
    assert invalid.value.detail == "invalid_cursor"


def test_timezone_naive_cursor_is_a_safe_client_error():
    value = base64.urlsafe_b64encode(
        json.dumps(["2026-09-02T12:00:00", "00000000-0000-0000-0000-000000000001"]).encode()
    ).decode().rstrip("=")
    with pytest.raises(HTTPException) as invalid:
        _decode_cursor(value)
    assert invalid.value.status_code == 400
    assert invalid.value.detail == "invalid_cursor"


@pytest.mark.asyncio
async def test_rate_limit_rejection_is_explicitly_retryable(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("MIDDLEWARE_BASE_URL", "https://middleware.example")
    client = MiddlewareAIClient()
    monkeypatch.setattr(client, "_token", lambda: "synthetic-token")

    class Response:
        status_code = 429

    class StubClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        async def post(self, *_args, **_kwargs):
            return Response()

    monkeypatch.setattr(httpx, "AsyncClient", lambda **_kwargs: StubClient())
    with pytest.raises(MiddlewareSubmissionError) as failure:
        await client.submit({}, tenant_id="tenant-1", correlation_id="correlation-1", idempotency_key="request-key")
    assert failure.value.retryable is True
    assert failure.value.outcome_unknown is False


def test_runtime_openapi_publishes_oauth_client_credentials_and_scopes():
    document = app.openapi()
    scheme = document["components"]["securitySchemes"]["serviceBearer"]
    assert "clientCredentials" in scheme["flows"]
    assert "ai.request" in document["paths"]["/v1/ai/generate"]["post"]["security"][0]["serviceBearer"]
    assert "ai.cancel" in document["paths"]["/v1/ai/requests/{request_id}/cancel"]["post"]["security"][0]["serviceBearer"]
