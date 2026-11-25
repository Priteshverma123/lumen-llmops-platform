"""Prompt registry service: immutable versions, movable labels, eval-gated promotion, rollback.

Promotion gate: moving the ``production`` label to a version requires a passing ``eval_runs``
row linked to that version (newer than the version itself). An explicit override is possible
but recorded with actor + reason in ``label_history``: auditable, never silent.
"""

from __future__ import annotations

import uuid
from typing import Any

from redis.asyncio import Redis
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from lumen.common.errors import Conflict, NotFound, PromotionBlocked
from lumen.registry.render import content_hash, validate_template
from lumen.storage.models import EvalRun, LabelHistory, Prompt, PromptLabel, PromptVersion

GATED_LABELS = {"production"}
INVALIDATE_CHANNEL = "lumen:prompts:invalidate"


def version_dict(p: Prompt, v: PromptVersion, labels: list[str]) -> dict[str, Any]:
    return {"prompt": p.name, "prompt_id": str(p.id), "version": v.version, "version_id": str(v.id),
            "messages": v.messages["messages"], "variables_schema": v.variables_schema,
            "model_config": v.model_config_, "labels": labels, "content_hash": v.content_hash,
            "commit_message": v.commit_message, "author": v.author, "warnings": v.warnings,
            "created_at": v.created_at.isoformat() if v.created_at else None}


