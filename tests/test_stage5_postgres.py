from __future__ import annotations

import os
import uuid

import pytest
from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.main import CancelRequest, GenerationRequest, TaskType, cancel_request, generate, get_request
from app.models import AIEventOutboxModel, AIRequestMutationModel
from app.event_worker import run_once

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
        assert await session.scalar(
            select(func.count()).select_from(AIEventOutboxModel).where(AIEventOutboxModel.state == "pending")
        ) == 1
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


@pytest.mark.asyncio
async def test_durable_event_worker_claims_and_publishes_once():
    engine = create_async_engine(os.environ["DATABASE_URL"])
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    tenant_id = f"tenant-events-{uuid.uuid4()}"
    async with sessions() as session:
        created = await generate(
            GenerationRequest(task=TaskType.CLASSIFY, input="synthetic event input"),
            tenant_id,
            f"event-{uuid.uuid4()}",
            session,
        )

    published: list[tuple[str, dict[str, str]]] = []

    class FakeRedis:
        async def xadd(self, topic: str, fields: dict[str, str]):
            published.append((topic, fields))
            return "1-0"

    assert await run_once(FakeRedis(), batch_size=10, lease_seconds=30, max_attempts=3) >= 1  # type: ignore[arg-type]
    assert any(topic == "codestra.ai.requests" for topic, _fields in published)
    assert any(fields["payload"].find(str(created.request_id)) >= 0 for _topic, fields in published)
    async with sessions() as session:
        assert await session.scalar(
            select(func.count()).select_from(AIEventOutboxModel).where(
                AIEventOutboxModel.state == "pending",
                AIEventOutboxModel.payload_json.contains(str(created.request_id)),
            )
        ) == 0
        assert await session.scalar(
            select(func.count()).select_from(AIEventOutboxModel).where(
                AIEventOutboxModel.state == "published",
                AIEventOutboxModel.payload_json.contains(str(created.request_id)),
            )
        ) == 1

    await engine.dispose()


@pytest.mark.asyncio
async def test_cancellation_idempotency_replays_committed_result():
    engine = create_async_engine(os.environ["DATABASE_URL"])
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    tenant_id = f"tenant-cancel-{uuid.uuid4()}"
    request_key = f"ai-{uuid.uuid4()}"
    cancel_key = f"cancel-{uuid.uuid4()}"
    body = GenerationRequest(task=TaskType.SUMMARIZE, input="synthetic cancellation input")

    async with sessions() as session:
        created = await generate(body, tenant_id, request_key, session)
    cancellation = CancelRequest(expected_version=created.resource_version, reason="synthetic cancellation")
    async with sessions() as session:
        first = await cancel_request(created.request_id, cancellation, tenant_id, cancel_key, session)
    async with sessions() as session:
        replay = await cancel_request(created.request_id, cancellation, tenant_id, cancel_key, session)
        assert replay.request_id == first.request_id
        assert replay.resource_version == first.resource_version
        assert replay.status == first.status == "cancelled"
    async with sessions() as session:
        with pytest.raises(HTTPException, match="idempotency_conflict") as conflict:
            await cancel_request(
                created.request_id,
                CancelRequest(expected_version=created.resource_version, reason="different reason"),
                tenant_id,
                cancel_key,
                session,
            )
        assert conflict.value.status_code == 409
        assert await session.scalar(
            select(func.count()).select_from(AIRequestMutationModel).where(
                AIRequestMutationModel.tenant_id == tenant_id,
                AIRequestMutationModel.request_id == created.request_id,
            )
        ) == 1

    await engine.dispose()
