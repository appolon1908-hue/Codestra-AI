import hashlib
import os
from enum import StrEnum
from uuid import UUID, uuid4

from fastapi import Depends, FastAPI, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from .db import get_session
from .models import AIRequestModel
from .providers.router import resolve_route

app = FastAPI(title="Codestra AI Gateway", version="0.2.0")
EXTERNAL_MODEL_CALLS_ENABLED = os.getenv("EXTERNAL_MODEL_CALLS_ENABLED", "false").lower() == "true"

class TaskType(StrEnum):
    COPY = "copy"
    CLASSIFY = "classify"
    SUMMARIZE = "summarize"
    SCORE = "score"
    CREATIVE_BRIEF = "creative_brief"

class GenerationRequest(BaseModel):
    task: TaskType
    input: str = Field(min_length=1, max_length=20000)
    tenant_id: str = Field(min_length=1, max_length=128)
    campaign_id: str | None = None

class GenerationResponse(BaseModel):
    request_id: UUID
    status: str
    output: str | None = None
    provider: str | None = None
    model: str | None = None

@app.get("/health")
def health() -> dict[str, object]:
    return {"status": "ok", "external_model_calls_enabled": EXTERNAL_MODEL_CALLS_ENABLED}

@app.get("/v1/capabilities")
def capabilities() -> dict[str, object]:
    return {"structured_generation": True, "audit": True, "usage_accounting": True, "external_model_calls": EXTERNAL_MODEL_CALLS_ENABLED, "business_action_authority": False}

@app.post("/v1/generate", response_model=GenerationResponse)
async def generate(body: GenerationRequest, session: AsyncSession = Depends(get_session)) -> GenerationResponse:
    route = resolve_route(body.task.value)
    request_id = uuid4()
    digest = hashlib.sha256(body.input.encode("utf-8")).hexdigest()
    row = AIRequestModel(id=request_id, tenant_id=body.tenant_id, task=body.task.value, provider=route.provider.value, model=route.model, status="blocked_by_capability" if not EXTERNAL_MODEL_CALLS_ENABLED else "queued", input_digest=digest)
    session.add(row)
    await session.commit()
    if not EXTERNAL_MODEL_CALLS_ENABLED:
        return GenerationResponse(request_id=request_id, status="blocked_by_capability", provider=route.provider.value, model=route.model)
    raise HTTPException(status_code=501, detail="external_provider_execution_not_implemented")
