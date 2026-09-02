import json
import logging
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.exc import IntegrityError

import app.main as main_module
from app.main import (
    EXTERNAL_MODEL_CALLS_ENABLED,
    TELEMETRY_EXPORT_ENABLED,
    GenerationRequest,
    TaskType,
    _fingerprint,
    app,
    capabilities,
    generate,
)
from app.telemetry import (
    audit_event,
    correlation_id_context,
    install_correlation_middleware,
    private_otlp_endpoint,
)


def test_telemetry_is_default_off_and_does_not_enable_provider_effects():
    assert TELEMETRY_EXPORT_ENABLED is False
    assert EXTERNAL_MODEL_CALLS_ENABLED is False
    assert capabilities()["telemetry_export"] is False
    assert capabilities()["correlation_ids"] is True


@pytest.mark.parametrize(
    "endpoint",
    (
        "http://alloy:4318",
        "http://alloy.monitoring.svc:4318",
        "http://127.0.0.1:4318",
        "https://10.20.30.40:4318/v1/traces",
    ),
)
def test_private_otlp_authorities_are_accepted(endpoint):
    assert private_otlp_endpoint(endpoint) == endpoint


@pytest.mark.parametrize(
    "endpoint",
    (
        "https://telemetry.example.com:4318",
        "https://user:secret@alloy:4318",
        "file:///tmp/traces",
        "alloy:4318",
        "https://alloy:4318?token=secret",
    ),
)
def test_external_or_credential_bearing_otlp_authorities_are_rejected(endpoint):
    with pytest.raises(RuntimeError):
        private_otlp_endpoint(endpoint)


def test_correlation_id_is_preserved_or_generated_and_invalid_values_fail_closed():
    test_app = FastAPI()
    install_correlation_middleware(test_app)

    @test_app.get("/")
    def root():
        return {"correlation_id": correlation_id_context.get()}

    client = TestClient(test_app)
    supplied = "order:018f4f7a-1234"
    response = client.get("/", headers={"X-Correlation-ID": supplied})
    assert response.status_code == 200
    assert response.headers["X-Correlation-ID"] == supplied
    assert response.json() == {"correlation_id": supplied}

    generated = client.get("/")
    assert generated.status_code == 200
    assert generated.headers["X-Correlation-ID"] == generated.json()["correlation_id"]

    rejected = client.get("/", headers={"X-Correlation-ID": "secret value invalid"})
    assert rejected.status_code == 400
    assert rejected.json() == {"detail": "invalid_correlation_id"}


def test_audit_log_contains_only_explicit_sanitized_fields(caplog):
    original_propagate = logging.getLogger("codestra.ai.audit").propagate
    logging.getLogger("codestra.ai.audit").propagate = True
    token = correlation_id_context.set("corr-123")
    try:
        with caplog.at_level(logging.INFO, logger="codestra.ai.audit"):
            audit_event("ai_request_recorded", request_id="request-123", status="blocked_by_capability")
    finally:
        correlation_id_context.reset(token)
        logging.getLogger("codestra.ai.audit").propagate = original_propagate
    record = json.loads(caplog.records[-1].message)
    assert record == {
        "correlation_id": "corr-123",
        "event": "ai_request_recorded",
        "request_id": "request-123",
        "service": "codestra-ai",
        "status": "blocked_by_capability",
    }
    assert "authorization" not in caplog.records[-1].message.lower()
    assert "input" not in record


def test_audit_logger_is_enabled_without_uvicorn_logger_configuration():
    logger = logging.getLogger("codestra.ai.audit")
    assert logger.level == logging.INFO
    assert logger.propagate is False
    assert logger.handlers


def test_unhandled_errors_return_a_correlation_id():
    test_app = FastAPI()
    install_correlation_middleware(test_app)

    @test_app.get("/failure")
    def failure():
        raise RuntimeError("sensitive failure detail")

    response = TestClient(test_app, raise_server_exceptions=False).get(
        "/failure", headers={"X-Correlation-ID": "failure-correlation"}
    )
    assert response.status_code == 500
    assert response.headers["X-Correlation-ID"] == "failure-correlation"
    assert response.json() == {"detail": "internal_server_error"}


@pytest.mark.asyncio
async def test_integrity_race_replay_is_audited(monkeypatch):
    tenant_id = "tenant-race"
    body = GenerationRequest(task=TaskType.SUMMARIZE, input="safe input")
    _, fingerprint = _fingerprint(tenant_id, body)
    winning_row = SimpleNamespace(
        id=uuid4(),
        status="blocked_by_capability",
        request_fingerprint=fingerprint,
        output_text=None,
        provider="rules",
        model="safe-default",
        input_tokens=0,
        output_tokens=0,
        cost_micros=0,
    )

    class Result:
        def __init__(self, row):
            self.row = row

        def scalar_one_or_none(self):
            return self.row

    class RacingSession:
        def __init__(self):
            self.results = iter((Result(None), Result(winning_row)))

        async def execute(self, _query):
            return next(self.results)

        def add(self, _row):
            return None

        async def commit(self):
            raise IntegrityError("duplicate", {}, RuntimeError("unique violation"))

        async def rollback(self):
            return None

    events = []
    monkeypatch.setattr(main_module, "audit_event", lambda event, **fields: events.append((event, fields)))

    response = await generate(body, tenant_id, "idempotency-race", RacingSession())

    assert response.request_id == winning_row.id
    assert events == [
        (
            "ai_request_replayed",
            {"request_id": str(winning_row.id), "status": "blocked_by_capability"},
        )
    ]


def test_existing_application_exposes_correlation_header_without_business_writes():
    response = TestClient(app).get("/health")
    assert response.status_code == 200
    assert response.headers["X-Correlation-ID"]
    assert response.json()["external_model_calls_enabled"] is False
