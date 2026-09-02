from __future__ import annotations

import base64
import asyncio
import hashlib
import json
import os
import re
import time
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Annotated
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, FastAPI, Header, HTTPException, Query, Request, Security, status
from fastapi.responses import JSONResponse, Response
from fastapi.security import HTTPBearer
from fastapi.exceptions import RequestValidationError
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import and_, func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from .auth import APP_ENV, authenticate
from .db import get_session
from .metrics import (
    AUTH_FAILURES,
    CAPABILITY,
    COST,
    DURATION,
    IDEMPOTENCY_CONFLICTS,
    MIDDLEWARE_RESULTS,
    POLICY_DENIALS,
    RECONCILIATION,
    REQUESTS,
    TOKENS,
    render_metrics,
)
from .middleware_client import MiddlewareAIClient, MiddlewareSubmissionError
from .models import AIEventOutboxModel, AIRequestEventModel, AIRequestModel, AIRequestMutationModel
from .providers.router import ROUTES, resolve_route

SERVICE = "codestra-ai"
API_VERSION = "0.4.0"
EXTERNAL_MODEL_CALLS_ENABLED = (
    os.getenv("EXTERNAL_MODEL_CALLS_ENABLED", "false").strip().lower() == "true"
)
EXTERNAL_MODEL_EXECUTION_AVAILABLE = False
TELEMETRY_EXPORT_ENABLED = (
    os.getenv("TELEMETRY_EXPORT_ENABLED", "false").strip().lower() == "true"
)
SOURCE_SHA = os.getenv("CODESTRA_GIT_SHA", os.getenv("SOURCE_SHA", "unknown"))
IMAGE_DIGEST = os.getenv("CODESTRA_IMAGE_DIGEST", os.getenv("IMAGE_DIGEST", "unknown"))
BUILD_TIMESTAMP = os.getenv("CODESTRA_BUILD_TIMESTAMP", os.getenv("BUILD_TIME", "unknown"))
MIGRATION_REVISION = os.getenv("CODESTRA_MIGRATION_REVISION", "003_api_completion")
DEPLOYMENT_ID = os.getenv("CODESTRA_DEPLOYMENT_ID", "unassigned")
CORRELATION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{7,179}$")

app = FastAPI(title="Codestra AI Gateway", version=API_VERSION)
bearer_contract = HTTPBearer(auto_error=False)
router = APIRouter(prefix="/v1/ai", tags=["ai"], dependencies=[Security(bearer_contract)])

TenantHeader = Annotated[str, Header(alias="X-Tenant-ID", min_length=1, max_length=128)]
IdempotencyHeader = Annotated[str, Header(alias="Idempotency-Key", min_length=8, max_length=200)]
CorrelationHeader = Annotated[str | None, Header(alias="X-Correlation-ID", max_length=180)]
ALLOWED_REGIONS = frozenset({"global"})
BOUNDED_METHODS = frozenset({"GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"})


UTC = timezone.utc


class TaskType(str, Enum):
    COPY = "copy"
    CLASSIFY = "classify"
    SUMMARIZE = "summarize"
    SCORE = "score"
    CREATIVE_BRIEF = "creative_brief"


class GenerationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    task: TaskType
    input: str = Field(min_length=1, max_length=20_000)
    tenant_id: str | None = Field(default=None, min_length=1, max_length=128)
    campaign_id: str | None = Field(default=None, max_length=128)
    data_class: str = Field(default="internal", pattern=r"^(public|internal|confidential)$")
    region: str = Field(default="global", min_length=2, max_length=32)
    maximum_cost_micros: int = Field(default=250_000, ge=0, le=10_000_000)
    retain_output: bool = False


class GenerationResponse(BaseModel):
    request_id: UUID
    status: str
    output: str | None = None
    provider: str | None = None
    model: str | None = None
    input_tokens: int = 0
    output_tokens: int = 0
    cost_micros: int = 0
    middleware_operation_id: str | None = None
    resource_version: int = 1
    created_at: datetime | None = None
    updated_at: datetime | None = None


class RequestEvent(BaseModel):
    event_id: int
    event_type: str
    previous_status: str | None
    new_status: str
    actor_id: str
    safe_detail: str | None
    occurred_at: datetime


class PagedRequests(BaseModel):
    items: list[GenerationResponse]
    next_cursor: str | None


