"""Auth: project-scoped API keys (``lmn_…``, SHA-256 stored, Redis-cached for 60 s)."""

from __future__ import annotations

import hashlib
import secrets
import uuid
from dataclasses import dataclass
from typing import Annotated

import orjson
from fastapi import Depends, Header, Request
from sqlalchemy import select

from lumen.common.errors import Forbidden, Unauthorized
from lumen.storage.models import ApiKey
from lumen.storage.postgres import session

PREFIX = "lmn_"


def new_key() -> str:
    return PREFIX + secrets.token_urlsafe(24)


def hash_key(raw: str) -> str:
    return hashlib.sha256(raw.encode()).hexdigest()


@dataclass(slots=True)
class Principal:
    project_id: uuid.UUID
    key_id: str
    scopes: list[str]

    def has(self, scope: str) -> bool:
        return "admin" in self.scopes or scope in self.scopes


async def principal(request: Request, authorization: Annotated[str | None, Header()] = None) -> Principal:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise Unauthorized("missing bearer token")
    raw = authorization[7:].strip()
    if not raw.startswith(PREFIX):
        raise Unauthorized("invalid api key")
    h = hash_key(raw)
    redis = request.app.state.redis
    if (cached := await redis.get(f"auth:{h}")) is not None:
        d = orjson.loads(cached)
        if not d:
            raise Unauthorized("invalid api key")
        return Principal(uuid.UUID(d["project_id"]), d["key_id"], d["scopes"])
    async with session() as s:
        k = (await s.execute(select(ApiKey).where(ApiKey.key_hash == h, ApiKey.revoked_at.is_(None)))
             ).scalar_one_or_none()
    data = {"project_id": str(k.project_id), "key_id": str(k.id), "scopes": list(k.scopes)} if k else None
    await redis.set(f"auth:{h}", orjson.dumps(data), ex=60 if k else 10)
    if k is None:
        raise Unauthorized("invalid api key")
    return Principal(k.project_id, str(k.id), list(k.scopes))


def scope(name: str):  # type: ignore[no-untyped-def]
    async def dep(p: Annotated[Principal, Depends(principal)]) -> Principal:
        if not p.has(name):
            raise Forbidden(f"api key lacks the '{name}' scope")
        return p
    return dep


Ingest = Annotated[Principal, Depends(scope("ingest"))]
Read = Annotated[Principal, Depends(scope("read"))]
Write = Annotated[Principal, Depends(scope("write"))]
Admin = Annotated[Principal, Depends(scope("admin"))]