class Registry:
    def __init__(self, redis: Redis) -> None:
        self.redis = redis

    async def _prompt(self, s: AsyncSession, project_id: uuid.UUID, name: str) -> Prompt:
        p = (await s.execute(select(Prompt).where(Prompt.project_id == project_id, Prompt.name == name))
             ).scalar_one_or_none()
        if p is None:
            raise NotFound(f"prompt '{name}' not found")
        return p

    async def create_version(self, s: AsyncSession, project_id: uuid.UUID, name: str, *,
                             messages: list[dict[str, str]], variables_schema: dict[str, Any],
                             model_config: dict[str, Any], commit_message: str, author: str,
                             labels: list[str] | None = None) -> dict[str, Any]:
        warnings = validate_template(messages)
        p = (await s.execute(select(Prompt).where(Prompt.project_id == project_id, Prompt.name == name))
             ).scalar_one_or_none()
        if p is None:
            p = Prompt(id=uuid.uuid4(), project_id=project_id, name=name)
            s.add(p)
            await s.flush()
        h = content_hash(messages, variables_schema, model_config)
        latest = (await s.execute(select(PromptVersion).where(PromptVersion.prompt_id == p.id)
                                  .order_by(PromptVersion.version.desc()).limit(1))).scalar_one_or_none()
        if latest is not None and latest.content_hash == h:
            raise Conflict("identical to the latest version", details={"version": latest.version})
        next_v = (latest.version + 1) if latest else 1
        v = PromptVersion(id=uuid.uuid4(), prompt_id=p.id, version=next_v, messages={"messages": messages},
                          variables_schema=variables_schema, model_config_=model_config, content_hash=h,
                          commit_message=commit_message, author=author, warnings=warnings)
        s.add(v)
        try:
            await s.flush()
        except IntegrityError as exc:  # concurrent publish of the same version number
            raise Conflict("concurrent version creation, retry") from exc
        for label in labels or []:
            if label in GATED_LABELS:
                continue  # gated labels must go through set_label (needs eval evidence)
            await self.set_label(s, project_id, name, label, next_v, actor=author, reason="set on create")
        return version_dict(p, v, labels or [])

    async def get(self, s: AsyncSession, project_id: uuid.UUID, name: str, *, label: str | None = None,
                  version: int | None = None) -> dict[str, Any]:
        p = await self._prompt(s, project_id, name)
        if version is None:
            lab = await s.get(PromptLabel, (p.id, label or "production"))
            if lab is None:
                raise NotFound(f"label '{label or 'production'}' not set for prompt '{name}'")
            version = lab.version
        v = (await s.execute(select(PromptVersion).where(PromptVersion.prompt_id == p.id,
                                                          PromptVersion.version == version))).scalar_one_or_none()
        if v is None:
            raise NotFound(f"version {version} not found")
        labels = list((await s.execute(select(PromptLabel.label).where(
            PromptLabel.prompt_id == p.id, PromptLabel.version == version))).scalars())
        return version_dict(p, v, labels)

    async def list_versions(self, s: AsyncSession, project_id: uuid.UUID, name: str) -> list[dict[str, Any]]:
        p = await self._prompt(s, project_id, name)
        versions = (await s.execute(select(PromptVersion).where(PromptVersion.prompt_id == p.id)
                                    .order_by(PromptVersion.version.desc()))).scalars().all()
        labels = (await s.execute(select(PromptLabel).where(PromptLabel.prompt_id == p.id))).scalars().all()
        by_v: dict[int, list[str]] = {}
        for lab in labels:
            by_v.setdefault(lab.version, []).append(lab.label)
        return [version_dict(p, v, by_v.get(v.version, [])) for v in versions]

    async def set_label(self, s: AsyncSession, project_id: uuid.UUID, name: str, label: str, version: int, *,
                        actor: str, reason: str = "", override: bool = False) -> dict[str, Any]:
        p = await self._prompt(s, project_id, name)
        v = (await s.execute(select(PromptVersion).where(PromptVersion.prompt_id == p.id,
                                                          PromptVersion.version == version))).scalar_one_or_none()
        if v is None:
            raise NotFound(f"version {version} not found")
        if label in GATED_LABELS and not override:
            passing = (await s.execute(select(func.count()).select_from(EvalRun).where(
                EvalRun.prompt_version_id == v.id, EvalRun.passed.is_(True)))).scalar_one()
            if not passing:
                raise PromotionBlocked(f"version {version} has no passing eval run; run the eval suite or "
                                       "promote with override=true and a reason")
        if override and not reason:
            raise PromotionBlocked("an override requires a reason (it is audited)")
        current = await s.get(PromptLabel, (p.id, label))
        stmt = pg_insert(PromptLabel).values(prompt_id=p.id, label=label, version=version, updated_by=actor)
        await s.execute(stmt.on_conflict_do_update(index_elements=["prompt_id", "label"],
                                                   set_={"version": version, "updated_by": actor,
                                                         "updated_at": func.now()}))
        s.add(LabelHistory(prompt_id=p.id, label=label, from_version=current.version if current else None,
                           to_version=version, actor=actor, reason=reason, override=override))
        # SDKs subscribed to this channel drop their cached label immediately (else ≤ TTL staleness)
        await self.redis.publish(INVALIDATE_CHANNEL, f"{project_id}:{name}:{label}")
        return {"prompt": name, "label": label, "version": version,
                "previous": current.version if current else None, "override": override}

    async def rollback(self, s: AsyncSession, project_id: uuid.UUID, name: str, label: str, *, actor: str
                       ) -> dict[str, Any]:
        """Move the label back to the version it pointed to before the last change. Rollbacks
        skip the eval gate: that version was already in service (recorded as an override)."""
        p = await self._prompt(s, project_id, name)
        last = (await s.execute(select(LabelHistory).where(LabelHistory.prompt_id == p.id, LabelHistory.label == label)
                                .order_by(LabelHistory.created_at.desc()).limit(1))).scalar_one_or_none()
        if last is None or last.from_version is None:
            raise NotFound("nothing to roll back to")
        return await self.set_label(s, project_id, name, label, last.from_version, actor=actor,
                                    reason=f"rollback from v{last.to_version}", override=True)

    async def record_eval(self, s: AsyncSession, project_id: uuid.UUID, name: str, version: int, *,
                          metrics: dict[str, Any], passed: bool, git_sha: str = "",
                          dataset_id: uuid.UUID | None = None) -> str:
        p = await self._prompt(s, project_id, name)
        v = (await s.execute(select(PromptVersion).where(PromptVersion.prompt_id == p.id,
                                                          PromptVersion.version == version))).scalar_one()
        run = EvalRun(id=uuid.uuid4(), project_id=project_id, prompt_version_id=v.id, metrics=metrics, passed=passed,
                      git_sha=git_sha, dataset_id=dataset_id)
        s.add(run)
        return str(run.id)
