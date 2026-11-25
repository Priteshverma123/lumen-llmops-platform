"""Prompt registry + experiments API."""

from __future__ import annotations

import uuid
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Header, Request, Response
from pydantic import BaseModel, Field

from lumen.api.deps import Read, Write
from lumen.common.errors import Conflict
from lumen.experiments import service as exps
from lumen.registry.render import render
from lumen.registry.service import Registry
from lumen.storage.models import Experiment
from lumen.storage.postgres import session

router = APIRouter(tags=["prompts", "experiments"])


def reg(request: Request) -> Registry:
    return request.app.state.registry  # type: ignore[no-any-return]


class Message(BaseModel):
    role: Literal["system", "developer", "user", "assistant"]
    content: str = Field(max_length=200_000)


class VersionIn(BaseModel):
    messages: list[Message] = Field(min_length=1)
    variables_schema: dict[str, Any] = Field(default_factory=dict)
    model_config_: dict[str, Any] = Field(default_factory=dict, alias="model_config")
    commit_message: str = ""
    labels: list[str] = Field(default_factory=list, description="non-gated labels to set, e.g. ['staging']")

    model_config = {"populate_by_name": True}


@router.post("/v1/prompts/{name}/versions", status_code=201)
async def create_version(name: str, body: VersionIn, request: Request, p: Write) -> dict[str, Any]:
    async with session() as s:
        return await reg(request).create_version(
            s, p.project_id, name, messages=[m.model_dump() for m in body.messages],
            variables_schema=body.variables_schema, model_config=body.model_config_,
            commit_message=body.commit_message, author=p.key_id, labels=body.labels)


@router.get("/v1/prompts/{name}")
async def get_prompt(name: str, request: Request, response: Response, p: Read, label: str | None = None,
                     version: int | None = None,
                     if_none_match: Annotated[str | None, Header()] = None) -> Any:
    """SDK fetch path. ETag = content hash → unchanged prompts revalidate with a 304 and no body."""
    async with session() as s:
        data = await reg(request).get(s, p.project_id, name, label=label, version=version)
    etag = f'"{data["content_hash"]}:{data["version"]}"'
    if if_none_match == etag:
        return Response(status_code=304, headers={"ETag": etag})
    response.headers["ETag"] = etag
    # immutable versions can be cached forever; labels move, so only briefly
    response.headers["Cache-Control"] = "public, max-age=31536000, immutable" if version else "max-age=60"
    return data


@router.get("/v1/prompts/{name}/versions")
async def list_versions(name: str, request: Request, p: Read) -> list[dict[str, Any]]:
    async with session() as s:
        return await reg(request).list_versions(s, p.project_id, name)


class LabelIn(BaseModel):
    version: int
    reason: str = ""
    override: bool = False


@router.put("/v1/prompts/{name}/labels/{label}")
async def set_label(name: str, label: str, body: LabelIn, request: Request, p: Write) -> dict[str, Any]:
    async with session() as s:
        return await reg(request).set_label(s, p.project_id, name, label, body.version, actor=p.key_id,
                                            reason=body.reason, override=body.override)


@router.post("/v1/prompts/{name}/labels/{label}/rollback")
async def rollback(name: str, label: str, request: Request, p: Write) -> dict[str, Any]:
    async with session() as s:
        return await reg(request).rollback(s, p.project_id, name, label, actor=p.key_id)


class EvalIn(BaseModel):
    metrics: dict[str, Any]
    passed: bool
    git_sha: str = ""
    dataset_id: uuid.UUID | None = None


@router.post("/v1/prompts/{name}/versions/{version}/evals", status_code=201)
async def record_eval(name: str, version: int, body: EvalIn, request: Request, p: Write) -> dict[str, str]:
    """CI posts eval results here; a passing run unlocks promotion to 'production'."""
    async with session() as s:
        rid = await reg(request).record_eval(s, p.project_id, name, version, metrics=body.metrics, passed=body.passed,
                                             git_sha=body.git_sha, dataset_id=body.dataset_id)
    return {"eval_run_id": rid}


class RenderIn(BaseModel):
    variables: dict[str, Any] = Field(default_factory=dict)
    label: str | None = None
    version: int | None = None


@router.post("/v1/prompts/{name}/render")
async def render_prompt(name: str, body: RenderIn, request: Request, p: Read) -> dict[str, Any]:
    async with session() as s:
        data = await reg(request).get(s, p.project_id, name, label=body.label, version=body.version)
    return {"version": data["version"], "model_config": data["model_config"],
            "messages": render(data["messages"], data["variables_schema"], body.variables)}


# ----------------------------------------------------------------------------- experiments
class ExperimentIn(BaseModel):
    key: str = Field(pattern=r"^[a-z0-9][a-z0-9\-_]{1,99}$")
    description: str = ""
    unit: Literal["user", "session", "tenant"] = "user"
    variants: list[dict[str, Any]] = Field(min_length=2, description="[{name, weight, prompt?, version?, model?}]")
    primary_metric: dict[str, Any] = Field(default_factory=lambda: {"score": "groundedness"})
    guardrails: dict[str, float] = Field(default_factory=lambda: {"error_rate": 0.02, "cost_ratio": 1.3})


@router.post("/v1/experiments", status_code=201)
async def create_experiment(body: ExperimentIn, p: Write) -> dict[str, Any]:
    names = [v.get("name") for v in body.variants]
    if len(set(names)) != len(names) or not all(names):
        raise Conflict("variant names must be unique and non-empty")
    async with session() as s:
        exp = Experiment(project_id=p.project_id, key=body.key, description=body.description, unit=body.unit,
                         variants={"variants": body.variants}, primary_metric=body.primary_metric,
                         guardrails=body.guardrails)
        s.add(exp)
    return {"key": body.key, "status": "draft"}


@router.post("/v1/experiments/{key}/{action}")
async def experiment_action(key: str, action: Literal["start", "stop", "complete"], p: Write) -> dict[str, str]:
    target = {"start": "running", "stop": "stopped", "complete": "completed"}[action]
    async with session() as s:
        exp = await exps.set_status(s, p.project_id, key, target, reason=f"manual {action}")
    return {"key": key, "status": exp.status}


@router.get("/v1/experiments/{key}/assign")
async def assign(key: str, unit_id: str, p: Read) -> dict[str, Any]:
    async with session() as s:
        return await exps.assign_unit(s, p.project_id, key, unit_id)


@router.get("/v1/experiments/{key}/results")
async def results(key: str, request: Request, p: Read) -> dict[str, Any]:
    async with session() as s:
        exp = await exps.get_experiment(s, p.project_id, key)
    analysis = await exps.analyze(request.app.state.ch, exp)
    analysis["auto_stop_recommendation"] = exps.auto_stop_decision(analysis)
    return analysis
