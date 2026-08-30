ALTER TABLE ai_requests ADD COLUMN IF NOT EXISTS idempotency_key varchar(200);
ALTER TABLE ai_requests ADD COLUMN IF NOT EXISTS request_fingerprint varchar(64);
UPDATE ai_requests
SET idempotency_key = COALESCE(idempotency_key, 'legacy:' || id::text),
    request_fingerprint = COALESCE(request_fingerprint, repeat('0', 64));
ALTER TABLE ai_requests ALTER COLUMN idempotency_key SET NOT NULL;
ALTER TABLE ai_requests ALTER COLUMN request_fingerprint SET NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS uq_ai_request_idempotency
  ON ai_requests(tenant_id, idempotency_key);
