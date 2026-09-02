from __future__ import annotations

import pytest
from fastapi import HTTPException

from app.main import GenerationRequest, TaskType, generate
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
