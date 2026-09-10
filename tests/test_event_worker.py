from __future__ import annotations

import pytest

from app import event_worker


@pytest.mark.parametrize(
    "value",
    [
        "redis://cache.example:6379/0",
        "http://cache.example/0",
        "redis://localhost.attacker.example:6379/0",
        "",
    ],
)
def test_event_worker_rejects_unencrypted_remote_or_malformed_redis_urls(
    monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    monkeypatch.setenv("EVENT_REDIS_URL", value)
    with pytest.raises(RuntimeError, match="event_redis_url_invalid"):
        event_worker._redis_url()


@pytest.mark.parametrize(
    "value",
    ["rediss://cache.example:6380/0", "redis://127.0.0.1:6379/0", "redis://localhost:6379/0"],
)
def test_event_worker_accepts_tls_remote_or_exact_loopback_urls(
    monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    monkeypatch.setenv("EVENT_REDIS_URL", value)
    assert event_worker._redis_url() == value
