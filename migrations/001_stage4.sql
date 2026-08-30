CREATE EXTENSION IF NOT EXISTS pgcrypto;
CREATE TABLE IF NOT EXISTS ai_requests (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id varchar(128) NOT NULL,
  task varchar(64) NOT NULL,
  provider varchar(64),
  model varchar(128),
  status varchar(32) NOT NULL,
  input_digest varchar(64) NOT NULL,
  output_text text,
  input_tokens bigint NOT NULL DEFAULT 0,
  output_tokens bigint NOT NULL DEFAULT 0,
  cost_micros bigint NOT NULL DEFAULT 0,
  created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS ix_ai_requests_tenant_created ON ai_requests(tenant_id, created_at DESC);
CREATE INDEX IF NOT EXISTS ix_ai_requests_task_status ON ai_requests(task, status);
