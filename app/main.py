from __future__ import annotations

import hashlib
import json
import os
from enum import StrEnum
from typing import Annotated, Any
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, FastAPI, Header, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from .db import get_session
from .models import AIRequestModel
from .providers.router import resolve_route
from .telemetry import audit_event, configure_telemetry, install_correlation_middleware

app = FastAPI(title="Codestra AI Gateway", version="0.3.0")
install_correlation_middleware(app)
TELEMETRY_EXPORT_ENABLED = configure_telemetry(app)
router = APIRouter(prefix="/v1/ai")
EXTERNAL_MODEL_CALLS_ENABLED = os.getenv("EXTERNAL_MODEL_CALLS_ENABLED", "false").lower() == "true"

TenantHeader = Annotated[str, Header(alias="X-Tenant-ID", min_length=1, max_length=128)]
IdempotencyHeader = Annotated[str, Header(alias="Idempotency-Key", min_length=8, max_length=200)]


class TaskType(StrEnum):
    COPY = "copy"
    CLASSIFY = "classify"
    SUMMARIZE = "summarize"
    SCORE = "score"
    CREATIVE_BRIEF = "creative_brief"


class GenerationRequest(BaseModel):
    task: TaskType
    input: str = Field(min_length=1, max_length=20000)
    tenant_id: str | None = Field(default=None, min_length=1, max_length=128)
    campaign_id: str | None = Field(default=None, max_length=128)


class GenerationResponse(BaseModel):
    request_id: UUID
    status: str
    output: str | None = None
    provider: str | None = None
    model: str | None = None
    input_tokens: int = 0
    output_tokens: int = 0
    cost_micros: int = 0


def _tenant(header_tenant: str, body_tenant: str | None) -> str:
    if body_tenant is not None and body_tenant != header_tenant:
        raise HTTPException(status_code=403, detail="tenant_mismatch")
    return header_tenant


def _fingerprint(tenant_id: str, body: GenerationRequest) -> tuple[str, str]:
    input_digest = hashlib.sha256(body.input.encode("utf-8")).hexdigest()
    request = {
        "tenant_id": tenant_id,
        "task": body.task.value,
        "input_digest": input_digest,
        "campaign_id": body.campaign_id,
    }
    encoded = json.dumps(request, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return input_digest, hashlib.sha256(encoded).hexdigest()


def _response(row: AIRequestModel) -> GenerationResponse:
    return GenerationResponse(
        request_id=row.id,
        status=row.status,
        output=row.output_text,
        provider=row.provider,
        model=row.model,
        input_tokens=row.input_tokens,
        output_tokens=row.output_tokens,
        cost_micros=row.cost_micros,
    )


@app.get("/health")
def health() -> dict[str, object]:
    return {"status": "ok", "external_model_calls_enabled": EXTERNAL_MODEL_CALLS_ENABLED}


@router.get("/capabilities")
def capabilities() -> dict[str, object]:
    return {
        "structured_generation": True,
        "audit": True,
        "usage_accounting": True,
        "external_model_calls": EXTERNAL_MODEL_CALLS_ENABLED,
        "correlation_ids": True,
        "telemetry_export": TELEMETRY_EXPORT_ENABLED,
        "business_action_authority": False,
    }


@router.post("/generate", response_model=GenerationResponse, status_code=status.HTTP_202_ACCEPTED)
async def generate(
    body: GenerationRequest,
    x_tenant_id: TenantHeader,
    idempotency_key: IdempotencyHeader,
    session: AsyncSession = Depends(get_session),
) -> GenerationResponse:
    tenant_id = _tenant(x_tenant_id, body.tenant_id)
    input_digest, fingerprint = _fingerprint(tenant_id, body)
    existing = await session.execute(
        select(AIRequestModel).where(
            AIRequestModel.tenant_id == tenant_id,
            AIRequestModel.idempotency_key == idempotency_key,
        )
    )
    row = existing.scalar_one_or_none()
    if row is not None:
        if row.request_fingerprint != fingerprint:
            raise HTTPException(status_code=409, detail="idempotency_conflict")
        audit_event("ai_request_replayed", request_id=str(row.id), status=row.status)
        return _response(row)

    route = resolve_route(body.task.value)
    row = AIRequestModel(
        id=uuid4(),
        tenant_id=tenant_id,
        task=body.task.value,
        provider=route.provider.value,
        model=route.model,
        status="blocked_by_capability" if not EXTERNAL_MODEL_CALLS_ENABLED else "queued",
        input_digest=input_digest,
        idempotency_key=idempotency_key,
        request_fingerprint=fingerprint,
    )
    session.add(row)
    try:
        await session.commit()
    except IntegrityError:
        await session.rollback()
        result = await session.execute(
            select(AIRequestModel).where(
                AIRequestModel.tenant_id == tenant_id,
                AIRequestModel.idempotency_key == idempotency_key,
            )
        )
        row = result.scalar_one_or_none()
        if row is None or row.request_fingerprint != fingerprint:
            raise HTTPException(status_code=409, detail="idempotency_conflict")
        audit_event("ai_request_replayed", request_id=str(row.id), status=row.status)
        return _response(row)
    await session.refresh(row)
    audit_event("ai_request_recorded", request_id=str(row.id), status=row.status)

    if EXTERNAL_MODEL_CALLS_ENABLED:
        raise HTTPException(status_code=501, detail="external_provider_execution_not_implemented")
    return _response(row)


@router.get("/requests/{request_id}", response_model=GenerationResponse)
async def get_request(
    request_id: UUID,
    x_tenant_id: TenantHeader,
    session: AsyncSession = Depends(get_session),
) -> GenerationResponse:
    result = await session.execute(
        select(AIRequestModel).where(
            AIRequestModel.id == request_id,
            AIRequestModel.tenant_id == x_tenant_id,
        )
    )
    row = result.scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail="ai_request_not_found")
    return _response(row)


app.include_router(router)
