from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from urllib.parse import urlsplit
from uuid import UUID

from redis.asyncio import Redis
from sqlalchemy import and_, func, or_, select

from .db import SessionLocal
from .metrics import EVENT_OUTBOX_DEPTH, EVENT_PUBLICATIONS
from .models import AIEventOutboxModel


UTC = timezone.utc


@dataclass(frozen=True)
class ClaimedEvent:
    id: UUID
    event_id: int
    topic: str
    payload_json: str
    attempts: int


def _redis_url() -> str:
    value = os.getenv("EVENT_REDIS_URL", "").strip()
    parsed = urlsplit(value)
    loopback = parsed.hostname in {"127.0.0.1", "::1", "localhost"}
    if not parsed.hostname or parsed.scheme not in ({"redis", "rediss"} if loopback else {"rediss"}):
        raise RuntimeError("event_redis_url_invalid")
    return value


async def claim(batch_size: int, lease_seconds: int) -> list[ClaimedEvent]:
    now = datetime.now(UTC)
    async with SessionLocal() as session:
        result = await session.execute(
            select(AIEventOutboxModel)
            .where(
                or_(
                    and_(AIEventOutboxModel.state == "pending", AIEventOutboxModel.available_at <= now),
                    and_(AIEventOutboxModel.state == "publishing", AIEventOutboxModel.lease_until < now),
                )
            )
            .order_by(AIEventOutboxModel.created_at.asc())
            .limit(batch_size)
            .with_for_update(skip_locked=True)
        )
        rows = list(result.scalars())
        for row in rows:
            row.state = "publishing"
            row.attempts += 1
            row.lease_until = now + timedelta(seconds=lease_seconds)
            row.last_error_code = None
        await session.commit()
        return [ClaimedEvent(row.id, row.event_id, row.topic, row.payload_json, row.attempts) for row in rows]


async def acknowledge(identity: UUID) -> None:
    async with SessionLocal() as session:
        row = await session.scalar(
            select(AIEventOutboxModel).where(AIEventOutboxModel.id == identity).with_for_update()
        )
        if row is None or row.state != "publishing":
            return
        row.state = "published"
        row.published_at = datetime.now(UTC)
        row.lease_until = None
        await session.commit()


async def reject(identity: UUID, attempts: int, max_attempts: int) -> None:
    async with SessionLocal() as session:
        row = await session.scalar(
            select(AIEventOutboxModel).where(AIEventOutboxModel.id == identity).with_for_update()
        )
        if row is None or row.state != "publishing":
            return
        row.state = "dead_letter" if attempts >= max_attempts else "pending"
        row.available_at = datetime.now(UTC) + timedelta(seconds=min(2 ** min(attempts, 8), 300))
        row.lease_until = None
        row.last_error_code = "event_publish_failed"
        await session.commit()


async def pending_depth() -> int:
    async with SessionLocal() as session:
        value = await session.scalar(
            select(func.count()).select_from(AIEventOutboxModel).where(
                AIEventOutboxModel.state.in_(("pending", "publishing"))
            )
        )
        return int(value or 0)


async def run_once(redis: Redis, *, batch_size: int, lease_seconds: int, max_attempts: int) -> int:
    events = await claim(batch_size, lease_seconds)
    for event in events:
        try:
            await redis.xadd(
                event.topic,
                {"event_id": str(event.event_id), "payload": event.payload_json},
            )
        except Exception:
            await reject(event.id, event.attempts, max_attempts)
            EVENT_PUBLICATIONS.labels(outcome="retry" if event.attempts < max_attempts else "dead_letter").inc()
        else:
            await acknowledge(event.id)
            EVENT_PUBLICATIONS.labels(outcome="published").inc()
    EVENT_OUTBOX_DEPTH.set(await pending_depth())
    return len(events)


async def main() -> None:
    batch_size = max(1, min(int(os.getenv("EVENT_OUTBOX_BATCH_SIZE", "50")), 200))
    lease_seconds = max(5, min(int(os.getenv("EVENT_OUTBOX_LEASE_SECONDS", "30")), 300))
    max_attempts = max(1, min(int(os.getenv("EVENT_OUTBOX_MAX_ATTEMPTS", "8")), 32))
    poll_seconds = max(0.1, min(float(os.getenv("EVENT_OUTBOX_POLL_SECONDS", "1")), 30.0))
    redis = Redis.from_url(_redis_url(), decode_responses=True, socket_timeout=5, socket_connect_timeout=5)
    try:
        while True:
            processed = await run_once(
                redis,
                batch_size=batch_size,
                lease_seconds=lease_seconds,
                max_attempts=max_attempts,
            )
            if processed == 0:
                await asyncio.sleep(poll_seconds)
    finally:
        await redis.aclose()


if __name__ == "__main__":
    asyncio.run(main())
