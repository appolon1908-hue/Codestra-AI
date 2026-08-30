DROP INDEX IF EXISTS uq_ai_request_idempotency;
ALTER TABLE ai_requests DROP COLUMN IF EXISTS request_fingerprint;
ALTER TABLE ai_requests DROP COLUMN IF EXISTS idempotency_key;
