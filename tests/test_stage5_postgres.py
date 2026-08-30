from __future__ import annotations

import os
import uuid

import pytest
from fastapi import HTTPException
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.main import GenerationRequest, TaskType, generate, get_request

pytestmark = pytest.mark.postgres


@pytest.mark.asyncio
async def test_ai_idempotency_and_tenant_isolation():
    engine = create_async_engine(os.environ["DATABASE_URL"])
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    tenant_a = f"tenant-a-{uuid.uuid4()}"
    tenant_b = f"tenant-b-{uuid.uuid4()}"
    key = f"ai-{uuid.uuid4()}"
    request = GenerationRequest(task=TaskType.SUMMARIZE, input="stage five safe input")

    async with sessions() as session:
        first = await generate(request, tenant_a, key, session)
    async with sessions() as session:
        duplicate = await generate(request, tenant_a, key, session)
        assert duplicate.request_id == first.request_id
    async with sessions() as session:
        with pytest.raises(HTTPException) as conflict:
            await generate(
                GenerationRequest(task=TaskType.SUMMARIZE, input="different input"),
                tenant_a,
                key,
                session,
            )
        assert conflict.value.status_code == 409
    async with sessions() as session:
        with pytest.raises(HTTPException) as denied:
            await get_request(first.request_id, tenant_b, session)
        assert denied.value.status_code == 404

    await engine.dispose()
