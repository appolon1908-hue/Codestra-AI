#!/usr/bin/env python3
from __future__ import annotations

from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]


def operations(document: dict) -> set[tuple[str, str]]:
    result: set[tuple[str, str]] = set()
    for path, item in document["paths"].items():
        for method in item:
            if method.lower() in {"get", "post", "put", "patch", "delete"}:
                result.add((method.upper(), path))
    return result


def main() -> None:
    from app.main import EXTERNAL_MODEL_CALLS_ENABLED, app

    source = yaml.safe_load((ROOT / "contracts/openapi.v1.yaml").read_text())
    committed = operations(source)
    runtime = operations(app.openapi())
    aliases = {("GET", "/health"), ("GET", "/ready")}
    assert not committed - runtime, sorted(committed - runtime)
    assert not runtime - committed - aliases, sorted(runtime - committed - aliases)
    for method, path in committed:
        assert source["paths"][path][method.lower()].get("operationId")

    assert EXTERNAL_MODEL_CALLS_ENABLED is False
    assert ("POST", "/v1/crm/leads") not in committed
    assert ("POST", "/v1/workflow/runs") not in committed

    events = yaml.safe_load((ROOT / "contracts/events.asyncapi.v1.yaml").read_text())
    assert events["asyncapi"] == "3.0.0"

    forbidden = {
        "tenant_id", "customer_id", "user_id", "email", "phone",
        "request_id", "correlation_id", "trace_id", "span_id",
        "token", "secret",
    }
    metrics_text = (ROOT / "app/metrics.py").read_text()
    for label in forbidden:
        assert f'"{label}"' not in metrics_text

    print("CODESTRA_AI_API_COMPLETION=PASS")
    print(f"operations={len(committed)}")


if __name__ == "__main__":
    main()