class PagedEvents(BaseModel):
    items: list[RequestEvent]
    next_cursor: str | None


class CancelRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_version: int = Field(ge=1)
    reason: str = Field(
        min_length=1,
        max_length=240,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9_.: -]*$",
    )


def _error(
    request: Request,
    *,
    status_code: int,
    code: str,
    message: str,
    retryable: bool = False,
    headers: dict[str, str] | None = None,
) -> JSONResponse:
    correlation_id = getattr(request.state, "correlation_id", str(uuid4()))
    return JSONResponse(
        status_code=status_code,
        content={
            "error": {
                "code": code,
                "message": message,
                "correlation_id": correlation_id,
                "retryable": retryable,
                "details": {},
            }
        },
        headers={
            **(headers or {}),
            "X-Correlation-ID": correlation_id,
            "Cache-Control": "no-store",
        },
    )


@app.exception_handler(HTTPException)
async def http_exception_handler(request: Request, exc: HTTPException) -> JSONResponse:
    return _error(
        request,
        status_code=exc.status_code,
        code=str(exc.detail),
        message="request could not be completed",
        retryable=exc.status_code >= 500,
        headers=exc.headers,
    )


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, _exc: RequestValidationError) -> JSONResponse:
    return _error(
        request,
        status_code=422,
        code="request_validation_failed",
        message="request validation failed",
    )


def _operation(request: Request) -> str:
    route = request.scope.get("route")
    path = getattr(route, "path", None)
    if isinstance(path, str):
        return path
    return "/metrics" if request.url.path == "/metrics" else "unmatched"


def _required_scope(path: str, method: str) -> str | None:
    if path == "/metrics":
        return "metrics.read"
    if not path.startswith("/v1/ai/") or path.endswith("/capabilities"):
        return None
    if method == "GET":
        return "ai.read"
    if path.endswith("/cancel"):
        return "ai.cancel"
    return "ai.request"


