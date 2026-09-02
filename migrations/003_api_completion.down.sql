DROP TABLE IF EXISTS ai_request_events;
DROP INDEX IF EXISTS ix_ai_requests_middleware_operation_id;
DROP INDEX IF EXISTS uq_ai_request_tenant_id;
ALTER TABLE ai_requests
    DROP COLUMN IF EXISTS middleware_operation_id,
    DROP COLUMN IF EXISTS resource_version,
    DROP COLUMN IF EXISTS cancelled_at,
    DROP COLUMN IF EXISTS updated_at;
