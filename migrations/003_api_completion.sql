ALTER TABLE ai_requests
    ADD COLUMN IF NOT EXISTS middleware_operation_id varchar(128),
    ADD COLUMN IF NOT EXISTS resource_version integer NOT NULL DEFAULT 1,
    ADD COLUMN IF NOT EXISTS cancelled_at timestamptz,
    ADD COLUMN IF NOT EXISTS updated_at timestamptz NOT NULL DEFAULT now();

CREATE INDEX IF NOT EXISTS ix_ai_requests_middleware_operation_id
    ON ai_requests (middleware_operation_id)
    WHERE middleware_operation_id IS NOT NULL;

CREATE UNIQUE INDEX IF NOT EXISTS uq_ai_request_tenant_id
    ON ai_requests (tenant_id, id);

CREATE TABLE IF NOT EXISTS ai_request_events (
    id bigserial PRIMARY KEY,
    tenant_id varchar(128) NOT NULL,
    request_id uuid NOT NULL,
    event_type varchar(80) NOT NULL,
    previous_status varchar(32),
    new_status varchar(32) NOT NULL,
    actor_id varchar(160) NOT NULL,
    safe_detail varchar(240),
    occurred_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT fk_ai_request_event_request
      FOREIGN KEY (tenant_id, request_id)
      REFERENCES ai_requests (tenant_id, id)
      ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS ix_ai_request_events_tenant_request_id
    ON ai_request_events (tenant_id, request_id, id);

CREATE TABLE IF NOT EXISTS ai_request_mutations (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id varchar(128) NOT NULL,
    request_id uuid NOT NULL,
    mutation_type varchar(48) NOT NULL,
    idempotency_key varchar(200) NOT NULL,
    request_fingerprint varchar(64) NOT NULL,
    result_version integer NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT fk_ai_request_mutation_request
      FOREIGN KEY (tenant_id, request_id)
      REFERENCES ai_requests (tenant_id, id)
      ON DELETE CASCADE,
    CONSTRAINT uq_ai_request_mutation_idempotency
      UNIQUE (tenant_id, request_id, mutation_type, idempotency_key)
);

CREATE INDEX IF NOT EXISTS ix_ai_request_mutations_request
    ON ai_request_mutations (tenant_id, request_id, created_at);

CREATE TABLE IF NOT EXISTS ai_event_outbox (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    event_id bigint NOT NULL UNIQUE REFERENCES ai_request_events(id) ON DELETE CASCADE,
    topic varchar(120) NOT NULL,
    payload_json text NOT NULL,
    state varchar(32) NOT NULL DEFAULT 'pending',
    attempts integer NOT NULL DEFAULT 0 CHECK (attempts >= 0),
    available_at timestamptz NOT NULL DEFAULT now(),
    lease_until timestamptz,
    last_error_code varchar(80),
    published_at timestamptz,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS ix_ai_event_outbox_claim
    ON ai_event_outbox (state, available_at, lease_until, created_at);

CREATE TABLE IF NOT EXISTS ai_control_outbox (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id varchar(128) NOT NULL,
    request_id uuid NOT NULL,
    mutation_id uuid NOT NULL UNIQUE REFERENCES ai_request_mutations(id) ON DELETE CASCADE,
    action varchar(48) NOT NULL,
    payload_json text NOT NULL,
    state varchar(32) NOT NULL DEFAULT 'pending',
    attempts integer NOT NULL DEFAULT 0 CHECK (attempts >= 0),
    available_at timestamptz NOT NULL DEFAULT now(),
    lease_until timestamptz,
    last_error_code varchar(80),
    completed_at timestamptz,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT fk_ai_control_outbox_request FOREIGN KEY (tenant_id, request_id)
      REFERENCES ai_requests (tenant_id, id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS ix_ai_control_outbox_claim
    ON ai_control_outbox (state, available_at, lease_until, created_at);
