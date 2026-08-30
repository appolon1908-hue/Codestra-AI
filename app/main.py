from enum import StrEnum
from uuid import UUID, uuid4

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

app = FastAPI(title="Codestra AI Gateway", version="0.1.0")

EXTERNAL_MODEL_CALLS_ENABLED = False

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

@app.get("/health")
def health() -> dict[str, object]:
    return {"status": "ok", "external_model_calls_enabled": EXTERNAL_MODEL_CALLS_ENABLED}

@app.get("/v1/capabilities")
def capabilities() -> dict[str, object]:
    return {
        "structured_generation": True,
        "audit": True,
        "usage_accounting": True,
        "external_model_calls": EXTERNAL_MODEL_CALLS_ENABLED,
        "business_action_authority": False,
    }

@app.post("/v1/generate", response_model=GenerationResponse)
def generate(body: GenerationRequest) -> GenerationResponse:
    request_id = uuid4()
    if not EXTERNAL_MODEL_CALLS_ENABLED:
        return GenerationResponse(request_id=request_id, status="blocked_by_capability")
    raise HTTPException(status_code=501, detail="provider_router_not_implemented")
