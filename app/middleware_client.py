from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx


class MiddlewareSubmissionError(RuntimeError):
    def __init__(self, code: str, *, outcome_unknown: bool = False) -> None:
        super().__init__(code)
        self.code = code
        self.outcome_unknown = outcome_unknown


@dataclass(frozen=True)
class MiddlewareOperation:
    operation_id: str
    state: str
    status_url: str | None


class MiddlewareAIClient:
    def __init__(self) -> None:
        self.base_url = os.getenv("MIDDLEWARE_BASE_URL", "").rstrip("/")
        self.token_file = os.getenv("MIDDLEWARE_TOKEN_FILE", "")
        self.timeout = float(os.getenv("MIDDLEWARE_TIMEOUT_SECONDS", "5"))

    def _token(self) -> str:
        if not self.token_file:
            raise MiddlewareSubmissionError("middleware_token_file_missing")
        path = Path(self.token_file)
        if not path.is_file() or path.is_symlink():
            raise MiddlewareSubmissionError("middleware_token_file_invalid")
        token = path.read_text(encoding="utf-8").strip()
        if not token:
            raise MiddlewareSubmissionError("middleware_token_empty")
        return token

    async def submit(
        self,
        payload: dict[str, Any],
        *,
        tenant_id: str,
        correlation_id: str,
        idempotency_key: str,
    ) -> MiddlewareOperation:
        if not self.base_url.startswith(("https://", "http://127.0.0.1", "http://localhost")):
            raise MiddlewareSubmissionError("middleware_base_url_invalid")
        headers = {
            "Authorization": f"Bearer {self._token()}",
            "X-Tenant-ID": tenant_id,
            "X-Correlation-ID": correlation_id,
            "Idempotency-Key": idempotency_key,
            "Content-Type": "application/json",
        }
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                response = await client.post(
                    f"{self.base_url}/api/v1/control/ai/inference-requests",
                    headers=headers,
                    json=payload,
                )
        except (httpx.TimeoutException, httpx.NetworkError) as exc:
            raise MiddlewareSubmissionError(
                "middleware_submission_outcome_unknown",
                outcome_unknown=True,
            ) from exc
        if response.status_code not in {200, 202}:
            raise MiddlewareSubmissionError(
                f"middleware_rejected_{response.status_code}",
                outcome_unknown=response.status_code >= 500,
            )
        try:
            document = response.json()
            operation_id = str(document["operation_id"])
            state = str(document["state"])
        except (ValueError, KeyError, TypeError) as exc:
            raise MiddlewareSubmissionError(
                "middleware_response_invalid",
                outcome_unknown=True,
            ) from exc
        return MiddlewareOperation(
            operation_id=operation_id,
            state=state,
            status_url=(
                str(document.get("status_url"))
                if document.get("status_url")
                else None
            ),
        )
