#!/usr/bin/env python3
from __future__ import annotations

import asyncio
import os
from pathlib import Path

import asyncpg

ROOT = Path(__file__).resolve().parents[1]
UP = [
    ROOT / "migrations/001_stage4.sql",
    ROOT / "migrations/002_stage5.sql",
    ROOT / "migrations/003_api_completion.sql",
]
DOWN = [
    ROOT / "migrations/003_api_completion.down.sql",
    ROOT / "migrations/002_stage5.down.sql",
    ROOT / "migrations/001_stage4.down.sql",
]


def dsn() -> str:
    return (os.environ.get("POSTGRES_DSN") or os.environ.get("DATABASE_URL", "")).replace(
        "postgresql+asyncpg://", "postgresql://", 1
    )


async def run_files(conn: asyncpg.Connection, paths: list[Path]) -> None:
    for path in paths:
        await conn.execute(path.read_text(encoding="utf-8"))


async def main() -> None:
    if not dsn():
        raise SystemExit("POSTGRES_DSN or DATABASE_URL is required")
    conn = await asyncpg.connect(dsn())
    try:
        await conn.execute((ROOT / "migrations/001_stage4.down.sql").read_text(encoding="utf-8"))
        await run_files(conn, UP)
        assert await conn.fetchval("SELECT to_regclass('public.ai_requests')") == "ai_requests"
        assert await conn.fetchval("SELECT to_regclass('public.ai_request_events')") == "ai_request_events"
        assert await conn.fetchval("SELECT to_regclass('public.ai_request_mutations')") == "ai_request_mutations"
        assert await conn.fetchval("SELECT to_regclass('public.ai_event_outbox')") == "ai_event_outbox"
        assert await conn.fetchval("SELECT to_regclass('public.ai_control_outbox')") == "ai_control_outbox"
        assert await conn.fetchval(
            "SELECT count(*) FROM pg_indexes WHERE indexname='uq_ai_request_idempotency'"
        ) == 1
        await run_files(conn, DOWN)
        assert await conn.fetchval("SELECT to_regclass('public.ai_requests')") is None
        assert await conn.fetchval("SELECT to_regclass('public.ai_request_events')") is None
        assert await conn.fetchval("SELECT to_regclass('public.ai_request_mutations')") is None
        assert await conn.fetchval("SELECT to_regclass('public.ai_event_outbox')") is None
        assert await conn.fetchval("SELECT to_regclass('public.ai_control_outbox')") is None
        await run_files(conn, UP)
    finally:
        await conn.close()
    print("AI_API_POSTGRES_CERTIFICATION=PASS")


if __name__ == "__main__":
    asyncio.run(main())
