from app.models import AIRequestModel


def test_ai_audit_records_have_a_required_tenant_boundary():
    tenant = AIRequestModel.__table__.columns["tenant_id"]
    assert tenant.nullable is False
    assert "idempotency_key" in AIRequestModel.__table__.columns
    assert "request_fingerprint" in AIRequestModel.__table__.columns
