# Stage 5 integration certification

The AI service now exposes the contracted `/v1/ai` API prefix used by the SDK and Kong. Every request is bound to `X-Tenant-ID`, and an optional body tenant must match it. Generation requires `Idempotency-Key`; semantic reuse returns the original durable request while conflicting reuse fails with HTTP 409.

Only an SHA-256 digest of model input is stored. The audit row records provider/model route, status, token counts and cost fields without persisting raw prompt content. Request status reads query by both request ID and tenant.

The PostgreSQL certification applies both migrations, proves the tenant/idempotency controls, rolls back to no table, reapplies, and runs real duplicate/conflict/cross-tenant tests.

`EXTERNAL_MODEL_CALLS_ENABLED` remains false. The Stage 5 code records a durable `blocked_by_capability` result and makes no provider call. Provider credentials and live model execution require a later protected activation stage.
