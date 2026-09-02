from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass
from functools import lru_cache
from typing import Any

import jwt
from fastapi import HTTPException, Request, status
from jwt import PyJWKClient
from jwt.exceptions import InvalidTokenError

APP_ENV = os.getenv("CODESTRA_ENVIRONMENT", os.getenv("APP_ENV", "development")).strip().lower()
ALLOW_DEV_AUTH_BYPASS = (
    os.getenv("ALLOW_DEV_AUTH_BYPASS", "true" if APP_ENV in {"development", "test"} else "false")
    .strip()
    .lower()
    == "true"
)
KEYCLOAK_ISSUER = os.getenv(
    "KEYCLOAK_ISSUER",
    "https://auth.codestra.co/realms/codestra",
).rstrip("/")
KEYCLOAK_AUDIENCE = os.getenv("KEYCLOAK_AUDIENCE", "codestra-ai")
KEYCLOAK_JWKS_URL = os.getenv(
    "KEYCLOAK_JWKS_URL",
    f"{KEYCLOAK_ISSUER}/protocol/openid-connect/certs",
)

if APP_ENV in {"staging", "production"} and ALLOW_DEV_AUTH_BYPASS:
    raise RuntimeError("ALLOW_DEV_AUTH_BYPASS must be false in staging and production")


@dataclass(frozen=True)
class AuthContext:
    subject: str
    client_id: str
    scopes: frozenset[str]
    tenants: frozenset[str]
    claims: dict[str, Any]


@lru_cache(maxsize=1)
def _jwk_client() -> PyJWKClient:
    return PyJWKClient(KEYCLOAK_JWKS_URL, cache_keys=True, lifespan=300)


def _scope_set(claims: dict[str, Any]) -> frozenset[str]:
    raw = claims.get("scope", claims.get("scp", ""))
    if isinstance(raw, str):
        return frozenset(part for part in raw.split() if part)
    if isinstance(raw, list):
        return frozenset(str(part) for part in raw if part)
    return frozenset()


def _tenant_set(claims: dict[str, Any]) -> frozenset[str]:
    values: set[str] = set()
    single = claims.get("tenant_id")
    if isinstance(single, str) and single:
        values.add(single)
    multiple = claims.get("tenants")
    if isinstance(multiple, list):
        values.update(str(item) for item in multiple if item)
    return frozenset(values)


async def _decode(token: str) -> dict[str, Any]:
    try:
        signing_key = await asyncio.to_thread(
            _jwk_client().get_signing_key_from_jwt,
            token,
        )
        claims = jwt.decode(
            token,
            signing_key.key,
            algorithms=["RS256", "ES256"],
            audience=KEYCLOAK_AUDIENCE,
            issuer=KEYCLOAK_ISSUER,
            options={"require": ["exp", "iat", "iss", "sub"]},
            leeway=30,
        )
    except (InvalidTokenError, ValueError, RuntimeError) as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="invalid_access_token",
            headers={"WWW-Authenticate": "Bearer"},
        ) from exc
    if not isinstance(claims, dict):
        raise HTTPException(status_code=401, detail="invalid_access_token")
    return claims


async def authenticate(
    request: Request,
    *,
    required_scope: str,
    tenant_required: bool = True,
) -> AuthContext:
    tenant_id = request.headers.get("X-Tenant-ID", "").strip()
    authorization = request.headers.get("Authorization", "").strip()

    if not authorization:
        if ALLOW_DEV_AUTH_BYPASS and APP_ENV in {"development", "test"}:
            if tenant_required and not tenant_id:
                raise HTTPException(status_code=400, detail="tenant_header_required")
            return AuthContext(
                subject="development-bypass",
                client_id="development-bypass",
                scopes=frozenset({required_scope}),
                tenants=frozenset({tenant_id}) if tenant_id else frozenset(),
                claims={"development_bypass": True},
            )
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="bearer_token_required",
            headers={"WWW-Authenticate": "Bearer"},
        )

    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="bearer_token_required",
            headers={"WWW-Authenticate": "Bearer"},
        )

    claims = await _decode(token)
    scopes = _scope_set(claims)
    if required_scope not in scopes:
        raise HTTPException(status_code=403, detail="required_scope_missing")

    tenants = _tenant_set(claims)
    if tenant_required:
        if not tenant_id:
            raise HTTPException(status_code=400, detail="tenant_header_required")
        if tenant_id not in tenants and "platform-admin" not in scopes:
            raise HTTPException(status_code=403, detail="tenant_mismatch")

    client_id = str(
        claims.get("azp")
        or claims.get("client_id")
        or claims.get("clientId")
        or ""
    )
    if not client_id:
        raise HTTPException(status_code=401, detail="client_identity_missing")

    return AuthContext(
        subject=str(claims["sub"]),
        client_id=client_id,
        scopes=scopes,
        tenants=tenants,
        claims=claims,
    )