@app.middleware("http")
async def security_observability_boundary(request: Request, call_next):
    supplied = request.headers.get("X-Correlation-ID", "").strip()
    if supplied and not CORRELATION_RE.fullmatch(supplied):
        request.state.correlation_id = str(uuid4())
        return _error(
            request,
            status_code=400,
            code="invalid_correlation_id",
            message="X-Correlation-ID is outside the accepted format",
        )
    request.state.correlation_id = supplied or str(uuid4())
    scope = _required_scope(request.url.path, request.method)
    if scope is not None:
        try:
            request.state.auth = await authenticate(
                request,
                required_scope=scope,
                tenant_required=request.url.path != "/metrics",
            )
        except HTTPException as exc:
            reason = str(exc.detail)
            AUTH_FAILURES.labels(reason=reason[:80]).inc()
            return _error(
                request,
                status_code=exc.status_code,
                code=reason,
                message="request authorization failed",
                headers=exc.headers,
            )
    started = time.perf_counter()
    try:
        response = await call_next(request)
    except HTTPException as exc:
        response = _error(
            request,
            status_code=exc.status_code,
            code=str(exc.detail),
            message="request could not be completed",
            retryable=exc.status_code >= 500,
        )
    except Exception:
        response = _error(
            request,
            status_code=500,
            code="internal_error",
            message="request could not be completed",
            retryable=True,
        )
    operation = _operation(request)
    metric_method = request.method if request.method in BOUNDED_METHODS else "OTHER"
    REQUESTS.labels(
        operation=operation,
        method=metric_method,
        status_class=f"{response.status_code // 100}xx",
    ).inc()
    DURATION.labels(operation=operation, method=metric_method).observe(
        time.perf_counter() - started
    )
    response.headers["X-Correlation-ID"] = request.state.correlation_id
    response.headers["Cache-Control"] = "no-store"
    return response


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
        "data_class": body.data_class,
        "region": body.region,
        "maximum_cost_micros": body.maximum_cost_micros,
        "retain_output": body.retain_output,
    }
    encoded = json.dumps(
        request,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("utf-8")
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
        middleware_operation_id=row.middleware_operation_id,
        resource_version=row.resource_version,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


async def _event(
    session: AsyncSession,
    row: AIRequestModel,
    *,
    event_type: str,
    previous_status: str | None,
    actor_id: str,
    safe_detail: str | None = None,
) -> None:
    event = AIRequestEventModel(
            tenant_id=row.tenant_id,
            request_id=row.id,
            event_type=event_type,
            previous_status=previous_status,
            new_status=row.status,
            actor_id=actor_id[:160],
            safe_detail=safe_detail[:240] if safe_detail else None,
        )
    session.add(event)
    await session.flush()
    session.add(
        AIEventOutboxModel(
            event_id=event.id,
            topic="codestra.ai.requests",
            payload_json=json.dumps(
                {
                    "event_id": event.id,
                    "event_type": event.event_type,
                    "request_id": str(row.id),
                    "tenant_scope": "protected",
                    "occurred_at": datetime.now(UTC).isoformat(),
                    "safe_detail": event.safe_detail,
                },
                sort_keys=True,
                separators=(",", ":"),
            ),
        )
    )


def _encode_cursor(created_at: datetime, identity: UUID) -> str:
    raw = json.dumps([created_at.isoformat(), str(identity)], separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _decode_cursor(value: str | None) -> tuple[datetime, UUID] | None:
    if not value:
        return None
    try:
        padding = "=" * (-len(value) % 4)
        created_at, identity = json.loads(
            base64.urlsafe_b64decode(value + padding).decode("utf-8")
        )
        return datetime.fromisoformat(created_at), UUID(identity)
    except (ValueError, TypeError, json.JSONDecodeError) as exc:
        raise HTTPException(status_code=400, detail="invalid_cursor") from exc


@app.get("/health/live")
@app.get("/health")
def health() -> dict[str, object]:
    return {
        "status": "ok",
        "service": SERVICE,
        "component": "api",
        "timestamp": datetime.now(UTC).isoformat(),
    }


@app.get("/health/ready", response_model=None)
@app.get("/ready", response_model=None)
async def ready(request: Request, session: AsyncSession = Depends(get_session)):
    try:
        await asyncio.wait_for(session.execute(select(1)), timeout=2.0)
    except Exception:
        return _error(
            request,
            status_code=503,
            code="dependency_unavailable",
            message="required database dependency is unavailable",
            retryable=True,
        )
    return {
        "status": "ready",
        "service": SERVICE,
        "dependencies": {"database": "ready", "configuration": "ready"},
    }


@app.get("/version")
def version() -> dict[str, object]:
    return {
        "service": SERVICE,
        "application_version": API_VERSION,
        "source_sha": SOURCE_SHA,
        "git_sha": SOURCE_SHA,
        "image_digest": IMAGE_DIGEST,
        "environment": APP_ENV,
        "deployment_id": DEPLOYMENT_ID,
        "migration_revision": MIGRATION_REVISION,
        "build_timestamp": BUILD_TIMESTAMP,
    }


@app.get("/metrics")
def metrics() -> Response:
    CAPABILITY.labels(capability="external_model_calls").set(
        1 if EXTERNAL_MODEL_CALLS_ENABLED and EXTERNAL_MODEL_EXECUTION_AVAILABLE else 0
    )
    body, media_type = render_metrics()
    return Response(content=body, media_type=media_type)


@router.get("/capabilities")
def capabilities() -> dict[str, object]:
    provider_enabled = EXTERNAL_MODEL_CALLS_ENABLED and EXTERNAL_MODEL_EXECUTION_AVAILABLE
    return {
        "structured_generation": True,
        "audit": True,
        "usage_accounting": True,
        "cancellation": True,
        "policy_evaluation": True,
        "middleware_operation_tracking": True,
        "business_writes_enabled": False,
        "external_delivery_enabled": provider_enabled,
        "external_model_calls_enabled": provider_enabled,
        "external_model_calls": provider_enabled,
        "read_only_mode": not provider_enabled,
        "telemetry_export": TELEMETRY_EXPORT_ENABLED,
        "business_action_authority": False,
    }


@router.get("/models")
def models() -> dict[str, object]:
    items = [
        {
            "task": task,
            "provider": route.provider.value,
            "model": route.model,
            "external_execution_enabled": (
                EXTERNAL_MODEL_CALLS_ENABLED and EXTERNAL_MODEL_EXECUTION_AVAILABLE
            ),
        }
        for task, route in sorted(ROUTES.items())
    ]
    return {"items": items, "next_cursor": None}


@router.get("/policies")
def policies() -> dict[str, object]:
    return {
        "items": [
            {
                "policy_id": "codestra-ai-governance-v1",
                "allowed_data_classes": ["public", "internal", "confidential"],
                "allowed_regions": ["global"],
                "maximum_input_characters": 20_000,
                "maximum_cost_micros": 10_000_000,
                "provider_credentials_in_service": False,
                "business_action_authority": False,
                "unknown_outcome_requires_reconciliation": True,
            }
        ],
        "next_cursor": None,
    }


@router.post("/generate", response_model=GenerationResponse, status_code=status.HTTP_202_ACCEPTED)
async def generate(
    body: GenerationRequest,
    x_tenant_id: TenantHeader,
    idempotency_key: IdempotencyHeader,
    session: AsyncSession = Depends(get_session),
    x_correlation_id: CorrelationHeader = None,
    request: Request = None,
) -> GenerationResponse:
    tenant_id = _tenant(x_tenant_id, body.tenant_id)
    correlation_id = x_correlation_id or str(uuid4())
    actor_id = (
        request.state.auth.subject
        if request is not None and hasattr(request.state, "auth")
        else "codestra-ai-internal"
    )
    if body.region not in ALLOWED_REGIONS:
        POLICY_DENIALS.labels(reason="region_not_allowed").inc()
        raise HTTPException(status_code=403, detail="region_not_allowed")
    input_digest, fingerprint = _fingerprint(tenant_id, body)
    existing = await session.execute(
        select(AIRequestModel).where(
            AIRequestModel.tenant_id == tenant_id,
            AIRequestModel.idempotency_key == idempotency_key,
        )
    )
    row = existing.scalar_one_or_none()
    provider_enabled = EXTERNAL_MODEL_CALLS_ENABLED and EXTERNAL_MODEL_EXECUTION_AVAILABLE
    if row is not None:
        if row.request_fingerprint != fingerprint:
            IDEMPOTENCY_CONFLICTS.inc()
            raise HTTPException(status_code=409, detail="idempotency_conflict")
        if row.status != "dispatch_pending":
            return _response(row)
    else:
        route = resolve_route(body.task.value)
        row = AIRequestModel(
            id=uuid4(),
            tenant_id=tenant_id,
            task=body.task.value,
            provider=route.provider.value,
            model=route.model,
            status="dispatch_pending" if provider_enabled else "blocked_by_capability",
            input_digest=input_digest,
            idempotency_key=idempotency_key,
            request_fingerprint=fingerprint,
        )
        session.add(row)
        try:
            await session.flush()
            await _event(
                session,
                row,
                event_type="ai.request.accepted",
                previous_status=None,
                actor_id=actor_id,
                safe_detail=(
                    "submitted_for_middleware_dispatch"
                    if provider_enabled
                    else "external_model_calls_disabled"
                ),
            )
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
                IDEMPOTENCY_CONFLICTS.inc()
                raise HTTPException(status_code=409, detail="idempotency_conflict")
            if row.status != "dispatch_pending":
                return _response(row)
        await session.refresh(row)

    if not provider_enabled:
        POLICY_DENIALS.labels(reason="capability_disabled").inc()
        return _response(row)

    try:
        operation = await MiddlewareAIClient().submit(
            {
                "request_id": str(row.id),
                "task": row.task,
                "provider": row.provider,
                "model": row.model,
                "input": body.input,
                "input_digest": row.input_digest,
                "campaign_id": body.campaign_id,
                "data_class": body.data_class,
                "region": body.region,
                "maximum_cost_micros": body.maximum_cost_micros,
                "retain_output": body.retain_output,
            },
            tenant_id=tenant_id,
            correlation_id=correlation_id,
            idempotency_key=idempotency_key,
        )
    except MiddlewareSubmissionError as exc:
        locked_result = await session.execute(
            select(AIRequestModel)
            .where(AIRequestModel.id == row.id, AIRequestModel.tenant_id == tenant_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        row = locked_result.scalar_one()
        previous = row.status
        transition_allowed = row.status == "dispatch_pending" or (
            exc.outcome_unknown and row.middleware_operation_id is None
        )
        if transition_allowed:
            row.status = "reconciliation_required" if exc.outcome_unknown else "failed"
            row.resource_version += 1
        await _event(
            session,
            row,
            event_type=(
                "ai.middleware_submission_failed"
                if transition_allowed
                else "ai.middleware_submission_failed_after_state_change"
            ),
            previous_status=previous,
            actor_id=actor_id,
            safe_detail=exc.code,
        )
        await session.commit()
        await session.refresh(row)
        MIDDLEWARE_RESULTS.labels(
            outcome="unknown" if exc.outcome_unknown else "rejected"
        ).inc()
        if exc.outcome_unknown:
            RECONCILIATION.inc()
        return _response(row)

    locked_result = await session.execute(
        select(AIRequestModel)
        .where(AIRequestModel.id == row.id, AIRequestModel.tenant_id == tenant_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    row = locked_result.scalar_one()
    previous = row.status
    if row.middleware_operation_id is not None and row.status not in {
        "cancelled",
        "cancellation_pending",
    }:
        if row.middleware_operation_id != operation.operation_id:
            row.status = "reconciliation_required"
            row.resource_version += 1
            await _event(
                session,
                row,
                event_type="ai.middleware_idempotency_mismatch",
                previous_status=previous,
                actor_id=actor_id,
                safe_detail="middleware_operation_identity_changed",
            )
            await session.commit()
            await session.refresh(row)
            RECONCILIATION.inc()
        return _response(row)
    row.middleware_operation_id = operation.operation_id
    if row.status in {"cancelled", "cancellation_pending"}:
        row.status = "reconciliation_required"
        event_type = "ai.middleware_operation_accepted_after_cancellation"
        safe_detail = "cancellation_must_be_reconciled"
        RECONCILIATION.inc()
    else:
        row.status = "queued"
        event_type = "ai.middleware_operation_accepted"
        safe_detail = operation.state
    row.resource_version += 1
    await _event(
        session,
        row,
        event_type=event_type,
        previous_status=previous,
        actor_id=actor_id,
        safe_detail=safe_detail,
    )
    await session.commit()
    await session.refresh(row)
    MIDDLEWARE_RESULTS.labels(outcome="accepted").inc()
    return _response(row)


@router.get("/requests", response_model=PagedRequests)
async def list_requests(
    x_tenant_id: TenantHeader,
    limit: int = Query(default=50, ge=1, le=100),
    cursor: str | None = None,
    status_filter: str | None = Query(default=None, alias="status", max_length=32),
    task: TaskType | None = None,
    session: AsyncSession = Depends(get_session),
) -> PagedRequests:
    position = _decode_cursor(cursor)
    statement = select(AIRequestModel).where(AIRequestModel.tenant_id == x_tenant_id)
    if status_filter:
        statement = statement.where(AIRequestModel.status == status_filter)
    if task:
        statement = statement.where(AIRequestModel.task == task.value)
    if position:
        statement = statement.where(
            or_(
                AIRequestModel.created_at < position[0],
                and_(
                    AIRequestModel.created_at == position[0],
                    AIRequestModel.id < position[1],
                ),
            )
        )
    result = await session.execute(
        statement.order_by(AIRequestModel.created_at.desc(), AIRequestModel.id.desc()).limit(limit + 1)
    )
    rows = list(result.scalars().all())
    items = rows[:limit]
    next_cursor = _encode_cursor(items[-1].created_at, items[-1].id) if len(rows) > limit and items else None
    return PagedRequests(items=[_response(row) for row in items], next_cursor=next_cursor)


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


@router.get("/requests/{request_id}/events", response_model=PagedEvents)
async def get_request_events(
    request_id: UUID,
    x_tenant_id: TenantHeader,
    limit: int = Query(default=50, ge=1, le=100),
    cursor: int | None = Query(default=None, ge=1),
    session: AsyncSession = Depends(get_session),
) -> PagedEvents:
    request_row = await session.execute(
        select(AIRequestModel.id).where(
            AIRequestModel.id == request_id,
            AIRequestModel.tenant_id == x_tenant_id,
        )
    )
    if request_row.scalar_one_or_none() is None:
        raise HTTPException(status_code=404, detail="ai_request_not_found")
    statement = select(AIRequestEventModel).where(
        AIRequestEventModel.tenant_id == x_tenant_id,
        AIRequestEventModel.request_id == request_id,
    )
    if cursor is not None:
        statement = statement.where(AIRequestEventModel.id > cursor)
    result = await session.execute(statement.order_by(AIRequestEventModel.id.asc()).limit(limit + 1))
    rows = list(result.scalars().all())
    items = rows[:limit]
    return PagedEvents(
        items=[
            RequestEvent(
                event_id=row.id,
                event_type=row.event_type,
                previous_status=row.previous_status,
                new_status=row.new_status,
                actor_id=row.actor_id,
                safe_detail=row.safe_detail,
                occurred_at=row.occurred_at,
            )
            for row in items
        ],
        next_cursor=str(items[-1].id) if len(rows) > limit and items else None,
    )


@router.post("/requests/{request_id}/cancel", response_model=GenerationResponse)
async def cancel_request(
    request_id: UUID,
    body: CancelRequest,
    x_tenant_id: TenantHeader,
    idempotency_key: IdempotencyHeader,
    session: AsyncSession = Depends(get_session),
    request: Request = None,
) -> GenerationResponse:
    fingerprint = hashlib.sha256(
        json.dumps(body.model_dump(mode="json"), sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    result = await session.execute(
        select(AIRequestModel)
        .where(AIRequestModel.id == request_id, AIRequestModel.tenant_id == x_tenant_id)
        .with_for_update()
    )
    row = result.scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail="ai_request_not_found")
    # Check the mutation ledger only after taking the aggregate lock. This makes
    # simultaneous retries observe the first transaction's committed result.
    replay = await session.execute(
        select(AIRequestMutationModel).where(
            AIRequestMutationModel.tenant_id == x_tenant_id,
            AIRequestMutationModel.request_id == request_id,
            AIRequestMutationModel.mutation_type == "cancel",
            AIRequestMutationModel.idempotency_key == idempotency_key,
        )
    )
    replay_record = replay.scalar_one_or_none()
    if replay_record is not None:
        if replay_record.request_fingerprint != fingerprint:
            IDEMPOTENCY_CONFLICTS.inc()
            raise HTTPException(status_code=409, detail="idempotency_conflict")
        return _response(row)
    if row.resource_version != body.expected_version:
        raise HTTPException(status_code=409, detail="stale_resource_version")
    if row.status in {"completed", "failed", "cancelled"}:
        raise HTTPException(status_code=409, detail="request_not_cancellable")
    previous = row.status
    row.status = "cancellation_pending" if row.middleware_operation_id else "cancelled"
    row.cancelled_at = datetime.now(UTC) if row.status == "cancelled" else None
    row.resource_version += 1
    session.add(
        AIRequestMutationModel(
            tenant_id=x_tenant_id,
            request_id=request_id,
            mutation_type="cancel",
            idempotency_key=idempotency_key,
            request_fingerprint=fingerprint,
            result_version=row.resource_version,
        )
    )
    await _event(
        session,
        row,
        event_type="ai.request.cancellation_requested",
        previous_status=previous,
        actor_id=(
            request.state.auth.subject
            if request is not None and hasattr(request.state, "auth")
            else "codestra-ai-internal"
        ),
        safe_detail=body.reason,
    )
    await session.commit()
    await session.refresh(row)
    return _response(row)


@router.get("/usage")
async def usage(
    x_tenant_id: TenantHeader,
    days: int = Query(default=30, ge=1, le=366),
    session: AsyncSession = Depends(get_session),
) -> dict[str, object]:
    since = datetime.now(UTC) - timedelta(days=days)
    result = await session.execute(
        select(
            func.count(AIRequestModel.id),
            func.coalesce(func.sum(AIRequestModel.input_tokens), 0),
            func.coalesce(func.sum(AIRequestModel.output_tokens), 0),
            func.coalesce(func.sum(AIRequestModel.cost_micros), 0),
        ).where(
            AIRequestModel.tenant_id == x_tenant_id,
            AIRequestModel.created_at >= since,
        )
    )
    count, input_tokens, output_tokens, cost_micros = result.one()
    TOKENS.labels(direction="input").inc(0)
    TOKENS.labels(direction="output").inc(0)
    COST.inc(0)
    return {
        "window_days": days,
        "request_count": int(count),
        "input_tokens": int(input_tokens),
        "output_tokens": int(output_tokens),
        "cost_micros": int(cost_micros),
    }


@app.get("/capabilities", include_in_schema=False)
def capabilities_alias() -> dict[str, object]:
    return capabilities()


app.include_router(router)
