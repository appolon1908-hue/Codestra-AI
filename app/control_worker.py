from __future__ import annotations

import asyncio
import json
import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from uuid import UUID

from sqlalchemy import and_, or_, select

from .db import SessionLocal
from .middleware_client import MiddlewareAIClient, MiddlewareSubmissionError
from .models import AIControlOutboxModel, AIEventOutboxModel, AIRequestEventModel, AIRequestModel

UTC = timezone.utc


@dataclass(frozen=True)
class Claim:
    id: UUID
    payload: dict[str, str]
    attempts: int


async def claim_one(lease_seconds: int, *, session_factory=SessionLocal) -> Claim | None:
    now = datetime.now(UTC)
    async with session_factory() as session:
        row = await session.scalar(
            select(AIControlOutboxModel)
            .where(
                or_(
                    and_(AIControlOutboxModel.state == "pending", AIControlOutboxModel.available_at <= now),
                    and_(AIControlOutboxModel.state == "processing", AIControlOutboxModel.lease_until < now),
                )
            )
            .order_by(AIControlOutboxModel.created_at)
            .limit(1)
            .with_for_update(skip_locked=True)
        )
        if row is None:
            return None
        row.state = "processing"
        row.attempts += 1
        row.lease_until = now + timedelta(seconds=lease_seconds)
        await session.commit()
        return Claim(row.id, json.loads(row.payload_json), row.attempts)


async def _record_event(session, request: AIRequestModel, kind: str, previous: str, detail: str) -> None:
    event = AIRequestEventModel(
        tenant_id=request.tenant_id,
        request_id=request.id,
        event_type=kind,
        previous_status=previous,
        new_status=request.status,
        actor_id="codestra-ai-control-worker",
        safe_detail=detail,
    )
    session.add(event)
    await session.flush()
    session.add(
        AIEventOutboxModel(
            event_id=event.id,
            topic="codestra.ai.requests",
            payload_json=json.dumps(
                {
                    "event_id": event.id,
                    "event_type": kind,
                    "request_id": str(request.id),
                    "tenant_scope": "protected",
                    "occurred_at": datetime.now(UTC).isoformat(),
                    "safe_detail": detail,
                },
                sort_keys=True,
                separators=(",", ":"),
            ),
        )
    )


async def complete(claim: Claim, *, session_factory=SessionLocal) -> None:
    async with session_factory() as session:
        row = await session.scalar(
            select(AIControlOutboxModel).where(AIControlOutboxModel.id == claim.id).with_for_update()
        )
        if row is None or row.state != "processing":
            return
        request = await session.scalar(
            select(AIRequestModel).where(
                AIRequestModel.id == row.request_id, AIRequestModel.tenant_id == row.tenant_id
            ).with_for_update()
        )
        if request is None:
            return
        previous = request.status
        if previous == "cancellation_pending":
            request.status = "cancelled"
            request.cancelled_at = datetime.now(UTC)
            request.resource_version += 1
        row.state = "completed"
        row.completed_at = datetime.now(UTC)
        row.lease_until = None
        await _record_event(session, request, "ai.middleware_cancellation_accepted", previous, "cancelled")
        await session.commit()


async def fail(
    claim: Claim, error: MiddlewareSubmissionError, max_attempts: int, *, session_factory=SessionLocal
) -> None:
    async with session_factory() as session:
        row = await session.scalar(
            select(AIControlOutboxModel).where(AIControlOutboxModel.id == claim.id).with_for_update()
        )
        if row is None or row.state != "processing":
            return
        request = await session.scalar(
            select(AIRequestModel).where(
                AIRequestModel.id == row.request_id, AIRequestModel.tenant_id == row.tenant_id
            ).with_for_update()
        )
        if request is None:
            return
        terminal = not error.outcome_unknown or claim.attempts >= max_attempts
        row.state = "dead_letter" if terminal else "pending"
        row.available_at = datetime.now(UTC) + timedelta(seconds=min(2 ** min(claim.attempts, 8), 300))
        row.lease_until = None
        row.last_error_code = error.code[:80]
        if terminal:
            previous = request.status
            request.status = "reconciliation_required"
            request.resource_version += 1
            await _record_event(session, request, "ai.middleware_cancellation_failed", previous, error.code[:80])
        await session.commit()


async def run_once(client: MiddlewareAIClient, *, lease_seconds: int, max_attempts: int, session_factory=SessionLocal) -> bool:
    item = await claim_one(lease_seconds, session_factory=session_factory)
    if item is None:
        return False
    payload = item.payload
    try:
        await client.cancel(
            payload["middleware_operation_id"],
            request_id=payload["request_id"],
            tenant_id=payload["tenant_id"],
            correlation_id=payload["correlation_id"],
            idempotency_key=payload["idempotency_key"],
            reason=payload["reason"],
        )
    except MiddlewareSubmissionError as exc:
        await fail(item, exc, max_attempts, session_factory=session_factory)
    else:
        await complete(item, session_factory=session_factory)
    return True


async def main() -> None:
    client = MiddlewareAIClient()
    lease = max(5, min(int(os.getenv("CONTROL_OUTBOX_LEASE_SECONDS", "30")), 300))
    attempts = max(1, min(int(os.getenv("CONTROL_OUTBOX_MAX_ATTEMPTS", "8")), 32))
    poll = max(0.1, min(float(os.getenv("CONTROL_OUTBOX_POLL_SECONDS", "1")), 30.0))
    while True:
        if not await run_once(client, lease_seconds=lease, max_attempts=attempts):
            await asyncio.sleep(poll)


if __name__ == "__main__":
    asyncio.run(main())
